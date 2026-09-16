"""오늘 마감 과제를 실시간 확인하고 미제출일 때만 추가 메일을 보낸다."""

from __future__ import annotations

import html
import json
import os
from datetime import datetime
from pathlib import Path

from .archive import Archiver
from .auth import ensure_session
from .client import LearnUsClient
from .daily import SEOUL, assignments_due_today, build_daily_report, current_term, send_report
from .store import Store


def refresh_assignments(store, client, *, year: str, semester: str) -> list[dict]:
    """매번 강좌·활동과 제출 페이지를 읽는다. 파일·영상·게시판은 받지 않는다."""
    arc = Archiver(client, store)
    courses = [c for c in arc.sync_courses() if c.year == year and c.semester == semester]
    errors = []
    for course in courses:
        try:
            arc.sync_course(course, assignments_only=True)
        except Exception as exc:
            errors.append({"course_id": course.course_id, "error": str(exc)})
    return errors


def render_reminder(rows: list[dict], *, checked_at: str, errors: list[dict]) -> tuple[str, str]:
    lines = [
        f"오늘 마감 미제출 과제 {len(rows)}개",
        f"LearnUs 제출 상태 확인: {checked_at} (한국 시간)",
        "",
    ]
    for row in rows:
        lines += [
            f"- {row['course_name']} · {row['title']}",
            f"  마감: {row.get('due_at') or row.get('open_to')} · 미제출",
            f"  {row.get('url') or ''}",
        ]
    lines += ["", "오늘 이미 마감 시간이 지난 과제도 포함됩니다."]
    if errors:
        lines.append("일부 과목은 조회에 실패하여 제외했습니다. 이 목록이 전체 미제출 목록은 아닐 수 있습니다.")
    body = "\n".join(lines) + "\n"
    return body, '<html><body><pre style="white-space:pre-wrap;font-family:sans-serif">' + html.escape(body) + '</pre></body></html>'


def run_deadline_reminder(*, store_path: str, cookie_path: str, dry_run: bool = False) -> tuple[int, dict]:
    # cron 외 수동 명령이 겹쳐도 같은 날짜의 메일을 중복 발송하지 않는다.
    import fcntl

    root = Path(store_path)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "deadline_reminder.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _run_deadline_reminder(store_path=store_path, cookie_path=cookie_path, dry_run=dry_run)


def _run_deadline_reminder(*, store_path: str, cookie_path: str, dry_run: bool = False) -> tuple[int, dict]:
    now = datetime.now(SEOUL)
    day = now.date()
    year, semester = current_term(day)
    store = Store(store_path)
    path = Path(store_path) / "deadline_reminder_state.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    state = {
        "date": day.isoformat(), "started_at": now.isoformat(timespec="seconds"),
        "status": "checking", "errors": [], "pending": [],
        "sent_date": previous.get("sent_date"),
    }

    def finish(code: int, status: str) -> tuple[int, dict]:
        state.update(status=status, finished_at=datetime.now(SEOUL).isoformat(timespec="seconds"))
        if not dry_run:
            store.write_json(path.name, state)
        return code, state

    if not dry_run and state["sent_date"] == day.isoformat():
        return finish(0, "already_sent")
    try:
        client = LearnUsClient(cookie_path)
        ensure_session(client, state_path=Path(store_path) / "auth_state.json")
        errors = refresh_assignments(store, client, year=year, semester=semester)
        state["errors"] = errors
        report = build_daily_report(store, target=day, year=year, semester=semester)
        due = assignments_due_today(report)
        failed_courses = {e["course_id"] for e in errors}
        # 조회 실패 시 남아 있는 과거 값을 미제출로 단정하지 않는다.
        pending = [r for r in due if r.get("submitted") == 0 and r["course_id"] not in failed_courses]
        state["pending"] = pending
        state["due_today_count"] = len(due)
        state["unknown_count"] = sum(r.get("submitted") is None for r in due)
        if datetime.now(SEOUL).date() != day:
            return finish(1, "date_changed")
        if not pending:
            return finish(1 if errors or state["unknown_count"] else 0,
                          "check_incomplete" if errors or state["unknown_count"] else "nothing_pending")
        body, html_body = render_reminder(pending, checked_at=report.generated_at, errors=errors)
        if dry_run:
            print(body)
            return finish(0, "dry_run")
        recipient = os.environ.get("YONSTUDY_REPORT_TO")
        if not recipient:
            return finish(1, "mail_not_configured")
        send_report(body, html_body=html_body, to=recipient,
                    subject=f"[yonstudy] {day.month}월 {day.day}일 22시 알림: 오늘 마감 미제출 {len(pending)}개")
        state["sent_date"] = day.isoformat()
        return finish(0, "sent")
    except Exception as exc:
        state["error"] = str(exc)
        return finish(1, "error")
