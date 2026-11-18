"""Per-job time series from the jobstats Prometheus (nvml_exporter and cgroup_exporter, 30 s scrape).

Builds PromQL for the job page charts, picks a step that keeps each line small, and (later in this
module) fetches and shapes `query_range` results. No I/O at import time.

Label conventions are the jobstats exporters' on every cluster we run: GPU series carry `jobid` (the raw
Slurm id), `instance` (`host:port`) and `minor_number`; cgroup series carry `jobid` and are filtered to
`step="",task=""`, the job-level cgroup, as upstream jobstats does.
"""
import re

SCRAPE_S = 30
STEPS = (30, 60, 120, 300)                          # offered on the page
LADDER = (30, 60, 120, 300, 600, 1800, 3600)        # steps the server may move to
AUTO_POINTS = 1500
MAX_POINTS = 5000                                   # Prometheus itself refuses > 11,000 per series

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
