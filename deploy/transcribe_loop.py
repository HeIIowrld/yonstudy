#!/usr/bin/env python3
"""Docker에서 폴더 전사를 겹침 없이 주기 실행한다."""

from __future__ import annotations

import os
import subprocess
import sys
import time


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(1, value)


def main() -> int:
    root = os.environ.get("YONSTUDY_TRANSCRIBE_ROOT", "/archive")
    interval = positive_int("YONSTUDY_TRANSCRIBE_INTERVAL", 1800)
    limit = positive_int("YONSTUDY_TRANSCRIBE_LIMIT", 1)
    command = [
        sys.executable,
        "/app/cli.py",
        "transcribe",
        root,
        "--limit",
        str(limit),
    ]
    while True:
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        print(f"{started} [transcribe] starting", flush=True)
        result = subprocess.run(command, check=False)
        print(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
            f"[transcribe] finished rc={result.returncode}; next in {interval}s",
            flush=True,
        )
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
