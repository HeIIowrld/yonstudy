#!/bin/sh
set -eu

# Python reads the environment file as data. Shell-sourcing a password would
# interpret characters such as $, !, backticks, and semicolons as code.
exec /usr/local/bin/python /app/deploy/run_job.py "$@"
