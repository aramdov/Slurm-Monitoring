#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
socket_name=slurm-monitoring
session_name=dashboard
token_file="$repo_dir/data/admin-token"
pid_file="$repo_dir/data/monitor.pid"
port=${SLURM_DASHBOARD_PORT:-19526}

usage() {
  printf 'Usage: %s {start|status|attach|stop|token}\n' "$0" >&2
  exit 2
}

has_session() {
  tmux -L "$socket_name" has-session -t "$session_name" 2>/dev/null
}

monitor_pid() {
  [[ -r $pid_file ]] || return 1
  local pid cmd owner
  pid=$(<"$pid_file")
  [[ $pid =~ ^[0-9]+$ ]] || return 1
  [[ -r /proc/$pid/cmdline ]] || return 1
  owner=$(stat -c %U "/proc/$pid")
  [[ $owner == "$(id -un)" ]] || return 1
  cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline")
  [[ $cmd == *"$repo_dir/.venv/bin/gunicorn"* && $cmd == *"app:app"* ]] || return 1
  printf '%s\n' "$pid"
}

ensure_watchdog() {
  if ! tmux -L "$socket_name" has-session -t watchdog 2>/dev/null; then
    tmux -L "$socket_name" new-session -d -s watchdog \
      "umask 077; exec $repo_dir/.venv/bin/python -u $repo_dir/scripts/monitor_watchdog.py --root $repo_dir >>$repo_dir/data/watchdog-process.log 2>&1"
  fi
}

case ${1:-} in
  start)
    cd "$repo_dir"
    mkdir -p data
    if [[ ${2:-} == --automatic ]]; then
      [[ ! -e $repo_dir/data/watchdog.paused ]] || exit 0
    else
      rm -f -- "$repo_dir/data/watchdog.paused"
    fi
    ensure_watchdog
    if has_session; then
      printf 'Monitoring is already running in tmux.\n'
      exit 0
    fi
    if ss -H -ltn "sport = :${port}" | grep -q .; then
      printf 'Port %s is already in use. Stop the old monitor before starting this one.\n' "$port" >&2
      exit 1
    fi
    if [[ ! -x .venv/bin/gunicorn ]]; then
      printf 'Missing .venv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n' >&2
      exit 1
    fi
    mkdir -p data
    if [[ ! -s $token_file ]]; then
      umask 077
      .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))' >"$token_file"
    fi
    chmod 600 "$token_file"
    export SLURM_DASHBOARD_ADMIN_TOKEN
    SLURM_DASHBOARD_ADMIN_TOKEN=$(<"$token_file")
    tmux -L "$socket_name" new-session -d -s "$session_name" "$repo_dir/run-monitor.sh"
    for _ in 1 2 3 4 5; do
      if has_session && monitor_pid >/dev/null && curl -fsS "http://127.0.0.1:${port}/api/config" >/dev/null 2>&1; then
        printf 'Monitoring started as %s in persistent tmux on %s.\n' "$(id -un)" "$(hostname -s)"
        printf 'Use "%s token" when you need the units/assignments admin token.\n' "$0"
        exit 0
      fi
      sleep 1
    done
    printf 'The monitor did not become healthy. Recent log output:\n' >&2
    tail -n 30 data/monitor.log >&2 || true
    exit 1
    ;;
  status)
    if tmux -L "$socket_name" has-session -t watchdog 2>/dev/null; then
      if [[ -e $repo_dir/data/watchdog.paused ]]; then
        printf 'Watchdog: running (paused by stop command)\n'
      else
        printf 'Watchdog: running\n'
      fi
    else
      printf 'Watchdog: stopped\n'
    fi
    if has_session; then
      printf 'tmux session: running\n'
    else
      printf 'tmux session: stopped\n'
    fi
    if monitor_pid >/dev/null; then
      printf 'Gunicorn process: running as %s\n' "$(id -un)"
    else
      printf 'Gunicorn process: stopped\n'
    fi
    curl -fsS "http://127.0.0.1:${port}/api/config" >/dev/null 2>&1 \
      && printf 'HTTP check: healthy\n' \
      || printf 'HTTP check: unavailable\n'
    ;;
  attach)
    exec tmux -L "$socket_name" attach-session -t "$session_name"
    ;;
  stop)
    mkdir -p "$repo_dir/data"
    touch "$repo_dir/data/watchdog.paused"
    touch "$repo_dir/data/watchdog.recovery-needed"
    if pid=$(monitor_pid); then
      kill -TERM "$pid"
      for _ in 1 2 3 4 5; do
        if ! kill -0 "$pid" 2>/dev/null; then break; fi
        sleep 1
      done
      if kill -0 "$pid" 2>/dev/null; then
        printf 'Gunicorn did not stop after TERM; refusing to force-kill it.\n' >&2
        exit 1
      fi
    fi
    if has_session; then
      tmux -L "$socket_name" kill-session -t "$session_name"
    fi
    printf 'Monitoring stopped.\n'
    ;;
  token)
    if [[ ! -r $token_file ]]; then
      printf 'No admin token exists yet; start the monitor first.\n' >&2
      exit 1
    fi
    cat "$token_file"
    printf '\n'
    ;;
  *) usage ;;
esac
