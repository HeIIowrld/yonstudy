#!/usr/bin/env python3
"""Load container secrets literally and run one scheduled yonstudy command."""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


CONFIG = Path("/config/yonstudy.env")
LOG = Path("/data/logs/scheduler.log")
LOCK = "/run/yonstudy/automation.lock"
WATCH_JITTER_SECONDS = 45 * 60

JOBS: dict[str, tuple[list[str], bool]] = {
    "recordings": (
        [
            "scan-recordings", "/data/inbox/recordings",
            "--destination", "/archive", "--consume",
        ],
        False,
    ),
    "keepalive": (["keepalive"], False),
    "monitor": (["monitor"], False),
    "watch": (["scheduled-watch", "--limit", "1"], False),
    "daily": (["automate", "--no-mail"], False),
    "report": (["report", "--sync-if-stale", "--email-if-configured"], True),
}


def read_environment(path: Path) -> dict[str, str]:
    """Parse the small KEY=VALUE file without executing any of its contents."""
    values: dict[str, str] = {}
    for source in path.read_text(encoding="utf-8").splitlines():
        line = source.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key.replace("_", "").isalnum() and not key[0].isdigit():
            values[key] = value
    return values


def stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def scheduled_delay_seconds(job: str) -> int:
    """Spread automated lecture starts without delaying unrelated jobs."""
    if job != "watch":
        return 0
    raw = os.environ.get("YONSTUDY_WATCH_JITTER_SECONDS", str(WATCH_JITTER_SECONDS))
    try:
        maximum = max(0, int(raw))
    except ValueError:
        maximum = WATCH_JITTER_SECONDS
    return random.randint(0, maximum) if maximum else 0


def job_environment(path: Path = CONFIG) -> dict[str, str]:
    """Build a cron-safe environment while allowing explicit config overrides."""
    env = os.environ.copy()
    # Debian cron does not preserve every image ENV entry. Playwright otherwise
    # falls back to /root/.cache and misses the browser installed in the image.
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")
    # LearnUs streams H.264/AAC. Use the branded browser installed in the image
    # because bundled Chromium may omit proprietary media codecs.
    env.setdefault("YONSTUDY_BROWSER_CHANNEL", "chrome")
    env.update(read_environment(path))
    return env


def main() -> int:
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job not in JOBS:
        print(f"unknown job: {job}", file=sys.stderr)
        return 2
    if not CONFIG.is_file():
        print(f"missing configuration: {CONFIG}", file=sys.stderr)
        return 1

    env = job_environment()
    args, wait_for_lock = JOBS[job]
    command = ["/usr/bin/flock", "-E", "0"]
    if not wait_for_lock:
        command.append("-n")
    command += [LOCK, "/usr/local/bin/python", "/app/cli.py", *args]

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as output:
        delay = scheduled_delay_seconds(job)
        if delay:
            output.write(f"{stamp()} [{job}] waiting {delay}s jitter\n")
            output.flush()
            time.sleep(delay)
        output.write(f"{stamp()} [{job}] starting\n")
        output.flush()
        result = subprocess.run(
            command,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output.write(f"{stamp()} [{job}] finished rc={result.returncode}\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
