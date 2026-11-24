#!/usr/bin/env bash
set -euo pipefail
umask 077
export PATH=/usr/local/bin:/usr/bin:/bin
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
[[ ! -e $repo_dir/data/watchdog.paused ]] || exit 0
exec /usr/bin/flock -n -o "$repo_dir/data/bootstrap.lock" "$repo_dir/monitor-tmux.sh" start --automatic >/dev/null
