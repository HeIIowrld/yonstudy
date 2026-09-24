"""검토를 통과한 자막만 게시하는 NAS 전사 작업자.

상태와 원본 자막 백업은 학습 자료와 분리한다. 한 번의 실행은 제한된 수의
파일을 처리하며, 잠금과 영속적인 실패 기록으로 중복 실행과 무한 재시도를 막는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .subtitle_review import probe_duration, review_segments, review_subtitle
from .transcribe import (
    MEDIA_EXTENSIONS,
    FasterWhisperTranscriber,
    render_srt,
    target_subtitle,
)

MAX_ATTEMPTS = 2
RETRY_SECONDS = 1800
AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"})


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stat(path: Path) -> list[int]:
    row = path.stat()
    return [row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns]


def paired_subtitles(media: Path) -> list[Path]:
    """실제 언어 태그 자막만 찾고 .backup.srt 같은 보관 파일은 제외한다."""
    pattern = re.compile(
        re.escape(media.stem) + r"(?:\.[a-z]{2,3}(?:[-_][a-z0-9]+)*)?\.(?:srt|vtt)$",
        re.IGNORECASE,
    )
    return sorted(
        (path for path in media.parent.iterdir()
         if not path.name.startswith(".") and pattern.fullmatch(path.name)
         and path.name[len(media.stem) + 1:].split(".")[0].casefold() not in {"bak", "old", "tmp"}
         and path.is_file() and not path.is_symlink()),
        key=lambda path: path.name.casefold(),
    )


def _snapshot(media: Path) -> dict:
    subtitles = {}
    for path in paired_subtitles(media):
        before = _stat(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if before != _stat(path):
            raise RuntimeError("자막 파일을 확인하는 동안 내용이 변경되었습니다")
        subtitles[path.name] = {"stat": before, "sha256": digest}
    return {"media": _stat(media), "subtitles": subtitles}


def _fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _save(state_dir: Path, state: dict) -> None:
    state["updated_at"] = _stamp()
    _atomic_text(state_dir / "state.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    rows = ["# 강의 녹음·영상 자막 처리", "", f"갱신: {state['updated_at']}", "",
            "| 파일 | 상태 | 시도 | 사유 |", "| --- | --- | ---: | --- |"]
    labels = {"ok": "정상 자막 유지", "pending": "처리 대기", "running": "전사 중",
              "completed": "완료", "failed": "재시도 대기", "blocked": "자동 재시도 중단",
              "waiting": "복사 안정화 대기", "changed": "파일 변경으로 보류", "gone": "파일 없음"}
    for name, item in sorted(state["items"].items()):
        reason = "; ".join(str(v) for v in item.get("reasons", []))
        if item.get("error"):
            reason = str(item["error"])
        values = [name, labels.get(item["status"], item["status"]), str(item.get("attempts", 0)), reason]
        values = [value.replace("|", "\\|").replace("\n", " ") for value in values]
        rows.append("| " + " | ".join(values) + " |")
    _atomic_text(state_dir / "status.md", "\n".join(rows) + "\n")


def _read_state(state_dir: Path, root: Path) -> dict:
    path = state_dir / "state.json"
    if not path.exists():
        return {"version": 1, "root": str(root), "items": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("root") != str(root) or not isinstance(state.get("items"), dict):
        raise ValueError("전사 상태 디렉터리가 다른 자료 폴더에 연결되어 있습니다")
    return state


def _failure(item: dict, exc: Exception | str) -> None:
    item.update(status="blocked" if item["attempts"] >= MAX_ATTEMPTS else "failed",
                error=str(exc), finished_at=_stamp(), next_retry_at=time.time() + RETRY_SECONDS)


def _backup_subtitle(source: Path, destination: Path, expected_hash: str) -> None:
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == expected_hash:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        shutil.copy2(source, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected_hash:
            raise FileChanged("기존 자막을 백업하는 동안 내용이 변경되었습니다")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _publish(media: Path, segments: list, language: str | None,
             before: dict, state_dir: Path, key: str) -> tuple[Path, list[str]]:
    output = target_subtitle(media, language)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".part", dir=output.parent)
    temporary = Path(temporary_name)
    backups = []
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(render_srt(segments))
            stream.flush()
            os.fsync(stream.fileno())
        if _snapshot(media) != before:
            raise FileChanged("전사 중 미디어 또는 기존 자막이 변경되었습니다")
        identity = hashlib.sha256(key.encode()).hexdigest()[:20]
        backup_dir = state_dir / "backups" / identity / _fingerprint(before)
        for name, expected in before["subtitles"].items():
            source = media.parent / name
            destination = backup_dir / name
            # 중간 종료로 부분 백업이 남지 않도록 검증 후 원자적으로 게시한다.
            _backup_subtitle(source, destination, expected["sha256"])
            backups.append(str(destination))
        if backups:
            _atomic_text(backup_dir / "source.json", json.dumps({"media": str(media), "snapshot": before}, ensure_ascii=False, indent=2))
        if _snapshot(media) != before:
            raise FileChanged("자막 백업 중 원본이 변경되었습니다")
        if output.name in before["subtitles"]:
            temporary.replace(output)
        else:
            # 다른 프로그램이 같은 이름의 자막을 먼저 만들면 덮어쓰지 않는다.
            try:
                os.link(temporary, output)
            except FileExistsError as exc:
                raise FileChanged("실행 중 새로운 자막이 생성되었습니다") from exc
        for name, expected in before["subtitles"].items():
            obsolete = media.parent / name
            if obsolete == output:
                continue
            # 검토한 불량 자막만 제거한다. 백업은 별도 상태 폴더에 남아 있다.
            if _stat(obsolete) == expected["stat"] and hashlib.sha256(obsolete.read_bytes()).hexdigest() == expected["sha256"]:
                obsolete.unlink()
        return output, backups
    finally:
        temporary.unlink(missing_ok=True)


class FileChanged(RuntimeError):
    """실행 중 사용자가 수정한 자료는 그대로 둔다."""


def run_once(root, *, state_dir, limit=1, model="medium", device="cpu", compute_type="int8",
             language=None, cpu_threads=2, model_cache=None, stable_seconds=120,
             dry_run=False, transcriber=None, probe=None, say=print, beam_size=1,
             reprocess_paths=(), model_id=None) -> dict:
    """큐를 검토하고 최대 ``limit``개를 처리한다. ``probe(path)``는 초를 반환한다."""
    import fcntl

    root = Path(root).expanduser().resolve()
    state_dir = Path(state_dir).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    if limit < 1 or stable_seconds < 0 or beam_size < 1:
        raise ValueError("limit/beam_size는 1 이상, stable_seconds는 0 이상이어야 합니다")
    state_dir.mkdir(parents=True, exist_ok=True)
    result = {"root": str(root), "state_dir": str(state_dir), "locked": False,
              **{f"{key}_files": 0 for key in ("media", "ok", "suspect", "missing", "waiting", "pending",
                                             "selected", "completed", "failed", "changed", "blocked")}, "items": []}
    with (state_dir / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["locked"] = True
            return result
        try:
            return _run_locked(root, state_dir, result, limit=limit, model=model, device=device,
                               compute_type=compute_type, language=language, cpu_threads=cpu_threads,
                               model_cache=model_cache, stable_seconds=stable_seconds, dry_run=dry_run,
                               transcriber=transcriber, probe=probe or probe_duration, say=say or (lambda _: None),
                               beam_size=beam_size, reprocess_paths=set(reprocess_paths), model_id=model_id)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _run_locked(root, state_dir, result, *, limit, model, device, compute_type, language,
                cpu_threads, model_cache, stable_seconds, dry_run, transcriber, probe, say, beam_size,
                reprocess_paths, model_id):
    state = _read_state(state_dir, root)
    queue = []
    seen = set()
    paths = sorted(root.rglob("*"), key=lambda path: str(path).casefold())
    for media in paths:
        if (media.suffix.casefold() not in MEDIA_EXTENSIONS or not media.is_file() or media.is_symlink()
            or state_dir in media.parents or any(part.startswith(".") for part in media.relative_to(root).parts)):
            continue
        key = media.relative_to(root).as_posix()
        seen.add(key)
        result["media_files"] += 1
        old = state["items"].get(key, {})
        try:
            snapshot = _snapshot(media)
            fingerprint = _fingerprint(snapshot)
            item = dict(old) if old.get("fingerprint") == fingerprint else {
                "attempts": 0, "total_attempts": old.get("total_attempts", 0)}
            item.update(media=key, fingerprint=fingerprint, checked_at=_stamp())
            state["items"][key] = item
            if media.stat().st_size == 0 or time.time() - media.stat().st_mtime < stable_seconds:
                item.update(status="waiting", reasons=["미디어 파일 복사 완료를 기다립니다"])
                result["waiting_files"] += 1
                continue
            duration = float(probe(media))
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("미디어 재생 시간을 확인할 수 없습니다")
            item["duration_sec"] = duration
            reviews = [review_subtitle(media.parent / name, duration) for name in snapshot["subtitles"]]
            model_upgrade = key in reprocess_paths and item.get("completed_model") != model_id
            if any(review.status == "ok" for review in reviews) and not model_upgrade:
                item.update(status="completed" if item.get("status") == "completed" else "ok", reasons=[], error=None)
                result["ok_files"] += 1
                continue
            kind = "suspect" if reviews else "missing"
            result[f"{kind}_files"] += 1
            item.update(kind=kind, reasons=[str(reason) for review in reviews for reason in review.reasons]
                        or ["자막 파일이 없습니다"])
            if model_upgrade:
                item["reasons"] = [f"상위 모델 재전사 대상: {model_id or '새 모델'}"]
            item["original_text_chars"] = max(
                (review.metrics.get("text_chars", 0) for review in reviews
                 if not any(str(reason).startswith("severe_repetition:") for reason in review.reasons)),
                default=0,
            )
            if any(time.time() - (media.parent / name).stat().st_mtime < stable_seconds
                   for name in snapshot["subtitles"]):
                item.update(status="waiting", reasons=["기존 자막 파일 복사 완료를 기다립니다"])
                result["waiting_files"] += 1
                continue
            if item.get("attempts", 0) >= MAX_ATTEMPTS:
                item["status"] = "blocked"
                result["blocked_files"] += 1
                continue
            if item.get("next_retry_at", 0) > time.time():
                item["status"] = "failed"
                continue
            item["status"] = "pending"
            priority = 0 if kind == "suspect" else 1 if media.suffix.casefold() in AUDIO_EXTENSIONS else 2
            queue.append((priority, item.get("attempts", 0), key, media, snapshot, duration))
        except Exception as exc:
            item = state["items"].setdefault(key, {"media": key, "attempts": 0})
            item.update(status="waiting", error=str(exc), reasons=["자료 검토에 실패했습니다"])
            result["waiting_files"] += 1
    for key in set(state["items"]) - seen:
        state["items"][key]["status"] = "gone"
    queue.sort(key=lambda row: (row[0], row[1], row[5], row[2]))
    selected = queue[:limit]
    result["selected_files"] = len(selected)
    result["pending_files"] = len(queue) if dry_run else max(0, len(queue) - len(selected))
    _save(state_dir, state)
    if dry_run:
        result["items"] = [dict(state["items"][row[2]]) for row in queue]
        return result
    backend = transcriber
    for _, _, key, media, snapshot, duration in selected:
        item = state["items"][key]
        item.update(status="running", started_at=_stamp(), attempts=item.get("attempts", 0) + 1,
                    total_attempts=item.get("total_attempts", 0) + 1, next_retry_at=time.time() + RETRY_SECONDS,
                    asr_passes=0, error=None)
        _save(state_dir, state)
        say(f"전사 시작: {key} ({item['kind']}, 시도 {item['attempts']}/{MAX_ATTEMPTS})")
        try:
            if _snapshot(media) != snapshot:
                raise FileChanged("대기 중 미디어 또는 자막이 변경되었습니다")
            if backend is None:
                backend = FasterWhisperTranscriber(model=model, device=device, compute_type=compute_type,
                                                  cpu_threads=cpu_threads, model_cache=model_cache)
            for vad_filter in (True, False):
                item["asr_passes"] += 1
                item["vad_filter"] = vad_filter
                _save(state_dir, state)
                segments, detected = backend.transcribe(media, language=language, beam_size=beam_size,
                                                       vad_filter=vad_filter, hotwords=None)
                segments = list(segments)
                review = review_segments(segments, duration)
                original_chars = item.get("original_text_chars", 0)
                if original_chars and review.metrics.get("text_chars", 0) < original_chars * 0.5:
                    review.status = "suspect"
                    review.reasons.append(
                        f"substantial_text_loss: 기존 {original_chars}자의 절반 미만인 "
                        f"{review.metrics.get('text_chars', 0)}자로 감소했습니다"
                    )
                item["result_review"] = {"status": review.status, "reasons": list(review.reasons), "metrics": review.metrics}
                if review.status == "ok":
                    break
                say(f"검토 보류: {key}: {'; '.join(map(str, review.reasons))}")
            if review.status != "ok":
                raise RuntimeError("재전사 결과도 검토를 통과하지 못했습니다: " + "; ".join(map(str, review.reasons)))
            output, backups = _publish(media, segments, language or detected, snapshot, state_dir, key)
            item.update(status="completed", finished_at=_stamp(), subtitle=str(output.relative_to(root)),
                        language=language or detected, segments=len(segments), backups=backups, next_retry_at=0,
                        fingerprint=_fingerprint(_snapshot(media)), completed_model=model_id)
            result["completed_files"] += 1
            say(f"전사 완료: {key} ({len(segments)}개 구간)")
        except FileChanged as exc:
            item.update(status="changed", error=str(exc), next_retry_at=0, finished_at=_stamp())
            result["changed_files"] += 1
            say(f"원본 보존: {key}: {exc}")
        except Exception as exc:
            _failure(item, exc)
            result["failed_files"] += 1
            say(f"전사 실패: {key}: {exc}")
        finally:
            _save(state_dir, state)
        result["items"].append(dict(item))
    return result
