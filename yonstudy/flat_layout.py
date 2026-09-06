"""DB 메타데이터로 평면형 아카이브 경로를 계산한다."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .export import term_folder


MAX_FILENAME_BYTES = 150


def _clean(value: str | None, fallback: str = "이름없음") -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value or "")
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value or fallback


def _truncate_utf8(value: str, size: int) -> str:
    return value.encode("utf-8")[: max(0, size)].decode("utf-8", "ignore").rstrip(" ._")


def week_number(section_idx: int | None, section_name: str | None, title: str | None) -> int:
    """명시적 `N주차`를 우선하고, 없을 때만 LearnUs 섹션 번호를 사용한다."""
    for value in (section_name, title):
        match = re.search(r"(?:week\s*|w\s*|)(\d{1,2})\s*주차", value or "", re.I)
        if not match:
            match = re.search(r"\bweek\s*(\d{1,2})\b", value or "", re.I)
        if match:
            return int(match.group(1))
    return max(0, int(section_idx or 0))


def lesson_number(title: str | None) -> int:
    value = title or ""
    patterns = (
        r"\bweek\s*\d{1,2}\s*[-_.]\s*(\d{1,2})\b",
        r"\blecture\s*\d{1,2}\s*[-_.]\s*(\d{1,2})\b",
        r"\b\d{1,2}\s*주차\D{0,8}(\d{1,2})\s*차시\b",
        r"^\s*\d{1,2}\s*[-_]\s*(\d{1,2})\b",
        r"\blecture\s*0*(\d{1,2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            return int(match.group(1))
    return 0


def canonical_filename(
    *,
    week: int,
    lesson: int,
    kind: str,
    title: str,
    stable_id: str,
    extension: str = "",
) -> str:
    """Windows에서도 정렬되고 이름이 겹치지 않는 파일명을 만든다."""
    suffix = re.sub(r"[^A-Za-z0-9]", "", extension.lstrip("."))[:15]
    ext = f".{suffix}" if suffix else ""
    head = f"W{week:02d}-L{lesson:02d}__{_clean(kind)}__"
    tail = f"__{_clean(stable_id)}{ext}"
    budget = MAX_FILENAME_BYTES - len((head + tail).encode("utf-8"))
    middle = _truncate_utf8(_clean(title), budget) or "이름없음"
    return f"{head}{middle}{tail}"


def resource_filename(
    *,
    section_idx: int | None,
    section_name: str | None,
    activity_title: str | None,
    name: str,
    file_id: int,
    open_from: str | None = None,
    saved_at: str | None = None,
) -> str:
    """과목 루트에 둘 강의자료의 정렬 가능한 파일명을 만든다.

    주차 정보가 있으면 ``W01-L02``를, 없으면 공개일/수집일 ``YYYYMMDD``를
    접두어로 쓴다. 종류와 고정 ID도 남겨 다른 루트 파일과 이름이 겹치지 않는다.
    """
    original = Path(name or f"파일_{file_id}")
    title_hint = " ".join(filter(None, (activity_title, original.stem)))
    week = week_number(section_idx, section_name, title_hint)
    lesson = lesson_number(title_hint)
    if week:
        return canonical_filename(
            week=week,
            lesson=lesson,
            kind="강의자료",
            title=original.stem,
            stable_id=f"f{file_id}",
            extension=original.suffix,
        )

    day = re.sub(r"\D", "", ((open_from or saved_at) or "")[:10]) or "날짜없음"
    suffix = re.sub(r"[^A-Za-z0-9]", "", original.suffix.lstrip("."))[:15]
    ext = f".{suffix}" if suffix else ""
    head = f"{day}__강의자료__"
    tail = f"__f{file_id}{ext}"
    budget = MAX_FILENAME_BYTES - len((head + tail).encode("utf-8"))
    middle = _truncate_utf8(_clean(original.stem), budget) or "이름없음"
    return f"{head}{middle}{tail}"


def _post_kind(board: str | None) -> str:
    value = (board or "").lower()
    if "공지" in value or "announcement" in value:
        return "공지"
    if "질의" in value or "q&a" in value or "qna" in value:
        return "질의응답"
    return "게시글"


@dataclass(frozen=True)
class FlatEntry:
    course_id: int
    cmid: int
    kind: str
    week: int
    lesson: int
    relative_path: str
    source_ready: bool
    bytes: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def build_flat_plan(store, *, destination: str | Path, year: str, semester: str) -> list[FlatEntry]:
    """복사/다운로드 없이 자료·게시글·VOD의 목표 경로만 반환한다."""
    destination = Path(destination)
    entries: list[FlatEntry] = []
    courses = store.query(
        "SELECT course_id,slug,title,name FROM course WHERE year=? AND semester=? ORDER BY name",
        (year, semester),
    )
    term = term_folder(year, semester)
    for course in courses:
        course_name = _clean(course["slug"] or course["title"] or course["name"])
        base = destination / term / course_name

        files = store.query(
            """
            SELECT f.id,f.cmid,f.role,f.name,f.sha256,f.bytes,
                   a.title AS activity_title,a.section_idx,a.section_name
              FROM file f LEFT JOIN activity a ON a.cmid=f.cmid
             WHERE f.course_id=? AND f.role IN ('resource','post')
             ORDER BY f.id
            """,
            (course["course_id"],),
        )
        for row in files:
            original = Path(row["name"] or f"파일_{row['id']}")
            week = week_number(row["section_idx"], row["section_name"], row["activity_title"])
            lesson = lesson_number(row["activity_title"] or original.stem)
            kind = "강의자료" if row["role"] == "resource" else "게시판첨부"
            if row["role"] == "resource":
                name = resource_filename(
                    section_idx=row["section_idx"], section_name=row["section_name"],
                    activity_title=row["activity_title"], name=str(original),
                    file_id=row["id"],
                )
            else:
                name = canonical_filename(
                    week=week,
                    lesson=lesson,
                    kind=kind,
                    title=original.stem,
                    stable_id=f"f{row['id']}",
                    extension=original.suffix,
                )
            blob = store.blob_path(row["sha256"]) if row["sha256"] else None
            entries.append(
                FlatEntry(
                    course_id=course["course_id"], cmid=row["cmid"] or 0,
                    kind=kind, week=week, lesson=lesson,
                    relative_path=str((base / name).relative_to(destination)),
                    source_ready=bool(blob and blob.is_file()), bytes=row["bytes"],
                )
            )

        posts = store.query(
            """
            SELECT p.post_id,p.cmid,p.subject,p.written_at,
                   a.title AS board_title,a.section_idx,a.section_name
              FROM post p LEFT JOIN activity a ON a.cmid=p.cmid
             WHERE p.course_id=?
               AND NOT (p.modname='forum' AND p.post_id LIKE 't%' AND p.body IS NULL)
             ORDER BY p.id
            """,
            (course["course_id"],),
        )
        for row in posts:
            week = week_number(row["section_idx"], row["section_name"], row["subject"])
            lesson = lesson_number(row["subject"])
            kind = _post_kind(row["board_title"])
            day = re.sub(r"\D", "", (row["written_at"] or "")[:10]) or "날짜없음"
            name = canonical_filename(
                week=week, lesson=lesson, kind=kind,
                title=f"{day}_{row['subject'] or '제목없음'}",
                stable_id=f"p{row['post_id']}", extension=".md",
            )
            entries.append(
                FlatEntry(
                    course_id=course["course_id"], cmid=row["cmid"] or 0,
                    kind=kind, week=week, lesson=lesson,
                    relative_path=str((base / name).relative_to(destination)),
                    source_ready=True,
                )
            )

        vods = store.query(
            """
            SELECT v.cmid,v.hls_url,v.duration_sec,a.title,a.section_idx,a.section_name
              FROM vod v JOIN activity a ON a.cmid=v.cmid
             WHERE v.course_id=? ORDER BY a.section_idx,a.cmid
            """,
            (course["course_id"],),
        )
        for row in vods:
            week = week_number(row["section_idx"], row["section_name"], row["title"])
            lesson = lesson_number(row["title"])
            name = canonical_filename(
                week=week, lesson=lesson, kind="강의영상", title=row["title"],
                stable_id=f"cmid{row['cmid']}", extension=".mp4",
            )
            entries.append(
                FlatEntry(
                    course_id=course["course_id"], cmid=row["cmid"],
                    kind="강의영상", week=week, lesson=lesson,
                    relative_path=str((base / name).relative_to(destination)),
                    source_ready=bool(row["hls_url"]), bytes=None,
                )
            )
    return sorted(entries, key=lambda entry: entry.relative_path)
