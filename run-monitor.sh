#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
umask 077
mkdir -p data
exec >>data/monitor.log 2>&1
exec .venv/bin/gunicorn --workers 1 --threads 4 --timeout 120 \
  --bind "${SLURM_DASHBOARD_HOST:-127.0.0.1}:${SLURM_DASHBOARD_PORT:-19526}" \
  --pid data/monitor.pid app:app
