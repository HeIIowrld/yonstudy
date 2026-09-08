#!/bin/sh
set -u

interval="${YONSTUDY_UPDATE_INTERVAL:-300}"

while :; do
    if ! /source/deploy/update-once.sh; then
        echo "$(date -Iseconds) update check failed; the running image was kept" >&2
    fi
    sleep "$interval"
done
