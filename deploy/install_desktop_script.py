#!/usr/bin/env python3
"""Install the bundled desktop transcriber into the active semester folder."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SCRIPT_NAME = "강의_자막_생성.py"
DEFAULT_SOURCE = Path("/app/desktop") / SCRIPT_NAME
DEFAULT_ARCHIVE_ROOT = Path("/archive")
KST = ZoneInfo("Asia/Seoul")


def current_semester(now: datetime | None = None) -> str:
    """Return the semester folder expected for the current Korean date."""
    current = now.astimezone(KST) if now is not None else datetime.now(KST)
    if current.month <= 2:
        return f"{current.year - 1}-2"
    if current.month <= 8:
        return f"{current.year}-1"
    return f"{current.year}-2"


def configured_destination(value: str | None) -> Path | None:
    """Return an explicitly configured absolute semester directory."""
    if value is None or not value.strip():
        return None
    destination = Path(value.strip())
    if not destination.is_absolute():
        raise ValueError(
            "YONSTUDY_DESKTOP_SCRIPT_DIR must be an absolute container path"
        )
    return destination


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_script(source: Path, destination: Path) -> tuple[Path, bool]:
    """Atomically copy source into destination and report whether it changed."""
    if not source.is_file():
        raise FileNotFoundError(f"bundled desktop script not found: {source}")
    if not destination.is_dir():
        raise NotADirectoryError(f"semester directory not found: {destination}")

    target = destination / SCRIPT_NAME
    source_hash = file_sha256(source)
    if target.is_file() and file_sha256(target) == source_hash:
        return target, False
    if target.exists() and not target.is_file():
        raise IsADirectoryError(f"desktop script target is not a file: {target}")

    temporary: Path | None = None
    try:
        with source.open("rb") as input_file, tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{SCRIPT_NAME}.",
            suffix=".tmp",
            dir=destination,
            delete=False,
        ) as output_file:
            temporary = Path(output_file.name)
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    if file_sha256(target) != source_hash:
        raise OSError(f"desktop script verification failed: {target}")
    return target, True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="현재 학기 폴더에 데스크톱 자막 생성기를 배포합니다."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configured_value = (
            str(args.destination)
            if args.destination is not None
            else os.environ.get("YONSTUDY_DESKTOP_SCRIPT_DIR")
        )
        explicit = configured_destination(configured_value)
        destination = explicit or args.archive_root / current_semester()
        if explicit is None and not destination.is_dir():
            print(f"현재 학기 폴더가 없어 데스크톱 스크립트 복사를 건너뜁니다: {destination}")
            return 0

        target, changed = install_script(args.source, destination)
    except (OSError, ValueError) as exc:
        print(f"데스크톱 자막 생성기 배포 실패: {exc}", file=sys.stderr)
        return 1

    action = "갱신" if changed else "확인"
    print(f"데스크톱 자막 생성기 {action}: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
