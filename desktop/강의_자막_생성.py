#!/usr/bin/env python3
"""이 파일이 있는 학기 폴더에서 누락된 강의 자막을 만든다.

Windows x64 데스크톱에서 파일을 더블클릭하는 사용 방식을 기준으로 한다. 음성
인식은 데스크톱에서 실행하고 NAS에는 완성된 SRT만 쓴다. 처음 실행할 때 런타임과
모델을 사용자 AppData에 내려받으며 CUDA, Vulkan, CPU 순서로 실행을 시도한다.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# medium 구조를 유지하면서 범용 GPU의 메모리 부담을 낮춘 공식 양자화 모델이다.
MODEL = "medium-q5_0"
LANGUAGE = "auto"
BEAM_SIZE = 5
STABLE_SECONDS = 120
IMAGEIO_FFMPEG_VERSION = "0.6.0"
WHISPER_CPP_VERSION = "1.8.7"
RUNTIME_RELEASE = "desktop-runtime-v1.8.7-1"

MODEL_FILE = "ggml-medium-q5_0.bin"
MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
    f"{MODEL_FILE}"
)
MODEL_SHA256 = "19fea4b380c3a618ec4723c3eef2eb785ffba0d0538cf43f8f235e7b3b34220f"
MODEL_BYTES = 539_212_467

VAD_FILE = "ggml-silero-v6.2.0.bin"
VAD_URL = f"https://huggingface.co/ggml-org/whisper-vad/resolve/main/{VAD_FILE}"
VAD_SHA256 = "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"
VAD_BYTES = 885_098

CUDA_ARCHIVE = "whisper-cublas-11.8.0-bin-x64.zip"
CUDA_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/"
    f"v{WHISPER_CPP_VERSION}/{CUDA_ARCHIVE}"
)
CUDA_SHA256 = "e9193626af0a29e6102212522127333050997f2d34cc7122c6a13cfef66144f2"

CPU_ARCHIVE = "whisper-bin-x64.zip"
CPU_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/"
    f"v{WHISPER_CPP_VERSION}/{CPU_ARCHIVE}"
)
CPU_SHA256 = "d9627486e1c34a03745880485593473e047294260ce9a3cb0aa8deaf15b99af6"

VULKAN_ARCHIVE = "yonstudy-whisper-vulkan-v1.8.7-win-x64.zip"
VULKAN_BASE_URL = (
    "https://github.com/HeIIowrld/yonstudy/releases/download/"
    f"{RUNTIME_RELEASE}"
)
VULKAN_URL = f"{VULKAN_BASE_URL}/{VULKAN_ARCHIVE}"
VULKAN_CHECKSUM_URL = f"{VULKAN_URL}.sha256"
# 릴리스 생성 뒤 고정한다. 비어 있으면 같은 릴리스의 체크섬 파일을 먼저 읽는다.
VULKAN_SHA256 = ""

MEDIA_EXTENSIONS = {
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
SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
BACKEND_LABELS = {"cuda": "CUDA", "vulkan": "Vulkan", "cpu": "CPU"}


@dataclass(frozen=True)
class Candidate:
    media: Path
    bytes: int


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class ArchiveSpec:
    name: str
    filename: str
    url: str
    sha256: str


@dataclass(frozen=True)
class Backend:
    name: str
    executable: Path
    no_gpu: bool = False


class BackendError(RuntimeError):
    """선택한 whisper.cpp 백엔드를 초기화하거나 실행하지 못했다."""


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "yonstudy-transcriber"


def runtime_python(runtime: Path) -> Path:
    if sys.platform == "win32":
        return runtime / "venv" / "Scripts" / "python.exe"
    return runtime / "venv" / "bin" / "python"


def media_decoder_available() -> bool:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.__version__ == IMAGEIO_FFMPEG_VERSION
    except ImportError:
        return False


def bootstrap_python_runtime(args: list[str]) -> int:
    """영상 디코더가 없으면 사용자 전용 venv를 만들고 다시 실행한다."""
    runtime = app_data_dir()
    python = runtime_python(runtime)
    if not python.is_file():
        print("처음 실행입니다. 사용자 전용 미디어 환경을 준비합니다.", flush=True)
        runtime.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True).create(runtime / "venv")
    check = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import imageio_ffmpeg; "
                f"assert imageio_ffmpeg.__version__ == '{IMAGEIO_FFMPEG_VERSION}'"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if check.returncode != 0:
        print("영상 음성을 읽을 FFmpeg 번들을 설치합니다.", flush=True)
        subprocess.check_call(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"imageio-ffmpeg=={IMAGEIO_FFMPEG_VERSION}",
            ]
        )
    command = [str(python), str(Path(__file__).resolve()), *args, "--_runtime"]
    return subprocess.call(command)


def existing_subtitle(media: Path) -> Path | None:
    stem = media.stem.casefold()
    prefix = f"{stem}."
    try:
        siblings = media.parent.iterdir()
    except OSError:
        return None
    for path in siblings:
        name = path.name.casefold()
        suffix = path.suffix.casefold()
        if suffix not in SUBTITLE_EXTENSIONS:
            continue
        if name != f"{stem}{suffix}" and not name.startswith(prefix):
            continue
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def scan(root: Path, *, force: bool, stable_seconds: int) -> tuple[list[Candidate], dict]:
    now = time.time()
    result = {"media": 0, "existing": 0, "waiting": 0, "empty": 0}
    candidates: list[Candidate] = []
    paths = []
    for path in root.rglob("*"):
        try:
            if path.is_file() and path.suffix.casefold() in MEDIA_EXTENSIONS:
                paths.append(path)
        except OSError:
            continue
    paths.sort(key=lambda path: str(path).casefold())
    result["media"] = len(paths)
    for media in paths:
        if not force and existing_subtitle(media):
            result["existing"] += 1
            continue
        try:
            stat = media.stat()
        except OSError:
            result["waiting"] += 1
            continue
        if stat.st_size <= 0:
            result["empty"] += 1
        elif now - stat.st_mtime < stable_seconds:
            result["waiting"] += 1
        else:
            candidates.append(Candidate(media, stat.st_size))
    return candidates, result


def _driver_library_available(name: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        ctypes.WinDLL(name)
        return True
    except (AttributeError, OSError):
        return False


def nvidia_driver_available() -> bool:
    return _driver_library_available("nvcuda.dll")


def vulkan_loader_available() -> bool:
    return _driver_library_available("vulkan-1.dll")


def backend_order(
    requested: str,
    *,
    has_nvidia: bool | None = None,
    has_vulkan: bool | None = None,
) -> list[str]:
    if requested != "auto":
        return [requested]
    if has_nvidia is None:
        has_nvidia = nvidia_driver_available()
    if has_vulkan is None:
        has_vulkan = vulkan_loader_available()
    result = []
    if has_nvidia:
        result.append("cuda")
    if has_vulkan:
        result.append("vulkan")
    result.append("cpu")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_checksum(value: str) -> str:
    value = value.strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("올바르지 않은 SHA-256 체크섬입니다.")
    return value


def fetch_checksum(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "yonstudy-transcriber"})
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read(1024).decode("ascii", errors="strict")
    return _valid_checksum(text.split()[0])


def download(
    url: str,
    destination: Path,
    *,
    sha256: str,
    expected_bytes: int | None = None,
) -> Path:
    expected_hash = _valid_checksum(sha256)
    marker = destination.with_suffix(f"{destination.suffix}.sha256")
    if destination.is_file() and marker.is_file():
        if marker.read_text(encoding="ascii").strip() == expected_hash:
            if expected_bytes is None or destination.stat().st_size == expected_bytes:
                return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    print(f"다운로드: {destination.name}", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "yonstudy-transcriber"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as out:
            total = int(response.headers.get("Content-Length", "0") or 0)
            received = 0
            last_report = 0.0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last_report >= 1.0:
                    if total:
                        print(f"    {received / total:6.1%}", end="\r", flush=True)
                    else:
                        print(f"    {received / 1024 / 1024:,.0f} MB", end="\r", flush=True)
                    last_report = now
        print(" " * 30, end="\r", flush=True)
        if expected_bytes is not None and temporary.stat().st_size != expected_bytes:
            raise RuntimeError(
                f"다운로드 크기가 다릅니다: {temporary.stat().st_size} != {expected_bytes}"
            )
        actual_hash = _sha256(temporary)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"다운로드 체크섬이 다릅니다: {actual_hash} != {expected_hash}"
            )
        temporary.replace(destination)
        marker.write_text(expected_hash + "\n", encoding="ascii")
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = (destination / member.filename).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise RuntimeError(f"압축 파일에 위험한 경로가 있습니다: {member.filename}")
        source.extractall(destination)


def install_runtime(runtime: Path, spec: ArchiveSpec) -> Path:
    target = runtime / "runtimes" / spec.name
    marker = target / ".archive.sha256"
    executables = sorted(target.rglob("whisper-cli.exe")) if target.is_dir() else []
    if executables and marker.is_file():
        if marker.read_text(encoding="ascii").strip() == spec.sha256:
            return executables[0]

    archive = download(
        spec.url,
        runtime / "downloads" / spec.filename,
        sha256=spec.sha256,
    )
    staging = runtime / "runtimes" / f".{spec.name}.installing-{os.getpid()}"
    ready = runtime / "runtimes" / f".{spec.name}.ready-{os.getpid()}"
    for temporary in (staging, ready):
        if temporary.exists():
            shutil.rmtree(temporary)
    try:
        staging.mkdir(parents=True)
        _safe_extract(archive, staging)
        candidates = sorted(
            staging.rglob("whisper-cli.exe"), key=lambda path: len(path.parts)
        )
        if not candidates:
            raise RuntimeError(f"{spec.filename}에 whisper-cli.exe가 없습니다.")
        shutil.copytree(candidates[0].parent, ready)
        (ready / ".archive.sha256").write_text(spec.sha256 + "\n", encoding="ascii")
        if target.exists():
            shutil.rmtree(target)
        ready.replace(target)
        return target / "whisper-cli.exe"
    finally:
        for temporary in (staging, ready):
            if temporary.exists():
                shutil.rmtree(temporary)


def archive_spec(name: str) -> ArchiveSpec:
    if name == "cuda":
        return ArchiveSpec("cuda-1.8.7", CUDA_ARCHIVE, CUDA_URL, CUDA_SHA256)
    if name == "vulkan":
        checksum = VULKAN_SHA256 or fetch_checksum(VULKAN_CHECKSUM_URL)
        return ArchiveSpec("vulkan-1.8.7-1", VULKAN_ARCHIVE, VULKAN_URL, checksum)
    if name == "cpu":
        return ArchiveSpec("cpu-1.8.7", CPU_ARCHIVE, CPU_URL, CPU_SHA256)
    raise ValueError(f"알 수 없는 백엔드입니다: {name}")


def prepare_backend(runtime: Path, name: str) -> Backend:
    executable = install_runtime(runtime, archive_spec(name))
    return Backend(name=name, executable=executable, no_gpu=name == "cpu")


def ensure_models(runtime: Path) -> tuple[Path, Path]:
    model_dir = runtime / "models"
    model = download(
        MODEL_URL,
        model_dir / MODEL_FILE,
        sha256=MODEL_SHA256,
        expected_bytes=MODEL_BYTES,
    )
    vad = download(
        VAD_URL,
        model_dir / VAD_FILE,
        sha256=VAD_SHA256,
        expected_bytes=VAD_BYTES,
    )
    return model, vad


def convert_to_wav(media: Path, output: Path) -> None:
    from imageio_ffmpeg import get_ffmpeg_exe

    command = [
        get_ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "알 수 없는 FFmpeg 오류").strip()
        raise RuntimeError(f"영상에서 음성을 추출하지 못했습니다: {detail[-1200:]}")


def _language_code(value: object) -> str:
    language = str(value or "und").casefold()
    return "".join(ch for ch in language if ch.isalnum() or ch in "-_") or "und"


def parse_whisper_json(path: Path) -> tuple[list[Cue], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    language = _language_code((payload.get("result") or {}).get("language"))
    cues = []
    for row in payload.get("transcription") or []:
        offsets = row.get("offsets") or {}
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        cues.append(
            Cue(
                float(offsets.get("from", 0)) / 1000.0,
                float(offsets.get("to", 0)) / 1000.0,
                text,
            )
        )
    return cues, language


def transcribe(
    backend: Backend,
    model: Path,
    vad_model: Path,
    wav: Path,
    output_base: Path,
) -> tuple[list[Cue], str, float]:
    threads = max(1, min(8, (os.cpu_count() or 4) - 1))
    command = [
        str(backend.executable),
        "-m",
        str(model),
        "-f",
        str(wav),
        "-l",
        LANGUAGE,
        "-bs",
        str(BEAM_SIZE),
        "-t",
        str(threads),
        "--vad",
        "--vad-model",
        str(vad_model),
        "-oj",
        "-of",
        str(output_base),
        "-pp",
    ]
    if backend.no_gpu:
        command.append("--no-gpu")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log_lines = []
    assert process.stdout is not None
    for line in process.stdout:
        log_lines.append(line)
        stripped = line.strip()
        if stripped and (
            "%" in stripped
            or "auto-detected language" in stripped
            or "using " in stripped.casefold()
        ):
            print(f"    {stripped}", flush=True)
    return_code = process.wait()
    log = "".join(log_lines)
    if return_code != 0:
        detail = "\n".join(line.strip() for line in log_lines[-20:] if line.strip())
        raise BackendError(
            f"{BACKEND_LABELS[backend.name]} 실행이 종료 코드 {return_code}로 실패했습니다. "
            f"{detail[-1200:]}"
        )
    if backend.name in {"cuda", "vulkan"}:
        expected = f"using {backend.name}"
        if expected not in log.casefold():
            raise BackendError(
                f"{BACKEND_LABELS[backend.name]} 장치를 초기화하지 못했습니다."
            )

    json_path = output_base.with_suffix(".json")
    if not json_path.is_file():
        raise BackendError("whisper.cpp 결과 JSON이 만들어지지 않았습니다.")
    cues, language = parse_whisper_json(json_path)
    match = re.search(
        r"auto-detected language:\s*[^\s]+\s*\(p\s*=\s*([0-9.]+)\)", log
    )
    probability = float(match.group(1)) if match else 0.0
    return cues, language, probability


def timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def render_srt(cues: Iterable[Cue]) -> str:
    blocks = []
    for cue in cues:
        text = cue.text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n{timestamp(cue.start)} --> "
            f"{timestamp(max(cue.start, cue.end))}\n{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_subtitle(
    media: Path,
    cues: list[Cue],
    language: str,
    before: os.stat_result,
    *,
    force: bool,
) -> Path | None:
    output = media.with_name(f"{media.stem}.{language}.srt")
    temporary = output.with_name(f".{output.name}.part-{os.getpid()}")
    temporary.write_text(render_srt(cues), encoding="utf-8", newline="\n")
    try:
        after = media.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            print("    원본이 실행 중 변경되어 다음 실행에서 다시 시도합니다.")
            return None
        if not force and existing_subtitle(media):
            print("    실행 중 다른 자막이 생겨 덮어쓰지 않았습니다.")
            return None
        temporary.replace(output)
        return output
    finally:
        temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(add_help=True)
    value.add_argument("--dry-run", action="store_true")
    value.add_argument("--force", action="store_true")
    value.add_argument("--limit", type=int)
    value.add_argument("--stable-seconds", type=int, default=STABLE_SECONDS)
    value.add_argument(
        "--backend", choices=("auto", "cuda", "vulkan", "cpu"), default="auto"
    )
    value.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("--_runtime", action="store_true", help=argparse.SUPPRESS)
    return value


def _check_platform() -> None:
    machine = platform.machine().casefold()
    if sys.platform != "win32" or machine not in {"amd64", "x86_64"}:
        raise RuntimeError(
            "이 단일 파일은 Windows x64 데스크톱용입니다. "
            "Linux에서는 저장소의 `python cli.py transcribe`를 사용하세요."
        )


def run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent
    candidates, counts = scan(
        root,
        force=args.force,
        stable_seconds=max(0, args.stable_seconds),
    )
    if args.limit is not None:
        candidates = candidates[: max(0, args.limit)]

    print(f"학기 폴더: {root}")
    print(f"모델: {MODEL} (medium 양자화) · 언어: 강의별 자동 감지")
    print(
        f"미디어 {counts['media']}개 · 기존 자막 {counts['existing']}개 · "
        f"복사 대기 {counts['waiting']}개 · 처리 대상 {len(candidates)}개"
    )
    if args.dry_run:
        for row in candidates:
            print(f"  {row.media.relative_to(root)}")
        return 0
    if not candidates:
        print("새로 만들 자막이 없습니다.")
        return 0

    _check_platform()
    if not media_decoder_available():
        if args._runtime:
            raise RuntimeError("전용 환경에 FFmpeg 번들을 설치하지 못했습니다.")
        forwarded = [arg for arg in sys.argv[1:] if arg not in {"--pause", "--_runtime"}]
        return bootstrap_python_runtime(forwarded)

    runtime = app_data_dir()
    model, vad_model = ensure_models(runtime)
    names = backend_order(args.backend)
    print("음성 인식 순서: " + " → ".join(BACKEND_LABELS[name] for name in names))
    backend_index = 0
    backend: Backend | None = None
    completed = failed = 0

    for index, candidate in enumerate(candidates, 1):
        relative = candidate.media.relative_to(root)
        print(f"\n[{index}/{len(candidates)}] {relative}")
        try:
            before = candidate.media.stat()
            with tempfile.TemporaryDirectory(prefix="yonstudy-whisper-") as temp_name:
                temporary = Path(temp_name)
                wav = temporary / "audio.wav"
                output_base = temporary / "result"
                print("    음성을 추출합니다.")
                convert_to_wav(candidate.media, wav)

                while backend_index < len(names):
                    name = names[backend_index]
                    try:
                        if backend is None or backend.name != name:
                            print(f"    {BACKEND_LABELS[name]} 런타임을 준비합니다.")
                            backend = prepare_backend(runtime, name)
                        cues, language, probability = transcribe(
                            backend, model, vad_model, wav, output_base
                        )
                        break
                    except (BackendError, OSError, RuntimeError) as exc:
                        if backend_index + 1 >= len(names):
                            raise
                        next_name = names[backend_index + 1]
                        print(f"    {BACKEND_LABELS[name]} 실패: {exc}")
                        print(f"    {BACKEND_LABELS[next_name]}로 전환해 다시 시도합니다.")
                        backend_index += 1
                        backend = None
                        output_base.with_suffix(".json").unlink(missing_ok=True)
                else:
                    raise RuntimeError("사용할 수 있는 음성 인식 백엔드가 없습니다.")

            if not cues:
                raise RuntimeError("음성 구간을 찾지 못했습니다.")
            output = write_subtitle(
                candidate.media,
                cues,
                language,
                before,
                force=args.force,
            )
            if output:
                completed += 1
                probability_text = f" ({probability:.0%})" if probability else ""
                print(
                    f"    완료: {output.name} · {len(cues)}개 구간 · "
                    f"언어 {language}{probability_text} · {BACKEND_LABELS[backend.name]}"
                )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            failed += 1
            print(f"    실패: {exc}")

    print(f"\n전체 완료: 성공 {completed}개 · 실패 {failed}개")
    return 1 if failed else 0


def main() -> int:
    double_clicked = len(sys.argv) == 1
    args = parser().parse_args()
    args.pause = args.pause or double_clicked
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n사용자가 중단했습니다.")
        return 130
    except Exception as exc:
        print(f"\n실행 실패: {exc}")
        return 1
    finally:
        if args.pause:
            try:
                input("\n창을 닫으려면 Enter 키를 누르세요...")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
