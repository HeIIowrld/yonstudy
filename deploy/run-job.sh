#!/bin/bash
set -euo pipefail
umask 077

job="${1:-}"
config=/config/yonstudy.env
log=/data/logs/scheduler.log

if [[ ! -r "$config" ]]; then
    echo "$(date -Iseconds) [$job] missing $config" >>"$log"
    exit 1
fi

set -a
# The file is mounted read-only and is deliberately kept outside Git.
source "$config"
set +a

case "$job" in
    keepalive)
        args=(keepalive)
        lock_args=(-n)
        ;;
    monitor)
        args=(monitor)
        lock_args=(-n)
        ;;
    watch)
        args=(scheduled-watch --limit 1)
        lock_args=(-n)
        ;;
    daily)
        args=(automate --no-mail)
        lock_args=(-n)
        ;;
    report)
        args=(report --sync-if-stale --email-if-configured)
        lock_args=()
        ;;
    *)
        echo "unknown job: $job" >&2
        exit 2
        ;;
esac

{
    echo "$(date -Iseconds) [$job] starting"
    if /usr/bin/flock -E 0 "${lock_args[@]}" /run/yonstudy/automation.lock \
        /usr/local/bin/python /app/cli.py "${args[@]}"; then
        rc=0
    else
        rc=$?
    fi
    echo "$(date -Iseconds) [$job] finished rc=$rc"
} >>"$log" 2>&1

exit "$rc"
