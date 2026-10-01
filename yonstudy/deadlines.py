"""과제 본문과 공지에서 LearnUs 화면에 없는 마감 기한을 보완한다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_KEYWORD = re.compile(
    r"(?:submission\s+deadline|deadline|\bdue\b|제출\s*기한|접수\s*마감|마감(?:일|기한|시간)?)",
    re.I,
)
_ISO_DATE = re.compile(
    r"(?P<year>20\d{2})\s*[-/.]\s*(?P<month>\d{1,2})\s*[-/.]\s*(?P<day>\d{1,2})"
)
_KO_DATE = re.compile(
    r"(?:(?P<year>20\d{2})\s*년\s*)?(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일"
)
_EN_DATE = re.compile(
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:\s*,\s*(?P<year>20\d{2}))?",
    re.I,
)
_NUMERIC_DATE = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*/\s*(?P<day>\d{1,2})(?!\d)")
_TABLE_ROW = re.compile(
    r"(?<!\d)(?P<number>\d{1,2})\.\s+(?P<title>.{1,100}?)\s+"
    r"\d+(?:\.\d+)?\s+"
    r"(?P<start>20\d{2}-\d{2}-\d{2})\s+"
    r"(?P<due>20\d{2}-\d{2}-\d{2})\s+"
    r"(?P<late>20\d{2}-\d{2}-\d{2})"
    r"(?=\s+\d{1,2}\.\s+|$)",
    re.S,
)


@dataclass(frozen=True)
class ParsedDeadline:
    due_at: str
    late_until: str | None = None
    evidence: str = ""


@dataclass(frozen=True)
class _Candidate:
    deadline: ParsedDeadline
    source_kind: str
    source_ref: str
    source_url: str
    source_at: str
    confidence: int


def _reference_date(value: str | None) -> date | None:
    if not value:
        return None
    match = re.search(r"(20\d{2})\D+(\d{1,2})\D+(\d{1,2})", value)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _resolved_year(explicit: str | None, month: int, year_hint: int,
                   reference: date | None) -> int:
    if explicit:
        return int(explicit)
    year = reference.year if reference else year_hint
    # 12월 공지가 다음 해 1월 기한을 안내하는 경우만 자연스럽게 넘긴다.
    if reference and reference.month - month >= 6:
        year += 1
    return year


def _time_after(segment: str, end: int) -> tuple[int, int]:
    tail = segment[end:end + 60]
    korean = re.search(r"(오전|오후)\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", tail)
    if korean:
        hour = int(korean.group(2)) % 12
        if korean.group(1) == "오후":
            hour += 12
        return hour, int(korean.group(3) or 0)
    clock = re.search(r"(?<!\d)(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?", tail, re.I)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        period = (clock.group(3) or "").lower().replace(".", "")
        if hour <= 12:
            if period == "pm" and hour < 12:
                hour += 12
            elif period == "am" and hour == 12:
                hour = 0
        return hour, minute
    return 23, 59


def _date_in_segment(segment: str, *, year_hint: int,
                     reference: date | None) -> tuple[datetime, tuple[int, int]] | None:
    matches: list[tuple[int, re.Match, str]] = []
    for kind, pattern in (
        ("iso", _ISO_DATE), ("ko", _KO_DATE), ("en", _EN_DATE),
        ("numeric", _NUMERIC_DATE),
    ):
        match = pattern.search(segment)
        if match:
            matches.append((match.start(), match, kind))
    if not matches:
        return None
    _, match, kind = min(matches, key=lambda item: item[0])
    groups = match.groupdict()
    month = _MONTHS[groups["month"].lower()] if kind == "en" else int(groups["month"])
    year = _resolved_year(groups.get("year"), month, year_hint, reference)
    hour, minute = _time_after(segment, match.end())
    try:
        return datetime(year, month, int(groups["day"]), hour, minute), match.span()
    except ValueError:
        return None


def extract_deadline(text: str, *, year_hint: int,
                     reference_date: str | None = None) -> ParsedDeadline | None:
    """deadline/due/마감 표현 뒤의 절대 날짜를 정규화한다."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    reference = _reference_date(reference_date)
    candidates: list[tuple[datetime, str]] = []
    for keyword in _KEYWORD.finditer(clean):
        segment = clean[keyword.start():keyword.end() + 130]
        parsed = _date_in_segment(segment, year_hint=year_hint, reference=reference)
        if parsed:
            when, _ = parsed
            candidates.append((when, segment[:180].strip()))
    if not candidates:
        return None
    # 연장 공지에 이전·새 기한이 함께 있으면 더 늦은 기한을 채택한다.
    when, evidence = max(candidates, key=lambda item: item[0])
    return ParsedDeadline(when.strftime("%Y-%m-%d %H:%M"), evidence=evidence)


def extract_deadline_table(text: str) -> dict[int, ParsedDeadline]:
    """START/END/LATE 형식으로 붙여 넣은 과제 일정표를 읽는다."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not re.search(r"\bSTART\b.*\bEND\b.*\bLATE\b", clean, re.I):
        return {}
    result: dict[int, ParsedDeadline] = {}
    for match in _TABLE_ROW.finditer(clean):
        result[int(match.group("number"))] = ParsedDeadline(
            due_at=match.group("due") + " 23:59",
            late_until=match.group("late") + " 23:59",
            evidence=match.group(0),
        )
    return result


def assignment_number(title: str) -> int | None:
    """Assignment 3, HW3, `3. ... Assignment`의 번호를 반환한다."""
    value = re.sub(r"\s+", " ", title or "").strip()
    match = re.search(
        r"(?:assignments?|homeworks?|hw|과제)\s*[#\"'“”]?\s*(\d{1,2})\b",
        value,
        re.I,
    )
    if match:
        return int(match.group(1))
    if re.search(r"\b(?:assignment|homework|과제)\b", value, re.I):
        match = re.match(r"(\d{1,2})\.\s+", value)
        if match:
            return int(match.group(1))
    return None


def infer_course_assignment_deadlines(store, course_id: int) -> int:
    """한 강좌의 마감 없는 제출 활동을 본문·공지와 연결해 보조 기한을 저장한다."""
    course = store.query("SELECT year FROM course WHERE course_id=?", (course_id,))
    if not course:
        return 0
    try:
        year_hint = int(course[0]["year"])
    except (TypeError, ValueError):
        year_hint = datetime.now().year
    submissions = store.query(
        """
        SELECT cmid,title,due_at,instructions,seen_at
          FROM submission
         WHERE course_id=? AND (due_at IS NULL OR trim(due_at)='')
        """,
        (course_id,),
    )
    posts = [dict(row) for row in store.query(
        """
        SELECT post_id,subject,body,written_at,url
          FROM post WHERE course_id=? AND COALESCE(body,'')<>''
        """,
        (course_id,),
    )]
    post_tables = {
        str(post["post_id"]): extract_deadline_table(
            f"{post.get('subject') or ''} {post.get('body') or ''}"
        )
        for post in posts
    }

    saved = 0
    for stored in submissions:
        row = dict(stored)
        number = assignment_number(row.get("title") or "")
        candidates: list[_Candidate] = []
        instruction_deadline = extract_deadline(
            row.get("instructions") or "", year_hint=year_hint,
            reference_date=row.get("seen_at"),
        )
        if instruction_deadline:
            candidates.append(_Candidate(
                instruction_deadline, "instructions", str(row["cmid"]), "", "", 3,
            ))

        for post in posts:
            post_id = str(post["post_id"])
            table_deadline = post_tables[post_id].get(number) if number is not None else None
            if table_deadline:
                candidates.append(_Candidate(
                    table_deadline, "post_table", post_id, post.get("url") or "",
                    post.get("written_at") or "", 3,
                ))

            post_text = f"{post.get('subject') or ''}\n{post.get('body') or ''}"
            if number is None or assignment_number(post_text) != number:
                continue
            deadline = extract_deadline(
                post_text, year_hint=year_hint, reference_date=post.get("written_at"),
            )
            if deadline:
                candidates.append(_Candidate(
                    deadline, "post", post_id, post.get("url") or "",
                    post.get("written_at") or "", 2,
                ))

        if not candidates:
            continue
        # 공지 수정/연장 가능성을 반영해 최근 글을 우선하고, 같은 글이면 표를 우선한다.
        chosen = max(candidates, key=lambda item: (item.source_at[:19], item.confidence))
        store.save_assignment_deadline({
            "cmid": row["cmid"],
            "due_at": chosen.deadline.due_at,
            "late_until": chosen.deadline.late_until,
            "source_kind": chosen.source_kind,
            "source_ref": chosen.source_ref,
            "source_url": chosen.source_url or None,
            "evidence": chosen.deadline.evidence,
        })
        saved += 1
    return saved


__all__ = [
    "ParsedDeadline", "assignment_number", "extract_deadline",
    "extract_deadline_table", "infer_course_assignment_deadlines",
]
