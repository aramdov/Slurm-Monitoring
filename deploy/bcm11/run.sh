#!/usr/bin/env bash
# BCM11 (YSU) launcher: GPU telemetry from the jobstats Prometheus, no node sampler.
#
# Start (as root on bcm11-headnode; runs as adovlatyan, logs to journald):
#   systemd-run --unit=slurm-dashboard-aram --uid=adovlatyan --gid=adovlatyan \
#     --working-directory=/home/adovlatyan/Slurm-Monitoring \
#     /home/adovlatyan/Slurm-Monitoring/deploy/bcm11/run.sh
# Stop:  systemctl stop slurm-dashboard-aram      Logs: journalctl -u slurm-dashboard-aram
# View from a laptop: ssh -N -L 19527:127.0.0.1:19527 <you>@<bcm11-headnode>
#   then open http://127.0.0.1:19527/
set -euo pipefail
cd "$(dirname "$0")/../.."

# Prometheus is loopback-only on bcm11-headnode (127.0.0.1:9090), scrape 30 s.
export SLURM_DASHBOARD_GPU_SOURCE=prometheus
export SLURM_DASHBOARD_PROMETHEUS_URL=http://127.0.0.1:9090
export SLURM_DASHBOARD_PROMETHEUS_SCRAPE_SEC=30
# /usr/bin/squeue here is Ubuntu's 23.11.4; the controller is 25.05.5. PATH covers bare
# calls elsewhere (jobstats_web.py's sacct, app.py's scontrol); SLURM_BIN covers slurm.py.
export SLURM_DASHBOARD_SLURM_BIN=/cm/local/apps/slurm/25.05/bin
export PATH="$SLURM_DASHBOARD_SLURM_BIN:$PATH"
export SLURM_CONF=/cm/shared/apps/slurm/etc/slurm/slurm.conf
# Separate DB from any other Slurm-Monitoring instance on this host.
export SLURM_DASHBOARD_DB="$PWD/data/slurm_history.db"
# Admin editing (units/projects) stays disabled unless a token file exists.
if [[ -r data/admin-token ]]; then
  SLURM_DASHBOARD_ADMIN_TOKEN=$(<data/admin-token); export SLURM_DASHBOARD_ADMIN_TOKEN
fi
# No SLURM_GPU_INGEST_TOKEN_FILE: /api/gpu-samples and /api/gpu-allocations answer 503.

# Ports 9100-9999 on this host belong to cmsh-agent; 19526 is Hrant's dashboard.
exec .venv/bin/gunicorn --workers 1 --threads 4 --timeout 120 \
  --bind 127.0.0.1:19527 --access-logfile - --error-logfile - app:app
