"""Read-only /jobstats pages: per-job GPU/CPU summaries that the jobstats epilog stores in
sacct AdminComment as "JS1:" + base64(gzip(json)).

Nothing here writes to the dashboard database. Importing this module runs no subprocess or
network call. Contract: jobstats step 10 spec (sections 4-7).
"""
import base64
import binascii
import json
import logging
import math
import os
import re
import signal
import subprocess
import threading
import time
import zlib
from collections import Counter, OrderedDict
from functools import wraps

from flask import Blueprint, current_app, jsonify, request, send_from_directory

import config
import jobstats_prom

log = logging.getLogger(__name__)

bp = Blueprint("jobstats", __name__)

SACCT_FIELDS = ("JobIDRaw,JobID,User%64,JobName%300,State,NodeList%300,AllocTRES%300,ElapsedRaw,"
                "Submit,Start,End,AdminComment%60000")
N_FIELDS = 12
SEP = "\x1f"                      # ASCII unit separator: "|" in names or comments is harmless
SACCT_TIMEOUT_S = 10
SACCT_MAX_BYTES = 32 * 1024 ** 2
B64_MAX = 2_800_000               # base64 chars of one AdminComment payload
JSON_MAX = 2 * 1024 ** 2          # decompressed bytes of one payload
MAX_SPAN_MS = 90 * 86400 * 1000
MAX_ROWS = 5000
CACHE_TTL_S = 60
CACHE_ENTRIES = 8
CACHE_BYTES = 64 * 1024 ** 2
DETAIL_ENTRIES = 64
TS_ENTRIES = 16                   # finished jobs' time-series bodies, stored as JSON bytes
TS_CACHE_BYTES = 48 * 1024 ** 2   # all of them; one body over a quarter of this is not cached
SUMMARY_STATES = ("ok", "short", "none", "invalid", "other", "missing")

JOBID_RE = re.compile(r"[0-9]{1,10}(_[0-9]{1,10})?")
TRES_GPU_RE = re.compile(r"(?:^|,)gres/gpu=(\d+)(?:,|$)")
TRES_CPU_RE = re.compile(r"(?:^|,)cpu=(\d+)(?:,|$)")

# Row key -> (JS1 per-GPU field, scale to display unit). Means are over GPUs that have the field.
MEAN_FIELDS = {
    "gpu_pct": ("gpu_utilization", 1),
    "sm": ("sm_util_percent_avg", 100),
    "tc": ("any_tensor_util_percent_avg", 100),
    "dram": ("dram_bw_util_percent_avg", 100),
    "occ": ("sm_occupancy_percent_avg", 100),
    "fp32": ("fp32_util_percent_avg", 100),
    "int": ("integer_util_avg", 100),
    "power_w": ("power_usage_milliwatts_avg", 0.001),
}
SEEN_FIELDS = {"fp16_seen": "fp16_util_percent_max", "fp64_seen": "fp64_util_percent_max"}
COVERAGE_FIELDS = dict({k: f for k, (f, _) in MEAN_FIELDS.items()}, gpu_mem="gpu_used_memory")
LOW_DUTY_PCT = 20
NODE_SCALARS = ("cpus", "total_time", "used_memory", "total_memory")   # per node, not per GPU

METRIC_KEYS = (list(MEAN_FIELDS) + list(SEEN_FIELDS) +
               ["gpu_mem_peak_b", "gpu_mem_peak_pct", "cpu_eff", "cpu_used_s", "cpu_avail_s",
                "cpu_mem_peak_pct", "low_duty_gpus", "coverage", "field_gpus", "summary_gpus"])

_sacct_slot = threading.Semaphore(1)     # admission: one sacct at a time, shared by all routes
_cache_lock = threading.Lock()           # guards the dicts below; never held during I/O
_explorer_cache = OrderedDict()          # (q_from_s, q_to_s) -> (ts, size, rows, unparsed)
_detail_cache = OrderedDict()            # key -> (ts, body)
_ts_cache = OrderedDict()                # (jobid, submit, step_s, agg, requested) -> JSON bytes, finished jobs only


class SourceError(Exception):
    """sacct could not answer: busy, failed, timed out or too much output. Maps to 503."""

    def __init__(self, message, busy=False):
        super().__init__(message)
        self.message = message
        self.busy = busy


def clear_caches():
    with _cache_lock:
        _explorer_cache.clear()
        _detail_cache.clear()
        _ts_cache.clear()


# ---------------------------------------------------------------- decoding

def _num(v):
    """A finite JSON number, else None (bools, strings, NaN, Infinity and ints beyond float range)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return v if math.isfinite(v) else None
    except OverflowError:
        return None


def _finite(x):
    """Derived values: a result that overflowed to inf or nan becomes None, so JSON stays valid."""
    return x if x is None or math.isfinite(x) else None


def _clean_payload(obj):
    """Validate the JS1 schema; return a copy with non-finite metric values set to None, or None."""
    if not isinstance(obj, dict):
        return None
    gpus, total = _num(obj.get("gpus")), _num(obj.get("total_time"))
    nodes = obj.get("nodes")
    if gpus is None or gpus < 0 or total is None or total < 0:
        return None
    if not isinstance(nodes, dict) or not nodes or not all(isinstance(n, dict) for n in nodes.values()):
        return None
    clean = {}
    for name, node in nodes.items():
        out = {}
        for k, v in node.items():
            if isinstance(v, dict) and k not in NODE_SCALARS:
                out[k] = {str(m): _num(x) for m, x in v.items()}
            else:
                out[k] = _num(v)        # a scalar field sent as a dict becomes None
        clean[str(name)] = out
    return {"gpus": gpus, "total_time": total, "nodes": clean}


def decode_summary(comment):
    """AdminComment -> (summary_state, invalid_reason, payload or None). Bounded in memory."""
    if not comment:
        return "missing", None, None
    if not comment.startswith("JS1:"):
        return "other", None, None
    body = comment[4:]
    if body == "Short":
        return "short", None, None
    if body == "None":
        return "none", None, None
    if len(body) > B64_MAX:
        return "invalid", "too_large", None
    try:
        raw = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        return "invalid", "base64", None
    try:
        d = zlib.decompressobj(wbits=31)
        out = d.decompress(raw, JSON_MAX + 1)
        if len(out) > JSON_MAX:          # also covers input left unconsumed
            return "invalid", "too_large", None
        out += d.flush(JSON_MAX + 1 - len(out))
        if len(out) > JSON_MAX:
            return "invalid", "too_large", None
        if not d.eof:
            return "invalid", "gzip", None
    except zlib.error:
        return "invalid", "gzip", None
    try:
        obj = json.loads(out)
    except ValueError:            # JSONDecodeError and UnicodeDecodeError
        return "invalid", "json", None
    payload = _clean_payload(obj)
    if payload is None:
        return "invalid", "schema", None
    return "ok", None, payload


# ---------------------------------------------------------------- sacct parsing

def _epoch(s):
    return int(s) if s.isdigit() and int(s) > 0 else None


def parse_alloc_tres(tres):
    gpu, cpu = TRES_GPU_RE.search(tres or ""), TRES_CPU_RE.search(tres or "")
    return (int(gpu.group(1)) if gpu else 0, int(cpu.group(1)) if cpu else 0)


def expand_hostlist(s, _limit=4096):
    """Slurm hostlist ("a[01-03,7],b") -> names. Unparseable parts are kept as written."""
    if not s or s.startswith("None"):
        return []
    out = []
    for part in re.findall(r"[^,\[]+(?:\[[^\]]*\][^,\[]*)*", s):
        m = re.fullmatch(r"([^\[]*)\[([^\]]*)\](.*)", part)
        if not m:
            out.append(part)
            continue
        pre, body, post = m.groups()
        names = []
        for item in body.split(","):
            a, _, b = item.partition("-")
            if b and a.isdigit() and b.isdigit() and int(b) - int(a) < _limit:
                names += [f"{pre}{i:0{len(a)}d}{post}" for i in range(int(a), int(b) + 1)]
            elif not b and a:
                names.append(f"{pre}{a}{post}")
            else:
                names = [part]
                break
        for n in names:
            out += expand_hostlist(n, _limit) if "[" in n and n != part else [n]
        if len(out) > _limit:
            break
    return out


def parse_sacct(text):
    """sacct -P stdout -> (records, unparsed_line_count). Pending array ranges are dropped."""
    records, unparsed = [], 0
    for line in text.split("\n"):
        if not line:
            continue
        f = line.split(SEP)
        if len(f) != N_FIELDS:
            unparsed += 1
            continue
        jobid_raw, jobid, user, name, state, nodelist, tres, elapsed, submit, start, end, comment = f
        if "[" in jobid:              # unexpanded pending range, e.g. 1041_[20-30]
            continue
        if not JOBID_RE.fullmatch(jobid) or not jobid_raw.isdigit():
            unparsed += 1
            continue
        records.append({
            "jobid": jobid, "jobid_raw": jobid_raw, "user": user, "name": name, "state": state,
            "nodelist": nodelist, "tres": tres,
            "elapsed": int(elapsed) if elapsed.isdigit() else 0,
            "submit": _epoch(submit), "start": _epoch(start), "end": _epoch(end),
            "comment": comment,
        })
    return records, unparsed


# ---------------------------------------------------------------- derived values

def _gpu_values(payload, field):
    vals = []
    for node in payload["nodes"].values():
        d = node.get(field)
        if isinstance(d, dict):
            vals += [v for v in d.values() if v is not None]
    return vals


def derive_metrics(payload):
    """Per-job values from an ok JS1 payload (spec 5.1). Missing data is None, never 0."""
    m = {}
    for key, (field, scale) in MEAN_FIELDS.items():
        vals = _gpu_values(payload, field)
        # divide before summing so large finite inputs cannot overflow the sum
        m[key] = _finite(sum(v / len(vals) for v in vals) * scale) if vals else None
    for key, field in SEEN_FIELDS.items():
        vals = _gpu_values(payload, field)
        m[key] = any(v > 0 for v in vals) if vals else None

    used = _gpu_values(payload, "gpu_used_memory")
    m["gpu_mem_peak_b"] = max(used) if used else None
    pcts = []
    for node in payload["nodes"].values():
        u, t = node.get("gpu_used_memory"), node.get("gpu_total_memory")
        if isinstance(u, dict) and isinstance(t, dict):
            pcts += [u[g] / t[g] * 100 for g in u if u[g] is not None and (t.get(g) or 0) > 0]
    m["gpu_mem_peak_pct"] = _finite(max(pcts)) if pcts else None

    elapsed = payload["total_time"]
    nodes = payload["nodes"].values()
    m["cpu_eff"] = m["cpu_used_s"] = m["cpu_avail_s"] = None
    if all(n.get("cpus") is not None and n.get("total_time") is not None for n in nodes):
        used_s = sum(n["total_time"] for n in nodes)
        avail_s = sum(n["cpus"] for n in nodes) * elapsed
        if avail_s > 0 and math.isfinite(avail_s) and math.isfinite(used_s):
            eff = _finite(used_s / avail_s * 100)
            if eff is not None:
                m.update(cpu_eff=eff, cpu_used_s=used_s, cpu_avail_s=avail_s)
    mem = [n["used_memory"] / n["total_memory"] * 100 for n in nodes
           if n.get("used_memory") is not None and (n.get("total_memory") or 0) > 0]
    m["cpu_mem_peak_pct"] = _finite(max(mem)) if mem else None

    util = _gpu_values(payload, "gpu_utilization")
    m["low_duty_gpus"] = sum(1 for v in util if v < LOW_DUTY_PCT) if util else None

    gpus = payload["gpus"]
    m["summary_gpus"] = gpus
    if gpus > 0:
        counts = {key: len(_gpu_values(payload, field)) for key, field in COVERAGE_FIELDS.items()}
        m["field_gpus"] = counts
        m["coverage"] = {key: n / gpus for key, n in counts.items()}
    else:
        m["field_gpus"] = m["coverage"] = None
    return m


def node_details(payload):
    """Job page: per node CPU block and per GPU raw JS1 fields (minor -> {field: value})."""
    elapsed = payload["total_time"]
    out = {}
    for name, node in payload["nodes"].items():
        cpus, cpu_s = node.get("cpus"), node.get("total_time")
        gpus = {}
        for field, per_gpu in node.items():
            if isinstance(per_gpu, dict):
                for minor, v in per_gpu.items():
                    gpus.setdefault(minor, {})[field] = v
        out[name] = {
            "cpus": cpus,
            "cpu_eff": _finite(cpu_s / (cpus * elapsed) * 100)
                       if (cpus or 0) > 0 and cpu_s is not None and elapsed and math.isfinite(cpus * elapsed) else None,
            "mem_peak_b": node.get("used_memory"),
            "mem_total_b": node.get("total_memory"),
            "gpus": dict(sorted(gpus.items(), key=lambda kv: (len(kv[0]), kv[0]))),
        }
    return out


def build_row(rec, now_s):
    """sacct record -> (API row, payload or None)."""
    state, reason, payload = decode_summary(rec["comment"])
    alloc_gpus, alloc_cpus = parse_alloc_tres(rec["tres"])
    # sacct can show an estimated Start for a pending job; that job has not run.
    not_started = rec["start"] is None or rec["state"].startswith("PENDING")
    array_job, _, array_task = rec["jobid"].partition("_")
    row = {
        "key": f"{rec['jobid']}@{rec['submit']}",
        "jobid": rec["jobid"], "jobid_raw": rec["jobid_raw"],
        "array_job": array_job if array_task else None, "array_task": array_task or None,
        "user": rec["user"], "name": rec["name"], "state": rec["state"],
        "nodes": expand_hostlist(rec["nodelist"]),
        "alloc_gpus": alloc_gpus, "alloc_cpus": alloc_cpus, "elapsed_s": rec["elapsed"],
        "submit": rec["submit"], "start": rec["start"], "end": rec["end"],
        "not_started": not_started, "summary_state": state, "invalid_reason": reason,
        "gpu_hours": 0 if not_started else alloc_gpus * rec["elapsed"] / 3600,
    }
    row.update(derive_metrics(payload) if payload else dict.fromkeys(METRIC_KEYS))
    return row, payload


def filter_window(rows, from_s, to_s, now_s):
    """Keep rows whose run [start, end or now] overlaps [from_s, to_s]. Never-started rows are
    kept (flagged) when submitted before to_s and not ended before from_s."""
    kept = []
    for r in rows:
        end = r["end"] if r["end"] is not None else now_s
        if r["not_started"]:
            if (r["submit"] or 0) <= to_s and end >= from_s:
                kept.append(r)
        elif r["start"] <= to_s and end >= from_s:
            kept.append(r)
    return kept


def _sort_key(r):
    end = r["end"] if r["end"] is not None else math.inf
    return (end, r["start"] or 0, int(r["jobid_raw"]))


# ---------------------------------------------------------------- aggregation (spec 5.2)

def _percentile(sorted_vals, q):
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _spread(vals):
    if len(vals) < 3:
        return None
    s = sorted(vals)
    return {"p10": _percentile(s, 0.1), "median": _percentile(s, 0.5), "p90": _percentile(s, 0.9)}


def aggregate(rows):
    """Group summary. Reference implementation for the Groups view; never-started rows excluded."""
    rows = [r for r in rows if not r["not_started"]]
    ok = [r for r in rows if r["summary_state"] == "ok"]
    alloc_h = sum(r["gpu_hours"] for r in rows)
    summ_h = sum(r["gpu_hours"] for r in ok)
    out = {
        "jobs": len(rows),
        "states": dict(Counter(r["summary_state"] for r in rows)),
        "gpu_hours": alloc_h,
        "summarized_gpu_hours": summ_h,
        "summarized_share": summ_h / alloc_h if alloc_h else None,
    }
    for key in MEAN_FIELDS:
        num = den = 0.0
        for r in ok:
            n = (r["field_gpus"] or {}).get(key, 0)
            if r[key] is not None and n:
                num += r["elapsed_s"] * r[key] * n
                den += r["elapsed_s"] * n
        out[key] = {"mean": num / den if den else None,
                    "spread": _spread([r[key] for r in ok if r[key] is not None])}
    cpu = [r for r in ok if r["cpu_eff"] is not None]
    avail = sum(r["cpu_avail_s"] for r in cpu)
    out["cpu_eff"] = {"mean": sum(r["cpu_used_s"] for r in cpu) / avail * 100 if avail else None,
                      "spread": _spread([r["cpu_eff"] for r in cpu])}
    for key in ("cpu_mem_peak_pct", "gpu_mem_peak_pct"):
        vals = sorted(r[key] for r in ok if r[key] is not None)
        out[key] = {"median": _percentile(vals, 0.5), "max": vals[-1]} if vals else None
    for key in SEEN_FIELDS:
        vals = [r[key] for r in ok if r[key] is not None]
        out[key] = sum(vals) / len(vals) if vals else None
    rep = sum(r["elapsed_s"] * (r["field_gpus"] or {}).get("gpu_pct", 0) for r in ok if r["alloc_gpus"])
    alloc_s = sum(r["elapsed_s"] * r["alloc_gpus"] for r in ok if r["alloc_gpus"])
    out["coverage"] = rep / alloc_s if alloc_s else None
    return out


def group_key(row, mode):
    if mode == "array":
        return row["array_job"] or row["jobid"]
    if mode == "family":
        return re.sub(r"\d+", "#", row["name"])
    return row["name"]


# ---------------------------------------------------------------- sacct access

def run_capped(argv, env, timeout, max_bytes):
    """Run argv (no shell); return stdout text. Kills the process on timeout or byte cap."""
    timed_out = []

    def kill_group():
        # sacct runs in its own session, so this also reaches any child still holding stdout
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def kill():
        timed_out.append(True)
        kill_group()

    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    except OSError as e:
        raise SourceError(f"cannot run {argv[0]}: {e.strerror or e}")
    timer = threading.Timer(timeout, kill)
    timer.start()
    chunks, total = [], 0
    try:
        while True:
            b = proc.stdout.read1(1 << 20)
            if not b:
                break
            total += len(b)
            if total > max_bytes:
                kill_group()
                proc.wait()
                raise SourceError("too_large")
            chunks.append(b)
        rc = proc.wait()
    finally:
        timer.cancel()
        proc.stdout.close()
    if timed_out:
        raise SourceError("sacct timed out")
    if rc != 0:
        raise SourceError(f"sacct failed (exit {rc})")
    return b"".join(chunks).decode("utf-8", "replace")


def _iso_utc(s):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s))


def sacct(extra_args):
    """One sacct call under the shared admission slot. Times come back as UTC epoch seconds."""
    if not _sacct_slot.acquire(blocking=False):
        raise SourceError("busy", busy=True)
    try:
        argv = ["sacct", "-a", "-X", "-n", "-P", "--delimiter", SEP, *extra_args, "-o", SACCT_FIELDS]
        env = {**os.environ, "SLURM_TIME_FORMAT": "%s", "TZ": "UTC"}
        return run_capped(argv, env, SACCT_TIMEOUT_S, SACCT_MAX_BYTES)
    finally:
        _sacct_slot.release()


def query_bounds(from_ms, to_ms):
    """Outward-rounded query bounds in epoch s; results are then filtered to the exact window."""
    return (from_ms // 60000) * 60, -(-to_ms // 60000) * 60


def explorer_rows(from_ms, to_ms):
    q = query_bounds(from_ms, to_ms)
    now = time.time()
    with _cache_lock:
        hit = _explorer_cache.get(q)
        if hit and now - hit[0] < CACHE_TTL_S:
            _explorer_cache.move_to_end(q)
            return hit[2], hit[3]
    text = sacct(["-S", _iso_utc(q[0]), "-E", _iso_utc(q[1])])
    records, unparsed = parse_sacct(text)
    rows = [build_row(rec, now)[0] for rec in records]
    size = len(text) * 3
    if size > CACHE_BYTES:              # one result over the whole budget is served, not cached
        return rows, unparsed
    with _cache_lock:
        _explorer_cache[q] = (now, size, rows, unparsed)
        _explorer_cache.move_to_end(q)
        while len(_explorer_cache) > CACHE_ENTRIES or sum(v[1] for v in _explorer_cache.values()) > CACHE_BYTES:
            _explorer_cache.popitem(last=False)
    return rows, unparsed


# ---------------------------------------------------------------- routes

def _envelope(body, source="sacct"):
    return dict(body, generated_at=int(time.time() * 1000), source=source)


def _error(message, status, retry_after=None):
    resp = jsonify({"error": message})
    resp.status_code = status
    if retry_after:
        resp.headers["Retry-After"] = str(retry_after)
    return resp


def _contained(fn):
    """Any failure answers this request only; the dashboard and recorder carry on."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SourceError as e:
            return _error(e.message, 503, retry_after=2 if e.busy else None)
        except Exception:
            log.exception("jobstats request failed")
            return _error("internal error", 500)
    return wrapped


def _int_arg(name):
    v = request.args.get(name, "")
    return int(v) if re.fullmatch(r"-?[0-9]{1,15}", v) else None


@bp.route("/jobstats")
def jobstats_page():
    return send_from_directory(current_app.static_folder, "jobstats.html")


@bp.route("/api/jobstats/jobs")
@_contained
def api_jobstats_jobs():
    from_ms, to_ms = _int_arg("from"), _int_arg("to")
    if from_ms is None or to_ms is None:
        return _error("from and to (epoch ms) are required", 400)
    if from_ms < 0 or not 0 < to_ms - from_ms <= MAX_SPAN_MS:
        return _error("window must be 0 < to - from <= 90 days", 400)
    if to_ms > (time.time() + 86400) * 1000:
        return _error("to is more than a day in the future", 400)
    rows, unparsed = explorer_rows(from_ms, to_ms)
    selected = filter_window(rows, from_ms / 1000, to_ms / 1000, time.time())
    counts = dict.fromkeys(SUMMARY_STATES, 0)
    for r in selected:
        if not r["not_started"]:
            counts[r["summary_state"]] += 1
    counts["unparsed"] = unparsed
    counts["not_started"] = sum(1 for r in selected if r["not_started"])
    selected.sort(key=_sort_key, reverse=True)
    return jsonify(_envelope({
        "jobs": selected[:MAX_ROWS],
        "counts": counts,
        "window": {"from": from_ms, "to": to_ms},
        "truncated": len(selected) > MAX_ROWS,
    }))


def _job_args(jobid):
    """(submit, None) for a valid id and submit, else (None, error response)."""
    if not JOBID_RE.fullmatch(jobid):
        return None, _error("bad job id", 400)
    submit = _int_arg("submit")
    if submit is None or not 86400 < submit <= time.time() + 86400:
        return None, _error("submit (epoch s) is required", 400)
    return submit, None


def job_detail(jobid, submit):
    """{"job": row, "nodes": ...} for one (jobid, submit), through the 60 s detail cache; None if absent."""
    key = f"{jobid}@{submit}"
    now = time.time()
    with _cache_lock:
        hit = _detail_cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            _detail_cache.move_to_end(key)
            return hit[1]
    records, _ = parse_sacct(sacct(["-j", jobid, "-S", _iso_utc(submit - 86400), "-E", "now"]))
    match = [r for r in records if r["jobid"] == jobid and r["submit"] == submit]
    if not match:
        return None
    row, payload = build_row(match[0], now)
    body = {"job": row, "nodes": node_details(payload) if payload else {}}
    with _cache_lock:
        _detail_cache[key] = (now, body)
        _detail_cache.move_to_end(key)
        while len(_detail_cache) > DETAIL_ENTRIES:
            _detail_cache.popitem(last=False)
    return body


@bp.route("/api/jobstats/job/<jobid>")
@_contained
def api_jobstats_job(jobid):
    submit, err = _job_args(jobid)
    if err:
        return err
    body = job_detail(jobid, submit)
    if body is None:
        return _error("job not found", 404)
    return jsonify(_envelope(dict(body, timeseries=bool(config.PROMETHEUS_URL))))


@bp.route("/api/jobstats/job/<jobid>/timeseries")
@_contained
def api_jobstats_timeseries(jobid):
    """Per-GPU and whole-job series from Prometheus. Only the page's Generate button calls this."""
    submit, err = _job_args(jobid)
    if err:
        return err
    step_arg, agg = request.args.get("step", ""), request.args.get("agg", "")
    if step_arg == "auto":
        requested = None
    elif step_arg.isdigit() and int(step_arg) in jobstats_prom.STEPS:
        requested = int(step_arg)
    else:
        return _error("step must be 30, 60, 120, 300 or auto", 400)
    if agg not in jobstats_prom.AGGS:
        return _error("agg must be avg or max", 400)
    if not config.PROMETHEUS_URL:
        return _error("time series are not configured on this dashboard", 404)
    detail = job_detail(jobid, submit)
    if detail is None:
        return _error("job not found", 404)
    row = detail["job"]
    if row["not_started"] or not row["start"]:
        return _error("job has not started", 409)
    start = int(row["start"])
    finished = row["end"] is not None
    end = int(row["end"]) if finished else int(time.time())
    end = max(end, start)
    step_s, coarsened = jobstats_prom.choose_step(end - start, requested, gpus=row["alloc_gpus"])
    if requested is None or step_s == jobstats_prom.SCRAPE_S:
        agg = "avg"     # auto is always the average; at 30 s each point is one raw sample
    key = (jobid, submit, step_s, agg, requested)
    if finished:
        with _cache_lock:
            hit = _ts_cache.get(key)
            if hit is not None:
                _ts_cache.move_to_end(key)
                return current_app.response_class(hit, mimetype="application/json")
    try:
        body = jobstats_prom.fetch_job_series(config.PROMETHEUS_URL, row["jobid_raw"], start, end,
                                              step_s, agg, selector=config.PROMETHEUS_SELECTOR)
    except jobstats_prom.PromError as e:
        return _error(e.message, 503, retry_after=2 if e.busy else None)
    body = dict(body, coarsened=coarsened, requested_step=requested,
                max_points=jobstats_prom.point_limit(row["alloc_gpus"]))
    raw = json.dumps(_envelope(body, source="prometheus"), separators=(",", ":")).encode()
    del body
    if finished and len(raw) <= TS_CACHE_BYTES // 4:
        with _cache_lock:
            _ts_cache[key] = raw
            _ts_cache.move_to_end(key)
            while len(_ts_cache) > TS_ENTRIES or sum(len(v) for v in _ts_cache.values()) > TS_CACHE_BYTES:
                _ts_cache.popitem(last=False)
    return current_app.response_class(raw, mimetype="application/json")
