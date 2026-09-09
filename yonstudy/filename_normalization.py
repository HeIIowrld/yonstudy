"""macOS 분해형 파일명을 Windows 호환 NFC 이름으로 정규화한다."""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


def nfc(value: str) -> str:
    """한글 자모를 포함한 유니코드 문자열을 NFC 조합형으로 반환한다."""
    return unicodedata.normalize("NFC", value)


@dataclass(frozen=True)
class RenameFailure:
    source: str
    target: str
    reason: str


@dataclass(frozen=True)
class RenameChange:
    source: str
    target: str


@dataclass
class NormalizeResult:
    root: str
    dry_run: bool
    scanned: int = 0
    renamed: int = 0
    unchanged: int = 0
    changes: list[RenameChange] = field(default_factory=list)
    failures: list[RenameFailure] = field(default_factory=list)


def normalize_tree(root: str | Path, *, dry_run: bool = False) -> NormalizeResult:
    """하위 파일·폴더 이름을 NFC로 바꾼다.

    폴더 이름을 마지막에 바꿔 하위 경로가 사라지지 않게 하고, 대상 이름이
    이미 존재하면 데이터를 덮어쓰지 않는다. 심볼릭 링크는 따라가지 않지만
    링크 자체의 이름은 정규화한다.
    """
    root = Path(root)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"폴더를 찾을 수 없습니다: {root}")

    result = NormalizeResult(root=str(root), dry_run=dry_run)
    entries = sorted(
        root.rglob("*"), key=lambda path: len(path.parts), reverse=True
    )
    for source in entries:
        result.scanned += 1
        normalized = nfc(source.name)
        if normalized == source.name:
            result.unchanged += 1
            continue

        target = source.with_name(normalized)
        if os.path.lexists(target):
            result.failures.append(
                RenameFailure(str(source), str(target), "대상 이름이 이미 존재합니다")
            )
            continue
        if not dry_run:
            try:
                source.rename(target)
            except OSError as exc:
                result.failures.append(
                    RenameFailure(str(source), str(target), str(exc))
                )
                continue
        result.renamed += 1
        result.changes.append(RenameChange(str(source), str(target)))
    return result
