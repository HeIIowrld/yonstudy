"""공지 원문에서 시험 날짜를 찾아 예정된 시험 안내를 계속 표시한다."""

from __future__ import annotations

import re
from datetime import date, timedelta

from .notice_sources import current_notice_posts, notice_excerpt as _excerpt, notice_version as _version


EXAM_SUBJECT = re.compile(r"중간\s*(?:고사|시험)|기말\s*(?:고사|시험)|시험|\bmid[ -]?term\b|\b(?:final\s+)?exams?\b", re.I)
_KINDS = re.compile(r"(?P<mid>중간\s*(?:고사|시험)|\bmid[ -]?term\b)|(?P<final>기말\s*(?:고사|시험)|\bfinal\s+exams?\b)", re.I)
_NUMERIC_DATE = re.compile(
    r"(?<![\d/.-])(?:(?P<year>20\d{2})\s*(?:년|[./-])\s*)?"
    r"(?P<month>\d{1,2})\s*(?:월\s*|[./-]\s*)(?P<day>\d{1,2})(?:\s*일)?(?!\d)",
)
_MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}
_ENGLISH_DATE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>20\d{2}))?", re.I,
)
_CHANGE = re.compile(r"변경|정정|연기|취소|reschedul|postpon|cancel", re.I)
_WHEN = re.compile(r"일시|시험일|시험\s*날짜|날짜\s*:|\bdate\s*:|\bon\s*$|\b(?:held|place|scheduled)\b", re.I)
_OTHER_DATE = re.compile(r"과제|제출|범위|페이지|작성일|등록일|수정일|assignment|deadline|due\b|chapter|section|pages?\b|posted|updated", re.I)


def is_exam_subject(subject: str | None) -> bool:
    text = subject or ""
    if not EXAM_SUBJECT.search(text):
        return False
    # 'Midterm Project'처럼 중간 평가용 제출물을 시험일로 분류하지 않는다.
    if re.search(r"과제|보고서|프로젝트|발표|\b(?:assignments?|homeworks?|projects?|reports?|presentations?)\b", text, re.I):
        return bool(re.search(r"시험|고사|\bexams?\b", text, re.I))
    return True


def _kind(match) -> str:
    return "중간시험" if match.lastgroup == "mid" else "기말시험"


def _entries(row: dict, *, year: int) -> list[dict]:
    body = row.get("body") or ""
    title = row.get("title") or ""
    title_kinds = {_kind(match) for match in _KINDS.finditer(title)}
    cues = list(_KINDS.finditer(body))
    dates: dict[str, set[date]] = {}
    ambiguous_kinds = set()
    matches = sorted([*_NUMERIC_DATE.finditer(body), *_ENGLISH_DATE.finditer(body)], key=lambda m: m.start())
    for match in matches:
        prefix = re.split(r"[.!?]\s+", body[:match.start()])[-1][-160:]
        # 범위의 숫자나 과제 기한을 시험 날짜로 삼지 않는다.
        if not (_WHEN.search(prefix) or _KINDS.search(prefix) or (
            title_kinds and body[:match.start()].strip() in {"", "-", "*"}
        )):
            continue
        other_dates = list(_OTHER_DATE.finditer(prefix))
        when = list(_WHEN.finditer(prefix))
        if other_dates and (not when or other_dates[-1].start() > when[-1].start()):
            continue
        earlier = [cue for cue in cues if cue.end() <= match.start()]
        kind = _kind(earlier[-1]) if earlier else (
            next(iter(title_kinds)) if len(title_kinds) == 1 else "시험"
        )
        month = match["month"]
        month = _MONTHS[month[:3].lower()] if month[0].isalpha() else int(month)
        try:
            day = date(int(match["year"] or year), month, int(match["day"]))
        except ValueError:
            continue
        dates.setdefault(kind, set()).add(day)
        if re.match(r"\s*(?:[-~–]|to\b)\s*\d", body[match.end():], re.I):
            ambiguous_kinds.add(kind)

    changed = bool(_CHANGE.search(title + " " + body))
    kinds = set(dates) or title_kinds or {"시험"}
    entries = []
    for kind in sorted(kinds):
        days = dates.get(kind, set())
        ambiguous = len(days) > 1 or kind in ambiguous_kinds
        exam_day = next(iter(days)) if len(days) == 1 and not ambiguous else None
        excerpt = body
        if len(dates) > 1:
            # 중간·기말이 같은 공지에 있으면 해당 문장과 공통 주의사항을 남긴다.
            parts = re.split(r"(?<=[.!?])\s+|[\r\n]+", body)
            matching = [part for part in parts if any(_kind(cue) == kind for cue in _KINDS.finditer(part))]
            shared = [part for part in parts if not _KINDS.search(part) and not (
                _NUMERIC_DATE.search(part) or _ENGLISH_DATE.search(part)
            )]
            excerpt = " ".join(matching + shared) or body
        entries.append(dict(row, exam_kind=kind, exam_date=exam_day.isoformat() if exam_day else None,
                            excerpt=_excerpt(excerpt), schedule_changed=changed,
                            ambiguous_dates=ambiguous, version=_version(row)))
    return entries


def collect_exam_notices(store, *, target: date, year: str, semester: str) -> list[dict]:
    rows = current_notice_posts(store, year=year, semester=semester)
    schedules = {}
    undated = []
    for source in rows:
        row = dict(source)
        if not is_exam_subject(row["title"]):
            continue
        for entry in _entries(row, year=int(year)):
            if entry["exam_date"] or entry["schedule_changed"] or entry["ambiguous_dates"]:
                key = (entry["course_id"], entry["exam_kind"])
                old = schedules.get(key)
                if old is None or entry["version"] > old["version"]:
                    schedules[key] = entry
            elif not re.search(r"팀\s*배정|team\s*(?:allocation|assignment)", entry["title"], re.I):
                undated.append(entry)
    result = []
    for entry in [*schedules.values(), *undated]:
        exam_day = date.fromisoformat(entry["exam_date"]) if entry["exam_date"] else None
        if exam_day and exam_day < target:
            continue
        if not exam_day:
            version_day = entry["version"][:10]
            if not version_day or version_day < (target - timedelta(days=14)).isoformat():
                continue
        entry["days_until"] = (exam_day - target).days if exam_day else None
        entry["updated_today"] = bool(
            entry.get("updated_at") and str(entry["updated_at"])[:10] == target.isoformat()
            and entry.get("updated_at") != entry.get("fetched_at")
        )
        result.append(entry)
    return sorted(result, key=lambda row: (row["exam_date"] or target.isoformat(), row["course_name"], row["exam_kind"]))


def exam_notice_label(row: dict) -> str:
    days = row.get("days_until")
    if days is not None:
        label = "오늘 시험" if days == 0 else f"D-{days}"
        label += f" · 시험일 {row['exam_date']}"
    else:
        label = "일정은 원문 확인"
    if row.get("schedule_changed"):
        label += " · 일정 변경 공지"
    if row.get("updated_today"):
        label += " · 본문 수정 감지"
    return label
