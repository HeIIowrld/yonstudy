"""폴더의 미디어를 찾아 faster-whisper 자막을 증분 생성한다.

DB나 LearnUs 로그인에 의존하지 않는다. NAS 마운트, OneDrive와 Google Drive
동기화 폴더, 일반 로컬 폴더를 같은 방식으로 스캔할 수 있게 파일의 존재 자체를
상태로 쓴다.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol


MEDIA_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".aac",
        ".flac",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
        ".wma",
        ".wmv",
    }
)
SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt"})


class TranscriptionDependencyError(RuntimeError):
    """선택 설치 ASR 패키지를 불러올 수 없을 때 발생한다."""


@dataclass(frozen=True)
class SubtitleSegment:
    start: float
    end: float
    text: str


class Transcriber(Protocol):
    def transcribe(
        self,
        media: Path,
        *,
        language: str | None,
        beam_size: int,
        vad_filter: bool,
        hotwords: str | None,
    ) -> tuple[list[SubtitleSegment], str | None]: ...


@dataclass(frozen=True)
class Candidate:
    media: Path
    subtitle: Path
    bytes: int


@dataclass
class ScanResult:
    root: Path
    media_files: int = 0
    existing_subtitles: int = 0
    unstable_files: int = 0
    empty_files: int = 0
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class TranscriptionResult:
    root: Path
    media_files: int = 0
    existing_subtitles: int = 0
    unstable_files: int = 0
    empty_files: int = 0
    selected_files: int = 0
    completed_files: int = 0
    skipped_during_run: int = 0
    changed_during_run: int = 0
    failed_files: int = 0
    segments: int = 0
    items: list[dict[str, object]] = field(default_factory=list)


def _media_files(
    root: Path,
    *,
    recursive: bool,
    extensions: frozenset[str],
) -> Iterable[Path]:
    if root.is_file():
        if root.suffix.casefold() in extensions:
            yield root
        return
    iterator = root.rglob("*") if recursive else root.glob("*")
    for path in iterator:
        try:
            is_file = path.is_file()
        except OSError:
            continue
        if is_file and path.suffix.casefold() in extensions:
            yield path


def target_subtitle(media: Path, language: str | None = "ko") -> Path:
    """`lecture.mp4`의 기본 산출물을 `lecture.ko.srt`로 정한다."""
    language = language.strip() if language else None
    if language and not all(ch.isalnum() or ch in "-_" for ch in language):
        raise ValueError(f"올바르지 않은 언어 코드입니다: {language}")
    tag = f".{language}" if language else ""
    return media.with_name(f"{media.stem}{tag}.srt")


def existing_subtitle(media: Path) -> Path | None:
    """같은 stem의 일반 또는 언어 태그 자막을 찾는다.

    `lecture.srt`, `lecture.ko.srt`, `lecture.en.vtt`를 모두 이미 있는 자막으로
    본다. 특정 언어 자막을 다시 만들 때는 상위 명령의 `force` 옵션을
    사용한다.
    """
    stem = media.stem.casefold()
    prefix = f"{stem}."
    try:
        siblings = media.parent.iterdir()
    except OSError:
        return None
    for path in siblings:
        name = path.name.casefold()
        if path.suffix.casefold() not in SUBTITLE_EXTENSIONS:
            continue
        if name == f"{stem}{path.suffix.casefold()}" or name.startswith(prefix):
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return path
            except OSError:
                continue
    return None


def scan_directory(
    root: str | Path,
    *,
    language: str | None = "ko",
    recursive: bool = True,
    stable_seconds: int = 120,
    force: bool = False,
    limit: int | None = None,
    extensions: frozenset[str] = MEDIA_EXTENSIONS,
    now: float | None = None,
) -> ScanResult:
    """전사할 파일을 고른다.

    오래 걸리는 모델은 여기서 불러오지 않는다.
    """
    root = Path(root).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"대상 경로가 없습니다: {root}")
    if stable_seconds < 0:
        raise ValueError("stable_seconds는 0 이상이어야 합니다")
    if limit is not None and limit < 1:
        raise ValueError("limit은 1 이상이어야 합니다")

    result = ScanResult(root=root)
    current_time = time.time() if now is None else now
    paths = sorted(
        _media_files(root, recursive=recursive, extensions=extensions),
        key=lambda path: str(path).casefold(),
    )
    result.media_files = len(paths)
    for media in paths:
        if not force and existing_subtitle(media) is not None:
            result.existing_subtitles += 1
            continue
        try:
            stat = media.stat()
        except OSError:
            result.unstable_files += 1
            continue
        if stat.st_size <= 0:
            result.empty_files += 1
            continue
        if current_time - stat.st_mtime < stable_seconds:
            result.unstable_files += 1
            continue
        result.candidates.append(
            Candidate(
                media=media,
                subtitle=target_subtitle(media, language),
                bytes=stat.st_size,
            )
        )
    if limit is not None:
        result.candidates = result.candidates[:limit]
    return result


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


class FasterWhisperTranscriber:
    """faster-whisper 호출을 감싸는 어댑터."""

    def __init__(
        self,
        *,
        model: str = "small",
        device: str = "auto",
        compute_type: str = "auto",
        cpu_threads: int = 0,
        model_cache: str | Path | None = None,
    ):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionDependencyError(
                "faster-whisper가 필요합니다: python -m pip install faster-whisper"
            ) from exc

        selected_device = _resolve_device(device)
        selected_compute = compute_type
        if compute_type == "auto":
            selected_compute = "float16" if selected_device == "cuda" else "int8"
        kwargs: dict[str, object] = {
            "device": selected_device,
            "compute_type": selected_compute,
        }
        if cpu_threads > 0:
            kwargs["cpu_threads"] = cpu_threads
        if model_cache:
            cache = Path(model_cache).expanduser()
            cache.mkdir(parents=True, exist_ok=True)
            kwargs["download_root"] = str(cache)
        self.model = WhisperModel(model, **kwargs)
        self.model_name = model
        self.device = selected_device
        self.compute_type = selected_compute

    def transcribe(
        self,
        media: Path,
        *,
        language: str | None,
        beam_size: int,
        vad_filter: bool,
        hotwords: str | None,
    ) -> tuple[list[SubtitleSegment], str | None]:
        kwargs: dict[str, object] = {
            "language": language,
            "beam_size": beam_size,
            "vad_filter": vad_filter,
        }
        if hotwords:
            kwargs["hotwords"] = hotwords
        raw_segments, info = self.model.transcribe(str(media), **kwargs)
        segments = [
            SubtitleSegment(float(row.start), float(row.end), row.text.strip())
            for row in raw_segments
            if row.text and row.text.strip()
        ]
        return segments, getattr(info, "language", language)


def _srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def render_srt(segments: Iterable[SubtitleSegment]) -> str:
    cues: list[str] = []
    for segment in segments:
        text = segment.text.strip().replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            continue
        index = len(cues) + 1
        cues.append(
            f"{index}\n{_srt_timestamp(segment.start)} --> "
            f"{_srt_timestamp(max(segment.start, segment.end))}\n{text}"
        )
    return "\n\n".join(cues) + ("\n" if cues else "")


def read_hotwords(path: str | Path | None) -> str | None:
    if not path:
        return None
    source = Path(path).expanduser()
    text = source.read_text(encoding="utf-8")
    words = [line.strip() for line in text.splitlines() if line.strip()]
    return ", ".join(words) or None


def transcribe_directory(
    root: str | Path,
    *,
    model: str = "small",
    device: str = "auto",
    compute_type: str = "auto",
    language: str | None = "ko",
    beam_size: int = 1,
    vad_filter: bool = True,
    hotwords_file: str | Path | None = None,
    model_cache: str | Path | None = None,
    cpu_threads: int = 0,
    recursive: bool = True,
    stable_seconds: int = 120,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    transcriber: Transcriber | None = None,
    say: Callable[[str], None] | None = None,
) -> TranscriptionResult:
    """폴더를 한 번 스캔해 빠진 자막을 만든다.

    산출물은 임시 파일에 쓴 뒤 같은 디렉터리에서 이름을 바꾼다. 전사하는 동안
    원본의 크기나 수정 시각이 달라지면 임시 자막을 버려 동기화 중인 파일을
    확정하지 않는다.
    """
    if beam_size < 1:
        raise ValueError("beam_size는 1 이상이어야 합니다")
    log = say or (lambda _message: None)
    scan = scan_directory(
        root,
        language=language,
        recursive=recursive,
        stable_seconds=stable_seconds,
        force=force,
        limit=limit,
    )
    result = TranscriptionResult(
        root=scan.root,
        media_files=scan.media_files,
        existing_subtitles=scan.existing_subtitles,
        unstable_files=scan.unstable_files,
        empty_files=scan.empty_files,
        selected_files=len(scan.candidates),
    )
    if dry_run or not scan.candidates:
        for candidate in scan.candidates:
            result.items.append(
                {
                    "media": str(candidate.media),
                    "subtitle": str(candidate.subtitle),
                    "status": "dry-run" if dry_run else "pending",
                }
            )
        return result

    backend = transcriber or FasterWhisperTranscriber(
        model=model,
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        model_cache=model_cache,
    )
    hotwords = read_hotwords(hotwords_file)

    for index, candidate in enumerate(scan.candidates, 1):
        media = candidate.media
        output = candidate.subtitle
        temporary = output.with_name(f".{output.name}.part-{os.getpid()}")
        item: dict[str, object] = {
            "media": str(media),
            "subtitle": str(output),
        }
        log(f"[{index}/{len(scan.candidates)}] {media}")
        try:
            before = media.stat()
            segments, detected_language = backend.transcribe(
                media,
                language=language,
                beam_size=beam_size,
                vad_filter=vad_filter,
                hotwords=hotwords,
            )
            if not segments:
                raise RuntimeError("음성 구간을 찾지 못했습니다")
            if language is None and detected_language:
                output = target_subtitle(media, detected_language)
                temporary = output.with_name(f".{output.name}.part-{os.getpid()}")
                item["subtitle"] = str(output)
            temporary.write_text(render_srt(segments), encoding="utf-8", newline="\n")
            after = media.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                temporary.unlink(missing_ok=True)
                result.changed_during_run += 1
                item.update(status="source-changed")
                log(
                    "    원본이 전사 중 변경되어 다음 실행에서 다시 시도합니다."
                )
            elif not force and existing_subtitle(media) is not None:
                temporary.unlink(missing_ok=True)
                result.skipped_during_run += 1
                item.update(status="subtitle-appeared")
                log(
                    "    실행 중 다른 자막이 생겨 결과를 덮어쓰지 않았습니다."
                )
            else:
                temporary.replace(output)
                result.completed_files += 1
                result.segments += len(segments)
                item.update(
                    status="completed",
                    language=detected_language,
                    segments=len(segments),
                )
                log(f"    → {output.name} ({len(segments)}개 구간)")
        except KeyboardInterrupt:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            result.failed_files += 1
            item.update(status="failed", error=str(exc))
            log(f"    실패: {exc}")
        result.items.append(item)
    return result
