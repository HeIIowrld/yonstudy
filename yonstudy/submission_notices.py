"""과제·보고서·활동일지 공지의 일정과 확인된 개인 제출 상태를 표시한다."""

from __future__ import annotations

import re
from datetime import date, timedelta

from .assignment_state import is_submission_required
from .deadlines import assignment_number, extract_deadline, extract_deadline_table
from .exam_notices import is_exam_subject
from .notice_sources import current_notice_posts, notice_excerpt, notice_version


_TASK = re.compile(r"과제|제출|보고서|레포트|리포트|프로젝트|발표|활동\s*일지|"
                   r"\b(?:assignments?|homeworks?|hw\s*\d|submissions?|reports?|projects?|presentations?|deliverables?)\b", re.I)
_NO_SUBMISSION = re.compile(r"제출\s*(?:하지\s*않|불필요|안\s*함|없음)|no\s+submission|do\s+not\s+submit", re.I)
_CHANGE = re.compile(r"변경|정정|연장|연기|취소|extend|reschedul|postpon|cancel", re.I)
_DEADLINE = re.compile(r"마감|제출\s*(?:기한|기간|일시|일)|등록\s*(?:기한|기간)|접수\s*기한|"
                       r"\bdue\b|deadline|\bsubmit\b.*\bby\b|발표\s*(?:일시|일)|presentation\s+(?:date|on)", re.I)
_DATE = re.compile(r"(?<![\d/.-])(?:(?:20\d{2})\s*(?:년|[./-])\s*)?"
                   r"\d{1,2}\s*(?:월\s*|[./-]\s*)\d{1,2}(?:\s*일)?(?!\d)|"
                   r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
                   r"\s+\d{1,2}(?:st|nd|rd|th)?\b(?:,?\s+20\d{2})?", re.I)
_CLOCK = re.compile(r"^\s*(?:\([^)]{1,30}\)\s*|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s*)?"
                    r"(?:at\s+)?(?:(?:오전|오후)\s*)?"
                    r"(?:\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?|\d{1,2}\s*[ap]\.?m\.?|\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?)", re.I)


def is_submission_subject(subject: str | None) -> bool:
    return bool(_TASK.search(subject or "")) and not is_exam_subject(subject)


def _deadline(text: str, *, year: int, reference: str) -> tuple[str | None, bool]:
    """마감 표현에 붙은 날짜만 사용한다. 별개 일정이 섞이면 원문 확인으로 둔다."""
    candidates = []
    for part in re.split(r"(?<=[.!?])\s+|[;\r\n]+", text):
        matches = list(_DATE.finditer(part))
        for index, match in enumerate(matches):
            left = part[(matches[index - 1].end() if index else 0):match.start()][-100:]
            right = part[match.end():(matches[index + 1].start() if index + 1 < len(matches) else len(part))][:80]
            # 기간의 시작일은 마감으로 쓰지 않는다.
            if re.match(r"\s*(?:[-~–]|to\b)", right, re.I) and index + 1 < len(matches):
                continue
            cue = _DEADLINE.search(left)
            until = re.search(r"까지\s*(?:제출|등록|접수)|(?:제출|등록).*마감|\b(?:is\s+)?(?:the\s+)?deadline\b", right, re.I)
            range_end = index > 0 and re.fullmatch(r"\s*(?:[-~–]|to)\s*", left, re.I)
            if range_end:
                cue = _DEADLINE.search(part[:matches[index - 1].start()][-100:])
            if index > 0 and _CHANGE.search(part) and re.fullmatch(r"\s*(?:에서|→|->|to)\s*", left, re.I):
                cue = _DEADLINE.search(part[:matches[index - 1].start()][-100:])
            if not cue and not until:
                continue
            if cue and re.search(r"시험|활동\s*기간|작성일|게시일|시작일|범위|페이지|\b(?:exam|posted|start|chapter|section|pages?)\b", left[cue.end():], re.I):
                continue
            clock = _CLOCK.match(right)
            date_text = match.group()
            # 기존 마감 파서는 한국어 날짜, ISO, 영어 월 이름, M/D를 읽는다.
            if re.fullmatch(r"\d{1,2}\s*[.-]\s*\d{1,2}", date_text):
                date_text = re.sub(r"\s*[.-]\s*", "/", date_text)
            clock_text = clock.group() if clock else ""
            # 시간만 적힌 6PM, 18시도 기존 파서가 읽는 H:MM 형태로 맞춘다.
            if clock and not re.search(r"오전|오후", clock_text):
                clock_text = re.sub(r"(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?",
                                    lambda m: f"{m[1]}:{int(m[2] or 0):02d}", clock_text)
                clock_text = re.sub(r"(?<![\d:])(\d{1,2})\s*([ap]\.?m\.?)", r"\1:00\2", clock_text, flags=re.I)
            parsed = extract_deadline("deadline: " + date_text + clock_text,
                                      year_hint=year, reference_date=reference)
            if parsed:
                candidates.append(parsed.due_at if clock else parsed.due_at[:10])
    unique = list(dict.fromkeys(candidates))
    # 제목은 날짜만, 본문은 시간까지 적은 경우 본문의 시간을 보완한다.
    timed_days = {value[:10] for value in unique if len(value) > 10}
    unique = [value for value in unique if len(value) > 10 or value not in timed_days]
    if len(unique) == 1:
        return unique[0], False
    if len(unique) > 1:
        # 명확한 이전→새 기한 표기만 마지막 날짜로 바꾼다. 다중 제출물은 추측하지 않는다.
        if _CHANGE.search(text) and re.search(r"(?:→|->)|(?:에서|from).*(?:으로|to)|기존.*(?:변경|새\s*기한)", text, re.I):
            return unique[-1], False
        return None, True
    return None, False


def _name(value: str) -> str:
    clean = re.sub(r"\b(?:coding\s+)?assignments?\b|\bhomeworks?\b|\bhw\b|"
                   r"\b(?:submission|deadline|schedule|updated|extended|cancelled)\b|"
                   r"과제|공지|안내|제출|기한|마감|일정|변경|정정|연장|취소", "", value, flags=re.I)
    return re.sub(r"[^\w]", "", clean).lower()


def _linked_assignments(entry: dict, assignments: list[dict]) -> list[dict]:
    course = [row for row in assignments if row["course_id"] == entry["course_id"]]
    number = entry.get("assignment_number")
    if number is not None:
        # 번호만 같은 여러 제출물을 하나로 단정하지 않는다.
        matches = [row for row in course if assignment_number(row["title"]) == number]
        for qualifier in (r"팀|\bteam\b", r"개인|\bindividual\b"):
            if re.search(qualifier, entry["title"], re.I):
                matches = [row for row in matches if re.search(qualifier, row["title"], re.I)]
    else:
        name = _name(entry["title"])
        matches = [row for row in course if (len(_name(row["title"])) >= 4
                   and _name(row["title"]) in name)
                   or (row.get("deadline_source_url") and row["deadline_source_url"] == entry.get("url"))]
    return matches if len(matches) == 1 else []


def _entries(row: dict, *, year: int) -> list[dict]:
    body = row.get("body") or ""
    text = row["title"] + "\n" + body
    common = dict(row, version=notice_version(row), schedule_changed=bool(_CHANGE.search(text)),
                  preparation_only=bool(_NO_SUBMISSION.search(row["title"])), late_until=None,
                  presentation_schedule=bool(re.search(r"발표|\bpresentation\b", row["title"], re.I)))
    table = extract_deadline_table(body)
    if table:
        entries = []
        for number, deadline in table.items():
            title = re.split(r"\s+\d+(?:\.\d+)?\s+20\d{2}-", deadline.evidence, maxsplit=1)[0]
            entries.append(dict(common, title=title, source_title=row["title"], assignment_number=number,
                                notice_due_at=deadline.due_at[:10], late_until=deadline.late_until[:10],
                                ambiguous_dates=False, excerpt=notice_excerpt(deadline.evidence)))
        return entries
    due_at, ambiguous = _deadline(text, year=year, reference=common["version"])
    return [dict(common, assignment_number=assignment_number(row["title"]), notice_due_at=due_at,
                 ambiguous_dates=ambiguous, excerpt=notice_excerpt(body))]


def collect_submission_notices(store, *, target: date, year: str, semester: str,
                               assignments: list[dict]) -> list[dict]:
    schedules = {}
    undated = []
    for row in current_notice_posts(store, year=year, semester=semester):
        if not is_submission_subject(row["title"]):
            continue
        for entry in _entries(row, year=int(year)):
            entry["linked_assignments"] = _linked_assignments(entry, assignments)
            if entry["notice_due_at"] or entry["ambiguous_dates"] or (
                entry["schedule_changed"]
                and re.search(r"마감|기한|일정|취소|deadline|due|cancel", row["title"] + " " + (row.get("body") or ""), re.I)
            ):
                qualifier = "team" if re.search(r"팀|\bteam\b", entry["title"], re.I) else (
                    "individual" if re.search(r"개인|\bindividual\b", entry["title"], re.I) else ""
                )
                identity = ("assignment", entry["assignment_number"], qualifier) if entry["assignment_number"] is not None else (
                    "title", _name(entry["title"]) or entry["post_id"]
                )
                key = (entry["course_id"], identity)
                if key not in schedules or entry["version"] > schedules[key]["version"]:
                    schedules[key] = entry
            else:
                undated.append(entry)
    result = []
    for entry in [*schedules.values(), *undated]:
        until = entry["late_until"] or entry["notice_due_at"]
        if until and until[:10] < target.isoformat():
            continue
        if not until and entry["version"][:10] < (target - timedelta(days=14)).isoformat():
            continue
        due = entry["notice_due_at"]
        entry["days_until"] = (date.fromisoformat(due[:10]) - target).days if due else None
        entry["updated_today"] = bool(entry.get("updated_at") and entry["updated_at"][:10] == target.isoformat()
                                      and entry["updated_at"] != entry.get("fetched_at"))
        result.append(entry)
    return sorted(result, key=lambda row: (row["notice_due_at"] or target.isoformat(), row["course_name"], row["title"]))


def submission_notice_label(row: dict) -> str:
    labels = []
    due = row.get("notice_due_at")
    days = row.get("days_until")
    if due:
        labels.append(("오늘 일정" if row.get("presentation_schedule") else "오늘 마감") if days == 0
                      else (f"D-{days}" if days > 0 else "정상 마감 지남"))
        labels.append(f"공지 일정 {due}")
        if row.get("late_until") and row["late_until"] != due[:10]:
            labels.append(f"지각 제출 {row['late_until']}까지")
    else:
        labels.append("일정은 원문 확인")
    linked = row.get("linked_assignments") or []
    if row.get("preparation_only"):
        labels.append("준비 안내 · 제출 없음")
    elif linked:
        assignment = linked[0]
        labels.append("본인 제출 불필요" if not is_submission_required(assignment) else
                      "제출 완료" if assignment.get("submitted") == 1 else
                      "미제출" if assignment.get("submitted") == 0 else "제출 상태 미확인")
        if assignment.get("due_at"):
            site_due = assignment["due_at"]
            match = re.search(r"(20\d{2})\D+(\d{1,2})\D+(\d{1,2})(?:\D+(\d{1,2}):(\d{2}))?", site_due)
            if match:
                site_due = f"{match[1]}-{int(match[2]):02d}-{int(match[3]):02d}"
                if match[4]:
                    site_due += f" {int(match[4]):02d}:{match[5]}"
            labels.append(f"사이트 마감 {site_due}")
    else:
        labels.append("제출 여부는 별도 확인")
    if row.get("schedule_changed"):
        labels.append("변경 공지")
    if row.get("updated_today"):
        labels.append("본문 수정 감지")
    return " · ".join(labels)
