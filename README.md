# Slurm Cluster Dashboard

Live and historical Slurm GPU/CPU allocations, queue/wait history, project
attribution, aggregate filesystem samples, historical installed capacity, and
optional measured per-job NVIDIA GPU activity.

The main timeline colors jobs by observed mean GPU activity. Click a job for
per-GPU measurements and a chart with date/time hover values. Use **All GPUs** in job details for one chart per sampled GPU on a shared time scale. CPU-only and
unmeasured GPU jobs use distinct muted colors; pending jobs retain waiting-reason
colors and generated command controls. **The Usage page reports allocation-hours,
not measured utilization.** Historical allocation cannot reconstruct past GPU activity.

Node labels list configured partition membership in small muted text, refreshed
from Slurm on the existing partition-cache interval. These names describe node
membership, not per-user eligibility. Pending bars use one muted color per waiting
reason: blue for general waiting, violet for dependencies, magenta for impossible
dependencies, cyan for QOS/association limits and slate for holds. The legend lists
each category; job IDs and row positions do not change these colors.

## Run the controller

Requires Python 3.10+, Flask/Gunicorn and working `squeue`, `sinfo`, `scontrol`,
and `df` access as the service account. Use a persistent host and local persistent
SQLite storage, with one collector per database, not an ephemeral login container.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/gunicorn --workers 1 --threads 4 --timeout 120 \
  --bind 127.0.0.1:19526 app:app
```

Open `http://127.0.0.1:19526/`; allocation history is at `/usage`. Use **one
Gunicorn worker**: importing the app starts its recorder and initializes
`data/slurm_history.db`. Polling defaults to ten seconds. Set
`SLURM_DASHBOARD_DISABLE_RECORDER=1` for tests and maintenance imports.

Loopback binding is suitable for SSH tunnels or a same-host proxy. Node telemetry
requires a reachable private bind address or proxy. Binding alone does not open
firewalls or publish URLs. Verify actual dashboard HTML and JSON, not just HTTP
200: a portal can serve fallback HTML instead of forwarding the dashboard.

`config.py` controls refresh rates, history path, fallback resource counts and
retention. `SLURM_DASHBOARD_HOST`/`SLURM_DASHBOARD_PORT` configure the built-in
Python server; Gunicorn uses its own `--bind`. Job history has no default expiry;
aggregate disk samples retain 14 days and do not measure per-user disk usage.

## Install GPU exporters on nodes

Start with **[the deployment guide](deploy/GPU_TELEMETRY_SETUP.md)**. It includes
controller credentials, node prerequisites, installation, preflight, networking,
failed-install recovery, upgrades and stopping collection.

| Included file | Purpose |
|---|---|
| `scripts/gpu_node_sampler.py` | Export authenticated NVIDIA snapshots every 15 seconds |
| `gpu_metrics.py` | Explicit GRES/device mapping, allocation validation and parsers |
| `deploy/slurm-gpu-monitor.service` | Generic systemd template; replace placeholders |

Nodes need Python 3.10+, `nvidia-smi`, explicit GRES File mappings and HTTP access
to the receiver. The sampler uses only Python's standard library: no node pip/apt
dependencies. Local allocation lookup needs a working Slurm client;
`--controller-allocations` instead uses the authenticated controller API.
No job changes, GPU workloads, Slurm restart or NVIDIA-service restart are needed.

[GPU_TELEMETRY.md](GPU_TELEMETRY.md) explains metrics, mapping, sampling limits,
observed mean/std, sample coverage and retention. Missing counters remain null.
Sampling starts at installation, with no invented earlier measurements.

## Access

Anyone who can reach the server can read all dashboard views and read APIs,
including other users' jobs. There is no per-user login, SSO or "only my jobs"
scope. Use an authenticated proxy if viewing should be limited to cluster users.

Metadata writes (units, projects and assignments) require the controller's private
`SLURM_DASHBOARD_ADMIN_TOKEN`. The UI prompts when editing and stores it in browser
`sessionStorage`. The pending-job endpoint generates copyable `scontrol` commands;
it never executes them.

The separate collector credential protects `POST /api/gpu-samples` and
`GET /api/gpu-allocations/<node>`. It is supplied through systemd `LoadCredential`
or a private controller file. Never commit tokens or put them in command arguments,
URLs or logs. Use HTTPS or protected tunnels across untrusted networks.

## Historical installed capacity

Per-node GPU/CPU counts support heterogeneous nodes. `capacity_history` stores
capacity changes and separate drain/down overlays. Percentages integrate installed
resource-hours over the selected interval, including partial buckets. Drain/down
does not subtract from the installed-capacity denominator. Unknown earlier capacity
remains blank and suppresses whole-range percentages; current capacity is never
extrapolated backward.

On upgrade, the last metadata row seeds history only at its own timestamp. For a
reviewed backfill, first back up SQLite consistently, then run:

```bash
SLURM_DASHBOARD_DISABLE_RECORDER=1 .venv/bin/python \
  scripts/import_capacity_history.py --db data/slurm_history.db \
  --snapshots /secure/path/capacity-snapshots.json
```

Each JSON entry has `timestamp_ms`, `node_capacities` (per-node `gpus`/`cpus`),
optional `node_states`/`node_reasons`, and an evidence `source`. Imports are
idempotent; conflicting timestamps fail atomically. Use that cluster's own
accounting/configuration evidence, not job occupancy or another cluster's data.
No runtime databases or historical snapshots are published.

## Queue history

The Queue chart shows average pending demand across time intervals. Switch
between requested resources and job count, and filter by job size. CPU-only
uses requested CPUs. Bars group eligible pending work into five age bands up
to 7+ days. The median, P90 and job count describe the last sampled timestamp.
GPU and CPU allocation charts share the queue timeline. Their dashed status
lines show installed capacity minus each status count separately.

The recorder tracks pending reasons, dependencies and eligibility for future
samples. It excludes observed holds and dependency blocks. Historical points
use Slurm accounting's Eligible field and cannot reconstruct later unrecorded
blocks. Unknown samples remain gaps and are omitted from interval averages.

Optional historical import, from the repository root after initializing and
backing up the SQLite database:

```bash
.venv/bin/python scripts/import_queue_accounting.py
.venv/bin/python scripts/import_queue_accounting.py --cached --apply
```

The first invocation stages an estimate using `sacct` and reports the eligible
records. `--apply` replaces only the derived queue accounting snapshot, leaving
job history and live observations intact. Keep the projected records under
the ignored `data/` directory.

## Optional monitoring watchdog

On a Linux host with tmux, flock and Slurm accounting available, run
`./monitor-tmux.sh start` instead of running Gunicorn directly. The wrapper
starts the dashboard and a separate watchdog. `./monitor-tmux.sh stop` pauses
automatic restart. Run `scripts/ensure_monitor.sh` periodically via cron to
also recover if tmux is lost. Set `SLURM_DASHBOARD_HOST`,
`SLURM_DASHBOARD_PORT` and GPU ingest credential settings in the launch
environment if needed, including the cron environment when using cron.

The watchdog checks recorder freshness and queues observed outages for
accounting repair. It takes and rotates backups before nonempty repairs.
Recovery restores supported job records, without reconstructing missing GPU
or disk samples. Keep the private watchdog state and backups under `data/`.

## Upgrades and tests

Back up source/configuration and use SQLite's backup API before upgrading a
populated deployment. Preserve local launchers, credentials and dirty changes.
Restart only monitoring after source changes; static-only updates need a browser
refresh. Added telemetry tables coexist with allocation/capacity/attribution
records. Never reset an operational dirty checkout.

```bash
SLURM_DASHBOARD_DISABLE_RECORDER=1 .venv/bin/python -m unittest discover -s tests
npm ci
npm test
```

Node.js/npm are development dependencies only; the DOM refresh/hover test uses
jsdom pinned in `package-lock.json`. An optional production regression skips
unless `SLURM_PARAMETER_GOLF_TEST_DB` points to its explicit fixture. Tests do
not implicitly select a production database.
