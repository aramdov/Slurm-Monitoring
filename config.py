"""App config. Nodes discovered from sinfo; GPUS/CPUS_PER_NODE are fallbacks when sinfo has no capacity."""
from datetime import datetime
import json
import os

PORT = int(os.environ.get("SLURM_DASHBOARD_PORT", "19526"))
HOST = os.environ.get("SLURM_DASHBOARD_HOST", "0.0.0.0")
ADMIN_TOKEN = os.environ.get("SLURM_DASHBOARD_ADMIN_TOKEN", "")


def recorder_disabled() -> bool:
    """Read at call time: config may be imported (e.g. via slurm) before a caller sets the flag."""
    return os.environ.get("SLURM_DASHBOARD_DISABLE_RECORDER", "").lower() in ("1", "true", "yes")


DISABLE_RECORDER = recorder_disabled()  # import-time value, kept for existing callers
GPUS_PER_NODE = 8
CPUS_PER_NODE = 224
REFRESH_INTERVALS = [5, 10, 30, 60]  # seconds
DEFAULT_REFRESH = 10

# Where per-job GPU measurements come from:
#   "sampler"    -- nodes push nvidia-smi snapshots to /api/gpu-samples (SQLite tables)
#   "prometheus" -- read jobstats' nvml_exporter series (labelled jobid=) from Prometheus
GPU_SOURCE = os.environ.get("SLURM_DASHBOARD_GPU_SOURCE", "sampler")
# Also drives the job page time series (jobstats_prom.py). Empty URL = time series off, button hidden.
PROMETHEUS_URL = os.environ.get("SLURM_DASHBOARD_PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")
# Extra label matchers added to every time-series query, e.g. cluster="slurm".
PROMETHEUS_SELECTOR = os.environ.get("SLURM_DASHBOARD_PROMETHEUS_SELECTOR", "").strip()
PROMETHEUS_SCRAPE_SEC = int(os.environ.get("SLURM_DASHBOARD_PROMETHEUS_SCRAPE_SEC", "30"))

# Directory holding squeue/sinfo/scontrol. Empty = whatever is first on PATH.
SLURM_BIN = os.environ.get("SLURM_DASHBOARD_SLURM_BIN", "")

# History settings
HISTORY_DB_PATH = os.environ.get(
    "SLURM_DASHBOARD_DB",
    os.path.join(os.path.dirname(__file__), "data", "slurm_history.db"),
)
HISTORY_RETENTION_DAYS = None  # None = keep forever (no cleanup)
JOB_NAME_PREFIX_LEN = 35
DURATION_STATS_TTL_SEC = 3600  # 1 hour
PARTITIONS_TTL_SEC = 300  # partition tiers are static cluster config

# Color palette for research units (12 distinct colors)
PROJECT_PALETTE = [
    '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6',
    '#ec4899', '#06b6d4', '#84cc16', '#f97316', '#6366f1',
    '#14b8a6', '#a855f7',
]

# Disk usage tracking
DISK_MIN_SIZE_GB = 200
DISK_RETENTION_DAYS = 14

# Per-person waiting view (Usage page, /api/waiting)
WAITING_THRESHOLDS_H = (2, 24)
# Per-user GPU cap by QOS (sacctmgr show qos format=name,maxtrespu), dated. None = no cap.
# Each period applies from "from" until the next period; the first has from=None.
# When a cap changes, add a period. The time is in `sacctmgr list transactions`.
# default_cap is used when a job's QOS is unknown (rows recorded before the qos column).
_CAPS_BEFORE_2026_11_25 = {
    "researcher": 35, "pi": 16, "student": 1, "admin": 64, "golf": 16, "normal": None,
    "research8_fullnode_guard": None,
}
WAITING_QOS_GPU_CAP_HISTORY = [
    {"from": None, "caps": _CAPS_BEFORE_2026_11_25, "default_cap": 35},
    # researcher 35 -> 14 (fairness trial with Hrant, sacctmgr transaction 2026-11-25T05:08:55-08:00).
    {"from": "2026-11-25T05:08:55-08:00",
     "caps": {**_CAPS_BEFORE_2026_11_25, "researcher": 14}, "default_cap": 14},
]


def parse_cap_history(periods):
    """[{from: ISO-with-offset | None, caps, default_cap}] -> sorted [{from_ms, caps, default_cap}]."""
    out = []
    for period in periods:
        start = period.get("from")
        from_ms = None if start is None else int(datetime.fromisoformat(start).timestamp() * 1000)
        out.append({"from_ms": from_ms, "caps": dict(period["caps"]),
                    "default_cap": period.get("default_cap")})
    return sorted(out, key=lambda p: -1 if p["from_ms"] is None else p["from_ms"])


def waiting_cap_history():
    """Env SLURM_DASHBOARD_WAITING_QOS_GPU_CAPS (one undated caps dict) overrides the dated list."""
    override = json.loads(os.environ.get("SLURM_DASHBOARD_WAITING_QOS_GPU_CAPS", "null"))
    if override:
        return [{"from_ms": None, "caps": override, "default_cap": WAITING_DEFAULT_GPU_CAP}]
    return parse_cap_history(WAITING_QOS_GPU_CAP_HISTORY)


WAITING_DEFAULT_GPU_CAP = 35          # override's unknown-QOS cap
WAITING_QOS_GPU_CAPS = parse_cap_history(WAITING_QOS_GPU_CAP_HISTORY)[-1]["caps"]  # newest, for old callers
WAITING_EXCLUDED_PARTITIONS = ("rtx",)
