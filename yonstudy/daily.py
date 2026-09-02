"""한국시간 기준 LearnUs 일일 리포트와 메일 전송."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")
WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")


def current_term(day: date) -> tuple[str, str]:
    """날짜를 LearnUs의 연도/학기 표기로 바꾼다."""
    if day.month <= 2:
        return str(day.year - 1), "겨울계절수업"
    if day.month <= 6:
        return str(day.year), "1학기"
    if day.month <= 8:
        return str(day.year), "여름계절수업"
    return str(day.year), "2학기"


def _rows(store, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in store.query(sql, args)]


def _course_filter(year: str, semester: str) -> tuple[str, tuple]:
    return "c.year=? AND c.semester=?", (year, semester)


def _date_in(value: str | None) -> date | None:
    if not value:
        return None
    m = re.search(r"(20\d{2})\D(\d{1,2})\D(\d{1,2})", value)
    if not m:
        return None
    try:
        return date(*(int(x) for x in m.groups()))
    except ValueError:
        return None


@dataclass
class DailyReport:
    target: date
    year: str
    semester: str
    generated_at: str
    source_updated_at: str | None
    stale: bool
    opened: list[dict]
    completed: list[dict]
    new_posts: list[dict]
    new_files: list[dict]
    updates: list[dict]
    todos: list[dict]
    today_schedule: list[dict]
    assignments: list[dict]
    viewing_queue: list[dict]
    attendance: list[dict]
    completion_by_course: list[dict]
    untracked: list[dict]


def _board_category(board_name: str | None, subject: str | None = None) -> str:
    """지난 학기 강의실에서 반복된 한/영문 게시판 이름을 같은 범주로 묶는다."""
    text = f"{board_name or ''} {subject or ''}".lower()
    if any(token in text for token in ("q&a", "qna", "질의", "질문", "문의")):
        return "Q&A"
    if any(token in text for token in ("공지", "announcement", "notice")):
        return "공지"
    if any(token in text for token in ("자료", "material", "slide", "강의안")):
        return "자료"
    return "게시글"


def _excerpt(value: str | None, limit: int = 180) -> str:
    clean = re.sub(r"\s+", " ", value or "").strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _file_category(role: str | None) -> str:
    return {
        "resource": "강의자료",
        "post": "게시판 첨부",
        "submission": "과제 첨부",
    }.get(role or "", "첨부자료")


def _format_datetime_ko(value: str | None, *, include_year: bool = False) -> str:
    """LearnUs 날짜 문자열을 메일에서 읽기 쉬운 한국어 표기로 바꾼다."""
    if not value:
        return ""
    match = re.search(
        r"(20\d{2})\D(\d{1,2})\D(\d{1,2})(?:\D+(\d{1,2}):(\d{2}))?",
        value,
    )
    if not match:
        return value
    year, month, day = (int(v) for v in match.group(1, 2, 3))
    result = f"{year}년 " if include_year else ""
    result += f"{month}월 {day}일({WEEKDAYS_KO[date(year, month, day).weekday()]})"
    if match.group(4) is not None:
        hour, minute = int(match.group(4)), int(match.group(5))
        period = "오전" if hour < 12 else "오후"
        display_hour = hour % 12 or 12
        result += f" {period} {display_hour}:{minute:02d}"
    return result


def _deadline_summary(row: dict) -> str:
    normal_raw = row.get("due_at") or row.get("open_to") or row.get("normal_deadline")
    late_raw = row.get("late_until") or row.get("final_deadline")
    normal = _format_datetime_ko(normal_raw)
    late = _format_datetime_ko(late_raw)
    is_video = row.get("modname") == "vod" or "period" in row
    normal_label = "출석 인정 마감" if is_video else "제출 마감"
    late_label = "지각 인정" if is_video else "지각 제출"
    if normal and late and late != normal:
        return f"{normal_label} {normal} · {late_label} {late}까지"
    return f"{normal_label} {normal}" if normal else "마감 시간이 표시되지 않음"


def _attendance_status(row: dict) -> tuple[str, str]:
    if row.get("verified"):
        return "수강 완료", "#047857"
    progress = float(row.get("progress_pct") or 0)
    watched = int(row.get("watched_sec") or 0)
    if progress <= 0 and watched <= 0:
        return "미수강", "#b45309"
    if progress < 100:
        return "수강 중", "#2563eb"
    return "확인 필요", "#b45309"


def _stable_schedule_day(row: dict, target: date, deadline: date | None) -> date:
    """공개일부터 정상 마감 전날 사이에 재현 가능한 권장 처리일을 배정한다.

    해시는 실행할 때마다 날짜가 흔들리지 않게 하기 위한 것이며, 외부 서비스에
    사람처럼 보이기 위한 랜덤화가 아니다. 이미 권장일이 지났으면 오늘로 당긴다.
    """
    if deadline is None or deadline <= target:
        return target
    opened = _date_in(row.get("open_from")) or target
    anchor = min(max(opened, target), deadline)
    last_safe = max(anchor, deadline - timedelta(days=1))
    span = max(0, (last_safe - anchor).days)
    key = f"{row.get('cmid')}:{deadline.isoformat()}".encode()
    offset = int.from_bytes(hashlib.sha256(key).digest()[:4], "big") % (span + 1)
    return anchor + timedelta(days=offset)


def _enrich_todo(row: dict, target: date) -> dict:
    row = dict(row)
    normal = _date_in(row.get("due_at") or row.get("open_to"))
    late = _date_in(row.get("late_until"))
    final = late or normal
    if normal is None:
        urgency = "기한 없음"
    elif normal < target:
        urgency = "지각 가능" if late and late >= target else "기한 지남"
    elif normal == target:
        urgency = "오늘 마감"
    elif normal <= target + timedelta(days=2):
        urgency = "긴급"
    else:
        urgency = "예정"

    duration = row.get("duration_sec") or 0
    watched = row.get("watched_sec") or 0
    remaining = max(0, duration - watched)
    rate = max(float(row.get("max_rate") or 1.0), 0.25)
    row.update(
        urgency=urgency,
        normal_deadline=normal.isoformat() if normal else None,
        final_deadline=final.isoformat() if final else None,
        scheduled_for=_stable_schedule_day(row, target, normal or final).isoformat(),
        remaining_minutes=(remaining + 59) // 60 if duration else None,
        eta_minutes=round(remaining / rate / 60) if duration else None,
        actionable=not final or final >= target,
    )
    return row


def build_daily_report(
    store,
    *,
    target: date | None = None,
    year: str | None = None,
    semester: str | None = None,
    horizon_days: int = 14,
) -> DailyReport:
    now = datetime.now(SEOUL)
    target = target or now.date()
    default_year, default_semester = current_term(target)
    year = year or default_year
    semester = semester or default_semester
    day_s = target.isoformat()
    horizon_s = (target + timedelta(days=horizon_days)).isoformat()
    course_sql, course_args = _course_filter(year, semester)

    freshness = store.query(
        f"""
        SELECT MAX(a.seen_at) AS updated_at
          FROM activity a JOIN course c ON c.course_id=a.course_id
         WHERE {course_sql}
        """,
        course_args,
    )[0]["updated_at"]
    stale = not freshness or not str(freshness).startswith(day_s)

    opened = _rows(
        store,
        f"""
        SELECT a.cmid,c.name AS course_name,a.modname,a.title,a.url,
               a.open_from,a.open_to,a.late_until,a.completion,
               v.progress_pct,s.submitted
          FROM activity a
          JOIN course c ON c.course_id=a.course_id
          LEFT JOIN vod v ON v.cmid=a.cmid
          LEFT JOIN submission s ON s.cmid=a.cmid
         WHERE {course_sql} AND a.restricted=0
           AND substr(a.open_from,1,10)=?
         ORDER BY a.open_from,c.name,a.section_idx,a.cmid
        """,
        course_args + (day_s,),
    )

    completed_events = _rows(
        store,
        f"""
        SELECT e.at,e.kind,e.cmid,e.title,e.old_value,e.new_value,
               c.name AS course_name,a.modname,a.url
          FROM change_event e
          JOIN course c ON c.course_id=e.course_id
          LEFT JOIN activity a ON a.cmid=e.cmid
         WHERE {course_sql} AND substr(e.at,1,10)=?
           AND (
                e.kind IN ('vod_completed','submission_completed')
                OR (e.kind='completion_changed' AND e.new_value='y')
           )
         ORDER BY e.at,e.id
        """,
        course_args + (day_s,),
    )
    # 한 활동에서 completion 아이콘과 상세 진도가 같이 바뀌면 한 번만 표시한다.
    completed: list[dict] = []
    seen_completed: set[int] = set()
    rank = {"vod_completed": 0, "submission_completed": 0, "completion_changed": 1}
    for row in sorted(completed_events, key=lambda r: rank.get(r["kind"], 9)):
        cmid = row.get("cmid")
        if cmid in seen_completed:
            continue
        seen_completed.add(cmid)
        completed.append(row)
    completed.sort(key=lambda r: r["at"])

    event_updates = _rows(
        store,
        f"""
        SELECT e.at,e.kind,e.cmid,e.title,e.new_value,e.details,
               c.name AS course_name,a.modname,a.url
          FROM change_event e
          JOIN course c ON c.course_id=e.course_id
          LEFT JOIN activity a ON a.cmid=e.cmid
         WHERE {course_sql} AND substr(e.at,1,10)=?
           AND e.kind IN ('activity_discovered','activity_updated')
         ORDER BY e.at,e.id
        """,
        course_args + (day_s,),
    )
    # 새 게시글은 게시판 이름과 본문 요약까지 붙여 Q&A/공지/자료로 분류한다.
    # fetched_at은 신규 발견 시점이고, written_at 당일 조건은 이력 기능 도입 전
    # 작성된 당일 글을 복구하기 위한 보조 조건이다.
    new_posts = _rows(
        store,
        f"""
        SELECT p.fetched_at AS at,p.cmid,p.post_id,p.subject AS title,
               p.written_at,p.writer,p.body,c.name AS course_name,
               a.title AS board_name,p.modname,p.url
          FROM post p
          JOIN course c ON c.course_id=p.course_id
          LEFT JOIN activity a ON a.cmid=p.cmid
         WHERE {course_sql}
           AND (substr(p.fetched_at,1,10)=? OR substr(p.written_at,1,10)=?)
         ORDER BY p.written_at,p.id
        """,
        course_args + (day_s, day_s),
    )
    unique_posts = []
    seen_posts = set()
    for row in new_posts:
        key = (row.get("cmid"), row.get("post_id"), row.get("title"))
        if key in seen_posts:
            continue
        seen_posts.add(key)
        row["category"] = _board_category(row.get("board_name"), row.get("title"))
        row["excerpt"] = _excerpt(row.get("body"))
        unique_posts.append(row)
    new_posts = unique_posts

    new_files = _rows(
        store,
        f"""
        SELECT f.saved_at AS at,f.cmid,f.role,f.name,f.url,f.bytes,f.sha256,
               c.course_id,c.name AS course_name,a.title AS activity_title,
               CASE WHEN a.modname IN ('ubboard','forum') THEN a.title ELSE NULL END AS board_name
          FROM file f
          JOIN course c ON c.course_id=f.course_id
          LEFT JOIN activity a ON a.cmid=f.cmid
         WHERE {course_sql}
           AND f.role IN ('resource','post','introattachment')
           AND substr(f.saved_at,1,10)=?
         ORDER BY f.saved_at,f.id
        """,
        course_args + (day_s,),
    )
    unique_files = []
    seen_files = set()
    for row in new_files:
        key = (row["course_id"], row.get("sha256") or row.get("url"), row["role"])
        if key in seen_files:
            continue
        seen_files.add(key)
        unique_files.append(row)
    new_files = unique_files
    updates = event_updates

    # LearnUs가 명시적으로 미완료(n)라고 표시한 항목과, 공개됐으나 진도
    # 100% 미만인 영상을 할 일로 잡는다. 완료 추적 신호가 없는(NULL) 자료는
    # 자동으로 미완료 취급하지 않는다.
    todo_candidates = _rows(
        store,
        f"""
        SELECT DISTINCT a.cmid,c.name AS course_name,a.modname,a.title,a.url,
               a.open_from,a.open_to,a.late_until,a.completion,
               v.progress_pct,v.duration_sec,v.watched_sec,v.max_rate,
               v.can_log_progress,s.submitted,s.due_at
          FROM activity a
          JOIN course c ON c.course_id=a.course_id
          LEFT JOIN vod v ON v.cmid=a.cmid
          LEFT JOIN submission s ON s.cmid=a.cmid
         WHERE {course_sql} AND a.restricted=0
           AND (
                a.completion='n'
                OR (
                    a.modname='vod' AND COALESCE(v.progress_pct,0)<100
                    AND a.open_from IS NOT NULL
                    AND substr(a.open_from,1,10)<=?
                    AND (
                        COALESCE(a.late_until,a.open_to) IS NULL
                        OR substr(COALESCE(a.late_until,a.open_to),1,10)>=?
                    )
                )
                OR (
                    s.submitted=0 AND s.due_at IS NOT NULL AND trim(s.due_at)<>''
                )
           )
           AND (
                a.open_from IS NULL OR substr(a.open_from,1,10)<=?
           )
         ORDER BY COALESCE(a.late_until,a.open_to,s.due_at,'9999'),c.name,a.cmid
        """,
        course_args + (horizon_s, day_s, horizon_s),
    )
    todos = []
    horizon = target + timedelta(days=horizon_days)
    for row in todo_candidates:
        opens = _date_in(row.get("open_from"))
        deadline = _date_in(row.get("due_at") or row.get("open_to"))
        final_deadline = _date_in(row.get("late_until")) or deadline
        if opens and opens > horizon:
            continue
        # 미래 항목은 미리보기 범위까지만, 이미 지난 미완료 항목은 놓치지 않는다.
        if deadline and deadline > horizon:
            continue
        if deadline is None and final_deadline and final_deadline > horizon:
            continue
        todos.append(_enrich_todo(row, target))

    urgency_order = {"오늘 마감": 0, "긴급": 1, "지각 가능": 2,
                     "예정": 3, "기한 없음": 4, "기한 지남": 5}
    todos.sort(key=lambda r: (
        urgency_order.get(r["urgency"], 9),
        r.get("normal_deadline") or r.get("final_deadline") or "9999",
        r["course_name"], r["cmid"],
    ))

    assignments = [row for row in todos if row.get("submitted") == 0]
    today_schedule: list[dict] = []
    scheduled_cmids: set[int] = set()
    for row in todos:
        if row.get("scheduled_for") == day_s or row.get("normal_deadline") == day_s:
            item = dict(row)
            item["schedule_kind"] = row.get("urgency") or "처리 예정"
            today_schedule.append(item)
            scheduled_cmids.add(row["cmid"])
    for row in opened:
        already_done = (
            row.get("completion") == "y"
            or (row.get("progress_pct") is not None and float(row["progress_pct"]) >= 100)
            or row.get("submitted") == 1
        )
        if row["cmid"] not in scheduled_cmids and not already_done:
            item = dict(row)
            item["schedule_kind"] = "오늘 공개"
            today_schedule.append(item)

    # 온라인출석부의 최대 학습위치와 진도율을 함께 검증한다. 재생은 하지 않는다.
    from .monitor import attendance_snapshot, viewing_queue

    viewing = viewing_queue(store, year=year, semester=semester, now=now)
    attendance = [
        row for row in attendance_snapshot(store, year=year, semester=semester)
        if row.get("progress_pct") is not None
        or not row.get("open_from")
        or str(row.get("open_from"))[:10] <= day_s
    ]

    completion_by_course = _rows(
        store,
        f"""
        SELECT c.course_id,c.name AS course_name,
               SUM(CASE WHEN a.completion='y' THEN 1 ELSE 0 END) AS done,
               SUM(CASE WHEN a.completion='n' THEN 1 ELSE 0 END) AS incomplete,
               SUM(CASE WHEN a.completion IS NULL THEN 1 ELSE 0 END) AS untracked,
               SUM(CASE WHEN a.completion IS NULL
                         AND (v.progress_pct IS NOT NULL OR s.submitted IS NOT NULL)
                        THEN 1 ELSE 0 END) AS alternate_state,
               SUM(CASE WHEN a.completion IS NULL
                         AND v.progress_pct IS NULL AND s.submitted IS NULL
                        THEN 1 ELSE 0 END) AS no_state,
               COUNT(*) AS total,
               SUM(CASE WHEN a.completion='y' OR v.progress_pct>=100 OR s.submitted=1
                        THEN 1 ELSE 0 END) AS effective_done,
               SUM(CASE WHEN a.completion='n' OR (v.progress_pct IS NOT NULL AND v.progress_pct<100)
                              OR s.submitted=0 THEN 1 ELSE 0 END) AS effective_incomplete
          FROM course c
          JOIN activity a ON a.course_id=c.course_id
          LEFT JOIN vod v ON v.cmid=a.cmid
          LEFT JOIN submission s ON s.cmid=a.cmid
         WHERE {course_sql}
         GROUP BY c.course_id,c.name
         ORDER BY c.name
        """,
        course_args,
    )
    untracked = _rows(
        store,
        f"""
        SELECT c.name AS course_name,a.modname,COUNT(*) AS count
              ,SUM(CASE WHEN v.progress_pct IS NOT NULL OR s.submitted IS NOT NULL
                        THEN 1 ELSE 0 END) AS alternate_state
          FROM activity a
          JOIN course c ON c.course_id=a.course_id
          LEFT JOIN vod v ON v.cmid=a.cmid
          LEFT JOIN submission s ON s.cmid=a.cmid
         WHERE {course_sql} AND a.completion IS NULL
         GROUP BY c.course_id,c.name,a.modname
         ORDER BY c.name,a.modname
        """,
        course_args,
    )

    return DailyReport(
        target=target,
        year=year,
        semester=semester,
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S KST"),
        source_updated_at=freshness,
        stale=stale,
        opened=opened,
        completed=completed,
        new_posts=new_posts,
        new_files=new_files,
        updates=updates,
        todos=todos,
        today_schedule=today_schedule,
        assignments=assignments,
        viewing_queue=viewing,
        attendance=attendance,
        completion_by_course=completion_by_course,
        untracked=untracked,
    )


def _status(row: dict) -> str:
    if row.get("progress_pct") is not None:
        return f"진도 {row['progress_pct']:g}%"
    if row.get("submitted") is not None:
        return "제출함" if row["submitted"] else "미제출"
    return {"y": "완료", "n": "미완료"}.get(row.get("completion"), "완료추적 없음")


def render_report(report: DailyReport) -> str:
    urgent = sum(
        r.get("urgency") in {"오늘 마감", "긴급", "지각 가능"}
        for r in report.todos
    )
    video_completed = [r for r in report.completed if r.get("kind") == "vod_completed"]
    submission_completed = [
        r for r in report.completed if r.get("kind") == "submission_completed"
    ]
    other_completed = [
        r for r in report.completed
        if r not in video_completed and r not in submission_completed
    ]
    lines = [
        f"[yonstudy 일일 리포트] {report.target.isoformat()} (KST)",
        f"대상: {report.year} {report.semester}",
        f"생성: {report.generated_at}",
        f"데이터 최종 동기화: {report.source_updated_at or '없음'}",
        f"요약: 할 일 {len(report.todos)} (긴급/지각 {urgent}) · "
        f"오늘 일정 {len(report.today_schedule)} · 미제출 과제 {len(report.assignments)} · "
        f"시청 대기 {len(report.viewing_queue)} · "
        f"새 글 {len(report.new_posts)} · 새 자료 {len(report.new_files)} · "
        f"영상 출석 완료 {len(video_completed)} · 제출 완료 {len(submission_completed)} · "
        f"자료·기타 완료 표시 {len(other_completed)}",
    ]
    if report.stale:
        lines += [
            "",
            "⚠ 오늘 동기화된 데이터가 아닙니다. 아래의 '오늘' 결과는 확정값이 아닙니다.",
            "  백그라운드 동기화가 완료되면 다음 리포트에 자동 반영됩니다.",
        ]

    lines += ["", f"오늘 공개된 항목 ({len(report.opened)})"]
    lines += [
        f"- [{r['course_name']}] {r['title']} · {_status(r)} · {r['open_from']}"
        for r in report.opened
    ] or ["- 없음"]

    lines += ["", f"오늘 동기화에서 영상 출석 완료 ({len(video_completed)})"]
    lines += [
        f"- [{r['course_name']}] {r['title']} · {r['at']}"
        for r in video_completed
    ] or ["- 없음"]

    lines += ["", f"오늘 동기화에서 제출 완료 ({len(submission_completed)})"]
    lines += [
        f"- [{r['course_name']}] {r['title']} · {r['at']}"
        for r in submission_completed
    ] or ["- 없음"]

    lines += ["", f"자료·기타 완료 표시 ({len(other_completed)})"]
    lines += [
        f"- [{r['course_name']}] {r['title']} · {r.get('modname') or '활동'} · {r['at']}"
        for r in other_completed
    ] or ["- 없음"]

    lines += ["", f"오늘 일정 ({len(report.today_schedule)})"]
    lines += [
        f"- [{r['schedule_kind']}] [{r['course_name']}] {r['title']}"
        + (f" · 정상 마감 {r['normal_deadline']}" if r.get("normal_deadline") else "")
        + (f" · {r['url']}" if r.get("url") else "")
        for r in report.today_schedule
    ] or ["- 없음"]

    lines += ["", f"제출할 과제 ({len(report.assignments)})"]
    lines += [
        f"- [{r['urgency']}] [{r['course_name']}] {r['title']}"
        + (f" · 마감 {r['normal_deadline']}" if r.get("normal_deadline") else "")
        + (f" · {r['url']}" if r.get("url") else "")
        for r in report.assignments
    ] or ["- 없음"]

    lines += ["", f"과목별 순차 시청 목록 ({len(report.viewing_queue)})"]
    lines += [
        f"- [{r['course_name']}] {r.get('section_name') or str(r.get('section_idx') or 0) + '주차'} · "
        f"{r['title']} · {r['period']} · 진도 {r.get('progress_pct') or 0:g}%"
        + (f" · 남은 {r['remaining_minutes']}분" if r.get("remaining_minutes") is not None else "")
        + (f" · 정상 마감 {str(r['open_to'])[:10]}" if r.get("open_to") else "")
        + (f" · {r['url']}" if r.get("url") else "")
        for r in report.viewing_queue
    ] or ["- 없음"]

    lines += ["", f"온라인출석부 확인 ({len(report.attendance)})"]
    lines += [
        f"- [{'정상' if r['verified'] else '확인 필요'}] [{r['course_name']}] {r['title']} · "
        f"콘텐츠 {r.get('duration_label') or '-'} · 최대 학습위치 "
        f"{r.get('max_position_label') or '-'} · 진도 {r.get('progress_pct') or 0:g}%"
        for r in report.attendance
    ] or ["- 없음"]

    lines += ["", f"새 Q&A·공지·게시글 ({len(report.new_posts)})"]
    lines += [
        f"- [{r['course_name']}] [{r['category']} · {r.get('board_name') or '게시판'}] "
        f"{r['title']} · {r.get('writer') or '작성자 미상'} · {r.get('written_at') or ''}"
        + (f"\n  {r['excerpt']}" if r.get("excerpt") else "")
        + (f"\n  {r['url']}" if r.get("url") else "")
        for r in report.new_posts
    ] or ["- 없음"]

    lines += ["", f"새 강의자료·첨부 ({len(report.new_files)})"]
    lines += [
        f"- [{r['course_name']}] [{_file_category(r.get('role'))} · "
        f"{r.get('board_name') or r.get('activity_title') or '활동명 없음'}] {r['name']}"
        + (f" · {r['bytes']/1024/1024:.1f}MB" if r.get("bytes") else "")
        + (f" · {r['url']}" if r.get("url") else "")
        for r in report.new_files
    ] or ["- 없음"]

    lines += ["", f"새 활동·기간 변경 ({len(report.updates)})"]
    lines += [
        f"- [{r['course_name']}] {r['title']} · {r['kind']}"
        + (f" · {r['url']}" if r.get("url") else "")
        for r in report.updates
    ] or ["- 없음"]

    lines += ["", f"확인할 일 ({len(report.todos)})"]
    lines += [
        f"- [{r['urgency']}] [{r['course_name']}] {r['title']} · {_status(r)}"
        + (f" · 정상 마감 {r['normal_deadline']}" if r.get("normal_deadline") else "")
        + (f" · 지각 마감 {r['final_deadline']}"
           if r.get("final_deadline") and r.get("final_deadline") != r.get("normal_deadline") else "")
        + (f" · 남은 {r['remaining_minutes']}분/{r['eta_minutes']}분 예상"
           if r.get("remaining_minutes") is not None else "")
        + (f" · 권장 처리일 {r['scheduled_for']}" if r.get("actionable") else " · 처리기간 종료")
        + (f" · {r['url']}" if r.get("url") else "")
        for r in report.todos
    ] or ["- 없음"]

    lines += ["", "과목별 진행 현황"]
    lines += [
        f"- {r['course_name']}: 확인된 완료 {r['effective_done']}, "
        f"미완료 {r['effective_incomplete']}, 완료 체크 {r['done']}, "
        f"체크표시 없음 {r['untracked']} (별도 진도/제출 상태 있음 {r['alternate_state']}, "
        f"판단 신호 없음 {r['no_state']}) / 전체 {r['total']}"
        for r in report.completion_by_course
    ] or ["- 대상 학기 강좌 데이터 없음"]
    if report.untracked:
        lines += ["", "표시 없음 상세 (LearnUs가 완료 추적 신호를 주지 않은 활동)"]
        lines += [
            f"- [{r['course_name']}] {r['modname']} {r['count']}개"
            + (f" (별도 상태 확인 가능 {r['alternate_state']}개)" if r['alternate_state'] else "")
            for r in report.untracked
        ]

    return "\n".join(lines) + "\n"


def report_as_json(report: DailyReport) -> str:
    payload = dict(report.__dict__)
    payload["target"] = report.target.isoformat()
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _email_completion_groups(report: DailyReport) -> tuple[list[dict], list[dict], list[dict]]:
    videos = [r for r in report.completed if r.get("kind") == "vod_completed"]
    submissions = [r for r in report.completed if r.get("kind") == "submission_completed"]
    others = [r for r in report.completed if r not in videos and r not in submissions]
    return videos, submissions, others


def render_email_text(report: DailyReport) -> str:
    """메일 클라이언트가 HTML을 지원하지 않을 때 보여 줄 간결한 대체 본문."""
    videos, submissions, others = _email_completion_groups(report)
    lines = [
        f"{report.target.month}월 {report.target.day}일 런어스 요약",
        f"{report.year}년 {report.semester}",
        "",
        f"오늘 일정 {len(report.today_schedule)}개 · 미제출 과제 {len(report.assignments)}개 · "
        f"볼 영상 {len(report.viewing_queue)}개 · 새 소식 {len(report.new_posts) + len(report.new_files)}개",
    ]
    if report.stale:
        lines += ["", "주의: 오늘 자료를 아직 모두 확인하지 못해 내용이 달라질 수 있습니다."]

    if report.today_schedule or report.assignments or report.viewing_queue:
        lines += ["", "해야 할 일"]
        for row in report.assignments:
            lines.append(
                f"- 과제 · {row['course_name']} · {row['title']}"
                + f" · {_deadline_summary(row)}"
            )
        todo_by_cmid = {r["cmid"]: r for r in report.todos}
        for row in report.viewing_queue:
            todo = todo_by_cmid.get(row["cmid"], row)
            recommended = _format_datetime_ko(todo.get("scheduled_for"))
            lines.append(
                f"- 영상 · {row['course_name']} · {row['title']} · 현재 {row.get('progress_pct') or 0:g}%"
                + (f" · 권장 수강일 {recommended}" if recommended else "")
                + (f" · {_deadline_summary(todo)}" if todo.get("open_to") else "")
            )
        assignment_ids = {r["cmid"] for r in report.assignments}
        viewing_ids = {r["cmid"] for r in report.viewing_queue}
        for row in report.today_schedule:
            if row["cmid"] not in assignment_ids | viewing_ids:
                lines.append(f"- 일정 · {row['course_name']} · {row['title']}")
    else:
        lines += ["", "현재 확인된 미제출 과제나 미수강 영상은 없습니다."]

    if videos or submissions or others:
        lines += ["", "오늘 확인된 완료"]
        lines += [f"- 영상 출석 · {r['course_name']} · {r['title']}" for r in videos]
        lines += [f"- 과제 제출 · {r['course_name']} · {r['title']}" for r in submissions]
        lines += [f"- 자료 확인 · {r['course_name']} · {r['title']}" for r in others]

    if report.attendance:
        lines += ["", "동영상 수강 현황"]
        lines += [
            f"- {_attendance_status(r)[0]} · {r['course_name']} · {r['title']} · "
            f"마지막 재생 {r.get('max_position_label') or '-'} / 전체 {r.get('duration_label') or '-'} · "
            f"진도 {r.get('progress_pct') or 0:g}%"
            for r in report.attendance
        ]

    if report.new_posts:
        lines += ["", "새 공지·Q&A"]
        lines += [
            f"- {r['course_name']} · {r['category']} · {r['title']}\n  {r.get('url') or ''}"
            for r in report.new_posts
        ]
    if report.new_files:
        lines += ["", "새 강의자료·첨부"]
        lines += [
            f"- {r['course_name']} · {_file_category(r.get('role'))} · {r['name']}\n  {r.get('url') or ''}"
            for r in report.new_files
        ]

    lines += ["", f"마지막 확인: {_format_datetime_ko(report.source_updated_at, include_year=True) or '확인 기록 없음'}"]
    return "\n".join(lines) + "\n"


def render_report_html(report: DailyReport) -> str:
    """Gmail 등에서 바로 읽기 좋은 단일 열 HTML 브리핑."""
    esc = lambda value: html.escape(str(value or ""), quote=True)
    videos, submissions, others = _email_completion_groups(report)
    attendance_ok = sum(bool(r.get("verified")) for r in report.attendance)

    def link(url: str | None, label: str = "LearnUs에서 보기") -> str:
        if not url:
            return ""
        return (
            f'<a href="{esc(url)}" style="color:#2563eb;text-decoration:none;font-weight:600">'
            f"{esc(label)} →</a>"
        )

    def item(title: str, meta: str, detail: str = "", url: str | None = None) -> str:
        detail_html = (
            f'<div style="margin-top:6px;color:#64748b;font-size:13px;line-height:1.55">{esc(detail)}</div>'
            if detail else ""
        )
        link_html = f'<div style="margin-top:8px;font-size:13px">{link(url)}</div>' if url else ""
        return (
            '<div style="padding:14px 0;border-bottom:1px solid #e2e8f0">'
            f'<div style="font-size:12px;color:#64748b;margin-bottom:4px">{esc(meta)}</div>'
            f'<div style="font-size:15px;font-weight:700;color:#0f172a;line-height:1.45">{esc(title)}</div>'
            f"{detail_html}{link_html}</div>"
        )

    def section(title: str, body: str, subtitle: str = "") -> str:
        subtitle_html = (
            f'<div style="font-size:13px;color:#64748b;margin-top:3px">{esc(subtitle)}</div>'
            if subtitle else ""
        )
        return (
            '<tr><td style="padding:0 24px 18px">'
            '<div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:20px">'
            f'<div style="font-size:18px;font-weight:800;color:#0f172a">{esc(title)}</div>'
            f"{subtitle_html}<div style=\"margin-top:10px\">{body}</div></div></td></tr>"
        )

    cards = [
        ("오늘 일정", len(report.today_schedule), "#1d4ed8"),
        ("미제출 과제", len(report.assignments), "#b45309"),
        ("볼 영상", len(report.viewing_queue), "#7c3aed"),
        ("수강 확인", attendance_ok, "#047857"),
    ]
    card_html = "".join(
        '<td width="25%" style="padding:5px;vertical-align:top">'
        '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:14px 8px;text-align:center">'
        f'<div style="font-size:24px;font-weight:800;color:{color}">{count}</div>'
        f'<div style="font-size:12px;color:#64748b;margin-top:3px">{esc(label)}</div></div></td>'
        for label, count, color in cards
    )

    rows: list[str] = []
    if report.stale:
        rows.append(
            '<tr><td style="padding:0 24px 18px"><div style="background:#fff7ed;border:1px solid #fed7aa;'
            'border-radius:12px;padding:14px 16px;color:#9a3412;font-size:13px;line-height:1.55">'
            '<strong>오늘 자료를 아직 모두 확인하지 못했습니다.</strong><br>'
            '확인이 끝나면 다음 메일에 최신 내용이 반영됩니다.</div></td></tr>'
        )

    todo_parts: list[str] = []
    todo_by_cmid = {r["cmid"]: r for r in report.todos}
    for r in report.assignments:
        todo_parts.append(item(r["title"], f"{r['course_name']} · 과제", _deadline_summary(r), r.get("url")))
    for r in report.viewing_queue:
        todo = todo_by_cmid.get(r["cmid"], r)
        detail = f"현재 {r.get('progress_pct') or 0:g}%"
        if r.get("remaining_minutes") is not None:
            detail += f" · 약 {r['remaining_minutes']}분 남음"
        recommended = _format_datetime_ko(todo.get("scheduled_for"))
        if recommended:
            detail += f" · 권장 수강일 {recommended}"
        if todo.get("open_to"):
            detail += f" · {_deadline_summary(todo)}"
        todo_parts.append(item(r["title"], f"{r['course_name']} · 시청 대기", detail, r.get("url")))
    used_ids = {r["cmid"] for r in report.assignments + report.viewing_queue}
    for r in report.today_schedule:
        if r["cmid"] not in used_ids:
            todo_parts.append(item(r["title"], f"{r['course_name']} · {r['schedule_kind']}", url=r.get("url")))
    if todo_parts:
        rows.append(section("해야 할 일", "".join(todo_parts)))
    else:
        rows.append(
            section(
                "해야 할 일",
                '<div style="padding:8px 0;color:#475569;font-size:14px">현재 확인된 미제출 과제나 미수강 영상이 없습니다.</div>',
            )
        )

    attendance_parts = []
    for r in report.attendance:
        status, color = _attendance_status(r)
        attendance_parts.append(
            '<div style="padding:14px 0;border-bottom:1px solid #e2e8f0">'
            f'<div style="font-size:12px;color:#64748b">{esc(r["course_name"])}</div>'
            f'<div style="font-size:15px;font-weight:700;color:#0f172a;margin-top:4px">{esc(r["title"])}</div>'
            f'<div style="margin-top:7px;font-size:13px;color:#475569">마지막 재생 위치 '
            f'{esc(r.get("max_position_label") or "-")} / 전체 {esc(r.get("duration_label") or "-")} · '
            f'진도 {r.get("progress_pct") or 0:g}% · <strong style="color:{color}">{status}</strong></div>'
            f'<div style="margin-top:8px;font-size:13px">{link(r.get("url"))}</div></div>'
        )
    if attendance_parts:
        rows.append(section("동영상 수강 현황", "".join(attendance_parts), "진도율과 마지막 재생 위치를 함께 확인한 결과입니다."))

    completed_parts = [item(r["title"], f"{r['course_name']} · 영상 출석 완료", url=r.get("url")) for r in videos]
    completed_parts += [item(r["title"], f"{r['course_name']} · 과제 제출 완료", url=r.get("url")) for r in submissions]
    completed_parts += [item(r["title"], f"{r['course_name']} · 자료 열람 처리", url=r.get("url")) for r in others]
    if completed_parts:
        rows.append(section("오늘 확인된 완료", "".join(completed_parts)))

    post_parts = [
        item(
            r["title"],
            f"{r['course_name']} · {r['category']} · {r.get('board_name') or '게시판'}",
            r.get("excerpt") or "",
            r.get("url"),
        )
        for r in report.new_posts
    ]
    if post_parts:
        rows.append(section("새 공지·Q&A", "".join(post_parts)))

    file_parts = []
    for r in report.new_files:
        context = r.get("board_name") or r.get("activity_title") or ""
        size = f"{r['bytes']/1024/1024:.1f}MB" if r.get("bytes") else "크기 미확인"
        file_parts.append(
            item(
                r["name"],
                f"{r['course_name']} · {_file_category(r.get('role'))}" + (f" · {context}" if context else ""),
                size,
                r.get("url"),
            )
        )
    if file_parts:
        rows.append(section("새 강의자료·첨부", "".join(file_parts)))

    covered_ids = {
        r.get("cmid") for r in (
            report.new_posts + report.new_files + report.todos + report.opened + report.completed
        )
    }
    update_labels = {
        "vod": "동영상",
        "assign": "과제",
        "quiz": "퀴즈",
        "ubfile": "강의자료",
        "resource": "강의자료",
        "url": "링크",
    }
    important_updates = [
        r for r in report.updates
        if r.get("cmid") not in covered_ids
        and (r.get("kind") == "activity_updated" or r.get("modname") in update_labels)
    ]
    if important_updates:
        update_parts = [
            item(
                r["title"],
                f"{r['course_name']} · {update_labels.get(r.get('modname'), '변경 사항')}",
                "기존 항목의 내용이나 이용 기간이 바뀌었습니다."
                if r.get("kind") == "activity_updated" else "새로 올라온 항목입니다.",
                r.get("url"),
            )
            for r in important_updates
        ]
        rows.append(section("새로 확인된 변경 사항", "".join(update_parts)))

    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans KR',Arial,sans-serif;color:#0f172a">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f1f5f9"><tr><td align="center" style="padding:24px 10px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:680px">
<tr><td style="padding:26px 24px;background:#172554;border-radius:16px 16px 0 0;color:#ffffff">
  <div style="font-size:12px;letter-spacing:.08em;color:#bfdbfe;font-weight:700">YONSTUDY</div>
  <div style="font-size:26px;line-height:1.3;font-weight:800;margin-top:7px">{report.target.month}월 {report.target.day}일 런어스 요약</div>
  <div style="font-size:13px;color:#cbd5e1;margin-top:7px">{esc(report.year)}년 {esc(report.semester)}</div>
</td></tr>
<tr><td style="padding:18px 19px 13px;background:#ffffff"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>{card_html}</tr></table></td></tr>
<tr><td style="height:18px"></td></tr>
{''.join(rows)}
<tr><td style="padding:2px 24px 28px;text-align:center;color:#94a3b8;font-size:11px;line-height:1.6">
마지막 확인: {esc(_format_datetime_ko(report.source_updated_at, include_year=True) or '확인 기록 없음')}<br>런어스에서 확인한 내용을 자동으로 정리해 보냈습니다.
</td></tr></table></td></tr></table></body></html>'''


def send_report(
    body: str,
    *,
    to: str,
    subject: str,
    sender: str | None = None,
    html_body: str | None = None,
) -> None:
    """SMTP 환경변수가 있으면 SMTP, 아니면 로컬 sendmail을 사용한다.

    YONSTUDY_SMTP_HOST/PORT/USER/PASSWORD/STARTTLS를 지원한다. 비밀번호는
    파일이나 DB에 저장하지 않는다.
    """
    sender = sender or os.environ.get("YONSTUDY_MAIL_FROM") or "yonstudy@localhost"
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    host = os.environ.get("YONSTUDY_SMTP_HOST")
    if host:
        port = int(os.environ.get("YONSTUDY_SMTP_PORT", "587"))
        username = os.environ.get("YONSTUDY_SMTP_USER")
        password = os.environ.get("YONSTUDY_SMTP_PASSWORD")
        starttls = os.environ.get("YONSTUDY_SMTP_STARTTLS", "1").lower() not in {
            "0", "false", "no"
        }
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if starttls:
                smtp.starttls()
            if username:
                if password is None:
                    raise RuntimeError("YONSTUDY_SMTP_PASSWORD가 설정되지 않았습니다")
                smtp.login(username, password)
            smtp.send_message(msg)
        return

    sendmail = Path("/usr/sbin/sendmail")
    if not sendmail.exists():
        raise RuntimeError("SMTP 설정도 /usr/sbin/sendmail도 없습니다")
    proc = subprocess.run(
        [str(sendmail), "-t", "-oi"],
        input=msg.as_bytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if proc.returncode:
        error = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"sendmail 실패({proc.returncode}): {error}")
