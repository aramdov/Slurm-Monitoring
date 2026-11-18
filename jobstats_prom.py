"""Per-job time series from the jobstats Prometheus (nvml_exporter and cgroup_exporter, 30 s scrape).

Builds PromQL for the job page charts, picks a step that keeps each line small, and (later in this
module) fetches and shapes `query_range` results. No I/O at import time.

Label conventions are the jobstats exporters' on every cluster we run: GPU series carry `jobid` (the raw
Slurm id), `instance` (`host:port`) and `minor_number`; cgroup series carry `jobid` and are filtered to
`step="",task=""`, the job-level cgroup, as upstream jobstats does.
"""
import json
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SCRAPE_S = 30
STEPS = (30, 60, 120, 300)                          # offered on the page
LADDER = (30, 60, 120, 300, 600, 1800, 3600)        # steps the server may move to
AUTO_POINTS = 1500
MAX_POINTS = 5000                                   # Prometheus itself refuses > 11,000 per series

QUERY_TIMEOUT_S = 10
DEADLINE_S = 60                                     # per request; gunicorn kills a worker at 120 s
READ_MAX = 32 * 1024 ** 2
DIGITS = {"%": 1, "GB": 2, "cores": 2, "W": 0}
YMAX_FIXED = {"%": 100, "W": 1000}
ALLOC_HEADROOM = 1.15

_slot = threading.Semaphore(1)                      # one time-series request at a time

RAW_ID_RE = re.compile(r"[0-9]{1,10}")
AGGS = ("avg", "max")

CHARTS = (
    {"name": "duty", "title": "GPU duty", "unit": "%", "kind": "gpu", "scale": 1,
     "metric": "nvidia_gpu_duty_cycle"},
    {"name": "sm", "title": "SM activity", "unit": "%", "kind": "gpu", "scale": 100,
     "metric": "nvidia_gpu_sm_util_percent"},
    {"name": "tensor", "title": "Tensor activity", "unit": "%", "kind": "gpu", "scale": 100,
     "metric": "nvidia_gpu_any_tensor_util_percent"},
    {"name": "dram", "title": "DRAM bandwidth", "unit": "%", "kind": "gpu", "scale": 100,
     "metric": "nvidia_gpu_dram_bw_util_percent"},
    {"name": "gpu_mem", "title": "GPU memory", "unit": "GB", "kind": "gpu", "scale": 1e-9,
     "metric": "nvidia_gpu_memory_used_bytes"},
    {"name": "power", "title": "GPU power", "unit": "W", "kind": "gpu", "scale": 1e-3,
     "metric": "nvidia_gpu_power_usage_milliwatts"},
    {"name": "cpu", "title": "CPU cores in use", "unit": "cores", "kind": "job", "scale": 1},
    {"name": "ram", "title": "RAM", "unit": "GB", "kind": "job", "scale": 1e-9},
)


def _points(span_s, step_s):
    return max(0, int(span_s)) // step_s + 1


def choose_step(span_s, requested):
    """(step_s, coarsened). requested=None is auto: smallest LADDER step keeping <= AUTO_POINTS.
    An explicit step is raised along LADDER until the line has <= MAX_POINTS; coarsened says so."""
    if requested is None:
        for step in LADDER:
            if _points(span_s, step) <= AUTO_POINTS:
                return step, False
        return LADDER[-1], False
    i = LADDER.index(requested)
    while i < len(LADDER) - 1 and _points(span_s, LADDER[i]) > MAX_POINTS:
        i += 1
    return LADDER[i], LADDER[i] != requested


def build_queries(raw_id, step_s, agg, selector=""):
    """PromQL per chart name, plus the scalars cpu_alloc, ram_alloc and gpu_mem_total."""
    if not isinstance(raw_id, str) or not RAW_ID_RE.fullmatch(raw_id):
        raise ValueError("raw job id must be digits")
    if step_s not in LADDER:
        raise ValueError(f"step must be one of {LADDER}")
    if agg not in AGGS:
        raise ValueError("agg must be avg or max")
    extra = "," + selector if selector else ""
    gpu_sel = f'{{jobid="{raw_id}"{extra}}}'
    cg_sel = f'{{jobid="{raw_id}",step="",task=""{extra}}}'
    w = f"[{step_s}s]"
    raw = step_s == SCRAPE_S

    q = {}
    for c in CHARTS:
        if c["kind"] != "gpu":
            continue
        series = c["metric"] + gpu_sel
        q[c["name"]] = series if raw else f"{agg}_over_time({series}{w})"

    cpu = f"cgroup_cpu_total_seconds{cg_sel}"
    if raw:
        q["cpu"] = f"sum(rate({cpu}[1m]))"
    elif agg == "avg":
        q["cpu"] = f"sum(rate({cpu}{w}))"
    else:   # peak of the job total, never a sum of per-node peaks
        q["cpu"] = f"max_over_time(sum(rate({cpu}[1m]))[{step_s}s:{SCRAPE_S}s])"

    rss, shmem = f"cgroup_memory_rss_bytes{cg_sel}", f"cgroup_memory_shmem_bytes{cg_sel}"
    ram = f"sum(({rss} + {shmem}) or {rss})"
    q["ram"] = ram if raw else f"{agg}_over_time({ram}[{step_s}s:{SCRAPE_S}s])"

    q["cpu_alloc"] = f"sum(cgroup_cpus{cg_sel})"
    q["ram_alloc"] = f"sum(cgroup_memory_total_bytes{cg_sel})"
    q["gpu_mem_total"] = f"max(nvidia_gpu_memory_total_bytes{gpu_sel})"
    return q


class PromError(Exception):
    """Prometheus could not answer: busy, down, slow, refused the query or sent too much. Maps to 503."""

    def __init__(self, message, busy=False):
        super().__init__(message)
        self.message = message
        self.busy = busy


def query_range(url, promql, start, end, step, timeout):
    """POST /api/v1/query_range; returns data.result (a list of {metric, values})."""
    data = urllib.parse.urlencode({"query": promql, "start": str(start), "end": str(end),
                                   "step": str(step)}).encode()
    req = urllib.request.Request(url + "/api/v1/query_range", data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(READ_MAX + 1)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read(65536) or b"{}").get("error", "")
        except (ValueError, AttributeError, OSError):
            pass
        raise PromError(f"Prometheus answered HTTP {e.code}" + (f": {detail}" if detail else ""))
    except (socket.timeout, TimeoutError):
        raise PromError("Prometheus timed out")
    except (urllib.error.URLError, OSError) as e:
        raise PromError(f"Prometheus unreachable: {getattr(e, 'reason', e)}")
    if len(raw) > READ_MAX:
        raise PromError("Prometheus response too large")
    try:
        body = json.loads(raw)
    except ValueError:
        raise PromError("Prometheus sent a response that is not JSON")
    if not isinstance(body, dict) or body.get("status") != "success":
        err = body.get("error", "unknown error") if isinstance(body, dict) else "unknown error"
        raise PromError(f"Prometheus error: {err}")
    result = (body.get("data") or {}).get("result")
    return result if isinstance(result, list) else []


def _value(v, scale, digits):
    try:
        x = float(v) * scale
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return int(round(x)) if digits == 0 else round(x, digits)


def _points_of(series, scale, digits):
    out = []
    for pair in series.get("values") or ():
        try:
            t, v = pair
        except (TypeError, ValueError):
            continue
        x = _value(v, scale, digits)
        if x is not None:
            out.append([int(float(t)), x])
    return out


def _gpu_key(metric):
    host = str(metric.get("instance", "")).rsplit(":", 1)[0] or "?"
    minor = str(metric.get("minor_number", "?"))
    return host, minor


def _minor_sort(m):
    return (0, int(m), "") if m.isdigit() else (1, 0, m)


def _last_scalar(result):
    """A scalar query run as a range: the last finite value of its (single) series, else None."""
    for series in result[:1]:
        for _, v in reversed(series.get("values") or []):
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(x):
                return x
    return None


def fetch_job_series(url, raw_id, start, end, step_s, agg, selector="", deadline_s=DEADLINE_S):
    """Run every chart's query for one job and shape the result for the job page."""
    queries = build_queries(raw_id, step_s, agg, selector)
    if not _slot.acquire(blocking=False):
        raise PromError("another time-series request is running; try again", busy=True)
    try:
        t_end = time.monotonic() + deadline_s
        results = {}
        for name, promql in queries.items():
            remaining = t_end - time.monotonic()
            if remaining <= 0:
                raise PromError("Prometheus queries timed out")
            results[name] = query_range(url, promql, start, end, step_s,
                                        min(QUERY_TIMEOUT_S, remaining))
    finally:
        _slot.release()

    gpus = {}
    charts = {}
    for c in CHARTS:
        digits = DIGITS[c["unit"]]
        series = {}
        for s in results.get(c["name"], []):
            pts = _points_of(s, c["scale"], digits)
            if c["kind"] == "gpu":
                host, minor = _gpu_key(s.get("metric") or {})
                key = f"{host}/{minor}"
                gpus[key] = {"key": key, "host": host, "minor": minor}
            else:
                key = "job"
            if pts:
                series[key] = pts
        charts[c["name"]] = {"title": c["title"], "unit": c["unit"], "kind": c["kind"],
                             "ymax": YMAX_FIXED.get(c["unit"]), "allocated": None, "series": series}

    total = _last_scalar(results.get("gpu_mem_total", []))
    charts["gpu_mem"]["ymax"] = round(total * 1e-9, 1) if total else None
    for name, alloc_q, scale in (("cpu", "cpu_alloc", 1), ("ram", "ram_alloc", 1e-9)):
        alloc = _last_scalar(results.get(alloc_q, []))
        if alloc:
            a = round(alloc * scale, 2)
            charts[name]["allocated"] = int(a) if a == int(a) and scale == 1 else a
            charts[name]["ymax"] = round(a * ALLOC_HEADROOM, 2)

    ordered = sorted(gpus.values(), key=lambda g: (g["host"], _minor_sort(g["minor"])))
    return {"step_s": step_s, "agg": agg, "start": start, "end": end, "gpus": ordered, "charts": charts}
