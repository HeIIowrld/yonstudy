"""기존 강좌 아카이브를 주차 폴더 없이 종류별 보관함으로 옮기는 계획."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .export import course_archive_root
from .flat_layout import MATERIAL_DIR, MEDIA_DIR

MATERIAL_EXTENSIONS = frozenset({
    ".pdf", ".ppt", ".pptx", ".doc", ".docx", ".hwp", ".hwpx", ".ipynb",
    ".xls", ".xlsx",
})
MEDIA_EXTENSIONS = frozenset({
    ".3gp", ".aac", ".flac", ".m4a", ".m4v", ".mkv", ".mov", ".mp3",
    ".mp4", ".mpeg", ".mpg", ".ogg", ".opus", ".wav", ".webm", ".wma", ".wmv",
    ".srt", ".vtt",
})
_ROOT_GENERATED = re.compile(
    r"(?:00_학습목차_자동생성|W\d{2}-00__주차학습_자동생성|"
    r"W99-00__주차미확인_자동생성|강좌내용요약_자동생성)\.md$"
)


@dataclass(frozen=True)
class LayoutMove:
    source: str
    target: str
    kind: str
    size: int


def plan_course_moves(course: dict, inventory: dict[str, int], *, include_media: bool = False) -> list[LayoutMove]:
    """과목 상대 파일 목록을 받아 충돌 검사 가능한 원격 이동 계획을 만든다.

    수동 작성 문서·과제·공지·프로젝트 폴더는 추측해 이동하지 않는다.
    """
    base = str(course_archive_root(course))
    prefix = base + "/"
    moves: list[LayoutMove] = []
    for full, size in sorted(inventory.items()):
        if not full.startswith(prefix):
            continue
        relative = full[len(prefix):]
        parts = PurePosixPath(relative).parts
        if not parts or any(part.startswith(".") for part in parts):
            continue
        suffix = PurePosixPath(relative).suffix.casefold()
        destination = None
        kind = ""
        if len(parts) == 1 and (suffix in MATERIAL_EXTENSIONS or "__강의자료__" in parts[0]):
            destination, kind = f"{MATERIAL_DIR}/{parts[0]}", "material"
        elif len(parts) > 2 and parts[0] == MATERIAL_DIR:
            # 오래된 주차별 강의자료 하위 폴더는 파일명에 경로 정보를 남기고 펼친다.
            heading = "__".join(parts[1:-1])
            destination, kind = f"{MATERIAL_DIR}/{heading}__{parts[-1]}", "material"
        elif include_media and len(parts) == 1 and suffix in MEDIA_EXTENSIONS:
            destination, kind = f"{MEDIA_DIR}/{parts[0]}", "media"
        elif len(parts) == 1 and _ROOT_GENERATED.fullmatch(parts[0]):
            destination = f".yonstudy-layout-backup/{PurePosixPath(base).name}/{parts[0]}"
            kind = "generated-backup"
        if destination and destination != relative:
            parent = PurePosixPath(base).parent if kind == "generated-backup" else PurePosixPath(base)
            moves.append(LayoutMove(full, str(parent / destination), kind, int(size)))
    return moves


def move_conflicts(moves: list[LayoutMove], inventory: dict[str, int]) -> list[LayoutMove]:
    """대상이 이미 있거나 둘 이상이 같은 대상으로 향하면 적용을 막는다."""
    from collections import Counter

    target_counts = Counter(move.target for move in moves)
    conflicts: list[LayoutMove] = []
    for move in moves:
        if move.target in inventory or target_counts[move.target] > 1:
            conflicts.append(move)
    return conflicts
