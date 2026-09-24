#!/usr/bin/env python3
"""Run ASR in the compute LXC while keeping source media on the NAS remote."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yonstudy.transcribe import FasterWhisperTranscriber  # noqa: E402
from yonstudy.transcription_worker import run_once  # noqa: E402


MEDIA_PATTERNS = (
    "*.3gp", "*.aac", "*.flac", "*.m4a", "*.m4v", "*.mkv", "*.mov",
    "*.mp3", "*.mp4", "*.mpeg", "*.mpg", "*.ogg", "*.opus", "*.wav",
    "*.webm", "*.wma", "*.wmv", "*.srt", "*.vtt",
)


def _positive(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _run(*args: str) -> None:
    subprocess.run(args, check=True)


def _read_reprocess(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def pull_archive(remote: str, local_root: Path) -> None:
    local_root.mkdir(parents=True, exist_ok=True)
    command = ["rclone", "copy", remote, str(local_root), "--update", "--create-empty-src-dirs"]
    for pattern in MEDIA_PATTERNS:
        command += ["--filter", f"+ {pattern}"]
    command += ["--filter", "- *", "--checkers", "4", "--transfers", "2"]
    _run(*command)


def push_results(remote: str, remote_state: str, local_root: Path, state_dir: Path,
                 model_id: str) -> None:
    state_path = state_dir / "state.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for item in state.get("items", {}).values():
            relative = item.get("subtitle")
            if item.get("status") != "completed" or item.get("completed_model") != model_id or not relative:
                continue
            source = local_root / relative
            if source.is_file():
                # A newer NAS file is treated as a user edit and is never overwritten.
                _run("rclone", "copyto", str(source), remote.rstrip("/") + "/" + relative,
                     "--update", "--metadata")
    if state_dir.is_dir():
        _run("rclone", "copy", str(state_dir), remote_state, "--update", "--metadata")
    report = state_dir / "status.md"
    if report.is_file():
        _run("rclone", "copyto", str(report), remote.rstrip("/") + "/전사_현황.md",
             "--update", "--metadata")


def main() -> int:
    remote = os.environ.get("YONSTUDY_TRANSCRIBE_REMOTE", "").strip()
    remote_state = os.environ.get("YONSTUDY_TRANSCRIBE_REMOTE_STATE", "").strip()
    if not remote or ":" not in remote or "\n" in remote:
        raise SystemExit("YONSTUDY_TRANSCRIBE_REMOTE must be an rclone remote path")
    if not remote_state or ":" not in remote_state or "\n" in remote_state:
        raise SystemExit("YONSTUDY_TRANSCRIBE_REMOTE_STATE must be an rclone remote path")
    local_root = Path(os.environ.get("YONSTUDY_TRANSCRIBE_LOCAL_ROOT", "/srv/yonstudy-transcription/archive"))
    state_dir = Path(os.environ.get("YONSTUDY_TRANSCRIBE_STATE_DIR", "/srv/yonstudy-transcription/state"))
    reprocess_file = Path(os.environ.get("YONSTUDY_TRANSCRIBE_REPROCESS_LIST", str(state_dir / "reprocess.txt")))
    model = os.environ.get("YONSTUDY_TRANSCRIBE_MODEL", "large-v3-turbo")
    device = os.environ.get("YONSTUDY_TRANSCRIBE_DEVICE", "cpu")
    compute_type = os.environ.get("YONSTUDY_TRANSCRIBE_COMPUTE_TYPE", "int8")
    model_id = os.environ.get("YONSTUDY_TRANSCRIBE_MODEL_ID", f"{Path(model).name}-{compute_type}")
    model_cache = os.environ.get("YONSTUDY_TRANSCRIBE_MODEL_CACHE", "/models")
    language_value = os.environ.get("YONSTUDY_TRANSCRIBE_LANGUAGE", "auto").strip()
    language = None if language_value.casefold() in {"", "auto"} else language_value
    cpu_threads = _positive("YONSTUDY_TRANSCRIBE_CPU_THREADS", 12)
    beam_size = _positive("YONSTUDY_TRANSCRIBE_BEAM_SIZE", 1)
    limit = _positive("YONSTUDY_TRANSCRIBE_LIMIT", 1)
    interval = _positive("YONSTUDY_TRANSCRIBE_INTERVAL", 300)
    stable_seconds = _positive("YONSTUDY_TRANSCRIBE_STABLE_SECONDS", 120)
    state_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())

    def heartbeat() -> None:
        while not stop.is_set():
            (state_dir / "heartbeat").touch()
            stop.wait(30)

    threading.Thread(target=heartbeat, daemon=True).start()

    backend = None
    refresh = True
    while not stop.is_set():
        try:
            push_results(remote, remote_state, local_root, state_dir, model_id)
            if refresh:
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} [remote-transcribe] syncing NAS", flush=True)
                pull_archive(remote, local_root)
            reprocess = _read_reprocess(reprocess_file)
            preview = run_once(
                local_root, state_dir=state_dir, limit=limit, model=model, device=device,
                compute_type=compute_type, language=language, cpu_threads=cpu_threads,
                model_cache=model_cache, stable_seconds=stable_seconds, beam_size=beam_size, dry_run=True,
                reprocess_paths=reprocess, model_id=model_id, say=lambda _: None,
            )
            if preview["pending_files"]:
                if backend is None:
                    print(f"[remote-transcribe] loading {model_id}", flush=True)
                    backend = FasterWhisperTranscriber(
                        model=model, device=device, compute_type=compute_type, cpu_threads=cpu_threads,
                        model_cache=model_cache,
                    )
                result = run_once(
                    local_root, state_dir=state_dir, limit=limit, model=model, device=device,
                    compute_type=compute_type, language=language, cpu_threads=cpu_threads,
                    model_cache=model_cache, stable_seconds=stable_seconds, beam_size=beam_size,
                    transcriber=backend,
                    reprocess_paths=reprocess, model_id=model_id, say=print,
                )
                push_results(remote, remote_state, local_root, state_dir, model_id)
                (state_dir / "last-error.txt").unlink(missing_ok=True)
                print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False), flush=True)
                refresh = False
                stop.wait(5)
            else:
                print(f"[remote-transcribe] queue idle; next NAS sync in {interval}s", flush=True)
                refresh = True
                stop.wait(interval)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            print(f"[remote-transcribe] failed: {message}", flush=True)
            (state_dir / "last-error.txt").write_text(message + "\n", encoding="utf-8")
            refresh = True
            stop.wait(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
