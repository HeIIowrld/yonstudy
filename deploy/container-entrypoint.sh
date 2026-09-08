#!/bin/bash
set -euo pipefail

install -d -m 0700 /data/store /data/logs /run/yonstudy
touch /data/logs/scheduler.log
chmod 0600 /data/logs/scheduler.log

if [[ ! -r /config/yonstudy.env ]]; then
    echo "missing configuration: /config/yonstudy.env" >&2
    exit 1
fi

if [[ ! -r /config/rclone.conf ]]; then
    echo "missing rclone configuration: /config/rclone.conf" >&2
    exit 1
fi

exec /usr/sbin/cron -f
