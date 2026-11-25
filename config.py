"""App config. Nodes discovered from sinfo; GPUS/CPUS_PER_NODE are fallbacks when sinfo has no capacity."""
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
# Per-user GPU cap by QOS (sacctmgr show qos format=name,maxtrespu). None = no cap.
# Checked 2026-11-24. Update when a cap changes.
WAITING_QOS_GPU_CAPS = json.loads(os.environ.get("SLURM_DASHBOARD_WAITING_QOS_GPU_CAPS", "null")) or {
    "researcher": 35, "pi": 16, "student": 1, "admin": 64, "golf": 16, "normal": None,
    "research8_fullnode_guard": None,
}
WAITING_DEFAULT_GPU_CAP = 35          # used when a job's QOS is unknown
WAITING_EXCLUDED_PARTITIONS = ("rtx",)
