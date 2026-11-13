"""Per-job GPU telemetry read from Prometheus instead of the node push sampler.

For clusters running Princeton jobstats: nvml_exporter on each GPU node labels
every GPU series with ``jobid`` (the raw SLURM_JOB_ID) while a job holds that
GPU, and Prometheus scrapes it. This module produces the same ``gpu_telemetry``
and trend shapes as ``history.get_gpu_metrics`` / ``history.get_gpu_trend`` so
the frontend needs no changes.

Facts the queries rely on:
* A series carries jobid="X" only while job X holds that GPU, so an
  ``*_over_time`` window that reaches back past the job start covers exactly
  the job's samples. A requeued job keeps its id; its attempts are merged, as
  the SQLite path also does.
* ``stddev_over_time`` is the population standard deviation, matching the
  Welford moments in gpu_metrics.combine_moments.
* Missing series stay missing. Nothing here turns an absent metric into 0.
"""
import json
import math
import re
import threading
import urllib.parse
import urllib.request

import config
from gpu_metrics import combine_moments

MIB = 1048576
MAX_WINDOW_SEC = 180 * 86400        # jobstats Prometheus retention on BCM11
_ID_RE = re.compile(r"\d+")

# Dashboard metric -> Prometheus metric name and the factor that turns a sample
# into the dashboard's unit. "vram" is derived from used/total below.
BASE_METRICS = {
    "gpu": ("nvidia_gpu_duty_cycle", 1.0),                  # percent, 0-100
    "used_mib": ("nvidia_gpu_memory_used_bytes", 1 / MIB),  # bytes -> MiB
}


def memory_metric():
    """Which Prometheus series feeds the dashboard's third line, "Memory activity".

    The fork's sampler reads nvidia-smi ``utilization.memory_util``: percent of
    time the memory controller was busy. nvml_exporter does not export that
    counter. What it does export:

      nvidia_gpu_dram_bw_util_percent  GPM DRAM bandwidth, stored as a 0-1
                                       fraction despite the name (D29). This is
                                       the share of peak bandwidth, the thing
                                       the fork's UI text says it is NOT.

    Return (prometheus_metric_name, scale_to_percent) or None to leave the line
    empty ("—"). Unknown is never shown as zero.
    """
    # TODO(aram): choose. See the trade-off in the chat / PR description.
    return None


def _metric_table():
    table = dict(BASE_METRICS)
    mem = memory_metric()
    if mem:
        table["memory"] = mem
    return table


class PrometheusError(RuntimeError):
    pass


def _query(expr, at_sec):
    params = urllib.parse.urlencode({"query": expr, "time": f"{at_sec:.3f}"})
    return _get(f"/api/v1/query?{params}")


def _query_range(expr, start_sec, end_sec, step_sec):
    params = urllib.parse.urlencode({"query": expr, "start": f"{start_sec:.3f}",
                                     "end": f"{end_sec:.3f}", "step": f"{step_sec}s"})
    return _get(f"/api/v1/query_range?{params}")


def _get(path):
    try:
        with urllib.request.urlopen(config.PROMETHEUS_URL + path, timeout=20) as resp:
            body = json.load(resp)
    except (OSError, ValueError) as exc:
        raise PrometheusError(str(exc)) from exc
    if body.get("status") != "success":
        raise PrometheusError(body.get("error", "query failed"))
    return body["data"]["result"]


def _finite(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _device_key(labels):
    return (labels.get("jobid"), labels.get("instance", "").split(":")[0], labels.get("uuid", ""))


def _by_device(result):
    """{(jobid, node, uuid): (labels, value)} from an instant-vector result."""
    out = {}
    for item in result:
        out[_device_key(item["metric"])] = (item["metric"], _finite(item["value"][1]))
    return out


def _selector(name, ids):
    return f'{name}{{jobid=~"{"|".join(ids)}"}}'


def _fetch_devices(ids, now_sec, window_sec):
    """Lifetime moments, first/last sample time and latest reading per job GPU."""
    window = f"{max(60, min(MAX_WINDOW_SEC, int(window_sec)))}s"
    step = f"{config.PROMETHEUS_SCRAPE_SEC}s"
    fresh = f"{config.PROMETHEUS_SCRAPE_SEC * 2 + 15}s"
    duty = _selector("nvidia_gpu_duty_cycle", ids)
    devices = {}

    def stat(fn, expr):
        return _by_device(_query(f"{fn}({expr}[{window}])", now_sec))

    for metric, (name, scale) in _metric_table().items():
        sel = _selector(name, ids)
        count, avg = stat("count_over_time", sel), stat("avg_over_time", sel)
        std, peak = stat("stddev_over_time", sel), stat("max_over_time", sel)
        latest = _by_device(_query(f"last_over_time({sel}[{fresh}])", now_sec))
        for key, (labels, n) in count.items():
            dev = devices.setdefault(key, {"labels": labels, "stats": {}, "current": {}})
            mean, sd, top = avg.get(key, (0, None))[1], std.get(key, (0, None))[1], peak.get(key, (0, None))[1]
            if not n or mean is None or top is None:
                # combine_moments needs mean and peak whenever n > 0; a partial
                # answer is recorded as unobserved rather than guessed.
                dev["stats"][metric] = {"n": 0}
            else:
                dev["stats"][metric] = {"n": int(n), "mean": mean * scale,
                                        "m2": (sd * scale) ** 2 * n if sd is not None else 0.0,
                                        "peak": top * scale}
            if key in latest and latest[key][1] is not None:
                dev["current"][metric] = latest[key][1] * scale

    total = _by_device(_query(f"max_over_time({_selector('nvidia_gpu_memory_total_bytes', ids)}[{window}])", now_sec))
    first = _by_device(_query(f"min_over_time(timestamp({duty})[{window}:{step}])", now_sec))
    last = _by_device(_query(f"max_over_time(timestamp({duty})[{window}:{step}])", now_sec))
    for key, dev in devices.items():
        total_mib = total.get(key, (0, None))[1]
        dev["total_mib"] = total_mib / MIB if total_mib else None
        dev["first_ms"] = int(first[key][1] * 1000) if key in first and first[key][1] else None
        dev["last_ms"] = int(last[key][1] * 1000) if key in last and last[key][1] else None
        used = dev["stats"].get("used_mib")
        if used and used["n"] and dev["total_mib"]:
            k = 100 / dev["total_mib"]
            dev["stats"]["vram"] = {"n": used["n"], "mean": used["mean"] * k,
                                    "m2": used["m2"] * k * k, "peak": used["peak"] * k}
            if "used_mib" in dev["current"]:
                dev["current"]["vram"] = dev["current"]["used_mib"] * k
        dev["current"]["total_mib"] = dev["total_mib"]
    return devices


def _summarize(job, devices, now_ms):
    metrics = ("gpu", "vram", "memory", "used_mib")
    period_ms = config.PROMETHEUS_SCRAPE_SEC * 1000
    stale_ms = period_ms * 2 + 15_000
    running = job.get("state") == "RUNNING"
    out_devices, fresh = [], []
    def order(item):
        (_, node, _), dev = item
        minor = dev["labels"].get("minor_number", "")
        return (node, int(minor) if minor.isdigit() else 1 << 30)

    for (_, node, uuid), dev in sorted(devices.items(), key=order):
        live = running and dev["last_ms"] is not None and now_ms - dev["last_ms"] <= stale_ms
        current = {m: dev["current"].get(m) for m in (*metrics, "total_mib")} if live else None
        if current:
            fresh.append(current)
        labels = dev["labels"]
        minor = labels.get("minor_number")
        out_devices.append({
            "node": node, "uuid": uuid,
            "minor": int(minor) if minor and minor.isdigit() else minor,
            "first_ms": dev["first_ms"], "last_ms": dev["last_ms"], "total_mib": dev["total_mib"],
            "lifetime": {m: combine_moments([dev["stats"].get(m, {})]) for m in metrics},
            "current": current,
        })
    current = {}
    for m in (*metrics, "total_mib"):
        vals = [c[m] for c in fresh if c.get(m) is not None]
        current[m] = sum(vals) / len(vals) if vals else None
    expected = sum(a.get("gpus", 0) for a in job.get("allocations", [])) or job.get("req_gpus", 0)
    samples = sum(d["lifetime"]["gpu"]["n"] for d in out_devices)
    start, end = job.get("start_time"), job.get("end_time")
    elapsed = max(0, min(end or now_ms, now_ms) - (start or now_ms))
    firsts = [d["first_ms"] for d in out_devices if d["first_ms"]]
    lasts = [d["last_ms"] for d in out_devices if d["last_ms"]]
    return {
        "source": "prometheus",
        "lifetime": {m: combine_moments([devices[k]["stats"].get(m, {}) for k in devices]) for m in metrics},
        "current": current, "fresh_gpus": len(fresh), "expected_gpus": expected,
        "first_ms": min(firsts) if firsts else None, "last_ms": max(lasts) if lasts else None,
        "observed_gpu_hours": samples * period_ms / 3_600_000,
        "coverage_pct": min(100, samples * period_ms / (elapsed * expected) * 100) if elapsed and expected else None,
        "sample_period_sec": config.PROMETHEUS_SCRAPE_SEC,
        "devices": out_devices,
    }


# Finished jobs never change in Prometheus: cache their summaries.
_final_cache: dict[tuple, dict | None] = {}
_final_lock = threading.Lock()
_FINAL_CACHE_MAX = 20_000
_BATCH = 150   # ids per regex selector


def _eligible(job):
    raw = job.get("raw_job_id") or job.get("job_id")
    return (job.get("start_time") and job.get("state") != "PENDING"
            and (job.get("req_gpus") or 0) > 0 and raw and _ID_RE.fullmatch(str(raw)))


def get_gpu_metrics(jobs, now_ms):
    """Attach ``gpu_telemetry`` to each job that has Prometheus GPU samples."""
    pending = {}
    for job in jobs:
        if not _eligible(job):
            continue
        raw = str(job.get("raw_job_id") or job["job_id"])
        done = job.get("state") != "RUNNING" and job.get("end_time")
        key = (raw, job.get("start_time"), job.get("end_time"))
        if done:
            with _final_lock:
                if key in _final_cache:
                    if _final_cache[key]:
                        job["gpu_telemetry"] = _final_cache[key]
                    continue
        pending.setdefault(raw, []).append((job, key, done))
    if not pending:
        return jobs
    now_sec = now_ms / 1000
    ids = sorted(pending)
    for i in range(0, len(ids), _BATCH):
        chunk = ids[i:i + _BATCH]
        earliest = min(j["start_time"] for r in chunk for j, _, _ in pending[r])
        try:
            devices = _fetch_devices(chunk, now_sec, now_sec - earliest / 1000 + 120)
        except PrometheusError as exc:
            print(f"Prometheus GPU query failed: {exc}")
            continue
        grouped = {}
        for key, dev in devices.items():
            grouped.setdefault(key[0], {})[key] = dev
        for raw in chunk:
            for job, cache_key, done in pending[raw]:
                summary = _summarize(job, grouped[raw], now_ms) if raw in grouped else None
                if summary:
                    job["gpu_telemetry"] = summary
                # Cache a finished job only once its last sample is safely behind us.
                if done and now_ms - job["end_time"] > config.PROMETHEUS_SCRAPE_SEC * 4000:
                    with _final_lock:
                        if len(_final_cache) >= _FINAL_CACHE_MAX:
                            _final_cache.clear()
                        _final_cache[cache_key] = summary
    return jobs


def get_gpu_trend(job, now_ms, max_points=720):
    """Job-average chart points: mean of per-GPU bin averages, plus min/max GPU."""
    if not job or not _eligible(job):
        return {"points": [], "raw_retained": False}
    raw = str(job.get("raw_job_id") or job["job_id"])
    start = job["start_time"] / 1000
    end = min(job.get("end_time") or now_ms, now_ms) / 1000
    scrape = config.PROMETHEUS_SCRAPE_SEC
    step = max(scrape, math.ceil((end - start) / (max_points - 1)))
    rng = f"{step}s" if step > scrape else f"{scrape * 2}s"
    series = {}
    for metric, (name, scale) in _metric_table().items():
        per_gpu = f"avg_over_time({_selector(name, [raw])}[{rng}])"
        series[metric] = (_query_range(f"avg({per_gpu})", start, end, step), scale)
        if metric == "gpu":
            series["gpu_min"] = (_query_range(f"min({per_gpu})", start, end, step), scale)
            series["gpu_max"] = (_query_range(f"max({per_gpu})", start, end, step), scale)
            series["n"] = (_query_range(f"count({per_gpu})", start, end, step), 1)
    used_ratio = (f"avg(avg_over_time({_selector('nvidia_gpu_memory_used_bytes', [raw])}[{rng}])"
                  f" / on(instance, uuid) max_over_time({_selector('nvidia_gpu_memory_total_bytes', [raw])}[{rng}]))")
    series["vram"] = (_query_range(used_ratio, start, end, step), 100)
    points = {}
    for field, (result, scale) in series.items():
        if field == "used_mib":
            continue
        for item in result[:1]:
            for ts, value in item["values"]:
                v = _finite(value)
                t = int(float(ts) * 1000)
                points.setdefault(t, {"t": t, "gpu": None, "vram": None, "memory": None,
                                      "gpu_min": None, "gpu_max": None, "n": 0})
                if v is not None:
                    points[t][field] = int(v) if field == "n" else v * scale
    return {"raw_retained": bool(points), "bucket_ms": step * 1000, "source": "prometheus",
            "points": [points[t] for t in sorted(points) if points[t]["n"]]}
