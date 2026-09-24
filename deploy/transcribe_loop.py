#!/usr/bin/env python3
"""검토·재전사 큐를 계속 처리하고 대기 중에는 주기적으로 다시 확인한다."""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(1, value)


def worker_options() -> dict:
    language = os.environ.get("YONSTUDY_TRANSCRIBE_LANGUAGE", "auto").strip()
    return {
        "state_dir": os.environ.get("YONSTUDY_TRANSCRIBE_STATE_DIR", "/state"),
        "limit": positive_int("YONSTUDY_TRANSCRIBE_LIMIT", 1),
        "model": os.environ.get("YONSTUDY_TRANSCRIBE_MODEL", "small"),
        "model_id": os.environ.get("YONSTUDY_TRANSCRIBE_MODEL_ID", "small-int8"),
        "device": os.environ.get("YONSTUDY_TRANSCRIBE_DEVICE", "cpu"),
        "compute_type": os.environ.get("YONSTUDY_TRANSCRIBE_COMPUTE_TYPE", "int8"),
        "language": None if language.casefold() in {"", "auto"} else language,
        "cpu_threads": positive_int("YONSTUDY_TRANSCRIBE_CPU_THREADS", 2),
        "model_cache": os.environ.get("YONSTUDY_TRANSCRIBE_MODEL_CACHE", "/models"),
        "stable_seconds": positive_int("YONSTUDY_TRANSCRIBE_STABLE_SECONDS", 120),
        "beam_size": positive_int("YONSTUDY_TRANSCRIBE_BEAM_SIZE", 1),
    }


def read_reprocess_list(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def next_delay(result: dict, idle_seconds: int) -> int:
    # pending_files는 지금 처리 가능한 나머지 큐다. 실패 backoff 파일은 제외된다.
    return 5 if result.get("pending_files", 0) > 0 and not result.get("locked") else idle_seconds


def publish_report(state_dir: Path, destination: Path) -> None:
    source = state_dir / "status.md"
    if not source.is_file():
        return
    body = source.read_bytes()
    if destination.is_file() and destination.read_bytes() == body:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part-{os.getpid()}")
    temporary.write_bytes(body)
    temporary.replace(destination)


def main() -> int:
    from yonstudy.transcription_worker import run_once

    root = os.environ.get("YONSTUDY_TRANSCRIBE_ROOT", "/archive")
    interval = positive_int("YONSTUDY_TRANSCRIBE_INTERVAL", 300)
    options = worker_options()
    state_dir = Path(options["state_dir"])
    reprocess_file = Path(os.environ.get("YONSTUDY_TRANSCRIBE_REPROCESS_LIST", str(state_dir / "reprocess.txt")))
    report_path = Path(os.environ.get("YONSTUDY_TRANSCRIBE_REPORT", str(Path(root) / "전사_현황.md")))
    state_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())

    def heartbeat() -> None:
        while not stop.is_set():
            (state_dir / "heartbeat").touch()
            try:
                publish_report(state_dir, report_path)
            except OSError as exc:
                print(f"[transcribe-review] report publish failed: {exc}", flush=True)
            stop.wait(30)

    threading.Thread(target=heartbeat, daemon=True).start()
    while not stop.is_set():
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} [transcribe-review] scanning {root}", flush=True)
        try:
            result = run_once(root, reprocess_paths=read_reprocess_list(reprocess_file), **options)
            publish_report(state_dir, report_path)
            print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False, default=str), flush=True)
            (state_dir / "last-error.txt").unlink(missing_ok=True)
            delay = next_delay(result, interval)
        except Exception as exc:
            # ASR 의존성·디스크 오류도 보고하고 재시도한다. 기존 자막은 worker가 보존한다.
            message = f"{type(exc).__name__}: {exc}"
            print(f"[transcribe-review] failed: {message}", flush=True)
            (state_dir / "last-error.txt").write_text(message + "\n", encoding="utf-8")
            delay = interval
        print(f"[transcribe-review] next scan in {delay}s", flush=True)
        stop.wait(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
