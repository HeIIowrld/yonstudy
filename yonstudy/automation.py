"""일일 동기화 → 로컬 내보내기 → OneDrive 업로드 → 메일 리포트."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .archive import Archiver
from .auth import ensure_session
from .client import LearnUsClient
from .daily import (
    SEOUL, build_daily_report, current_term, render_email_text,
    render_report, render_report_html, send_report,
)
from .export import export_onedrive_tree
from .store import Store


def _upload_with_rclone(source: Path, remote: str, dry_run: bool = False) -> dict:
    exe = shutil.which("rclone")
    if not exe:
        return {"status": "not_installed", "remote": remote}
    remote_name = remote.split(":", 1)[0] + ":"
    try:
        listed = subprocess.run(
            [exe, "listremotes"], capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "remote": remote, "output": "rclone listremotes timeout"}
    remotes = set(listed.stdout.splitlines()) if listed.returncode == 0 else set()
    if remote_name not in remotes:
        return {"status": "not_configured", "remote": remote}

    cmd = [
        exe,
        "copy",
        str(source),
        remote,
        "--create-empty-src-dirs",
        "--fast-list",
        "--checkers=8",
        "--transfers=4",
        "--stats-one-line",
    ]
    if dry_run:
        cmd.append("--dry-run")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=6 * 3600, check=False
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "remote": remote, "output": "rclone copy timeout"}
    return {
        "status": "ok" if proc.returncode == 0 else "error",
        "remote": remote,
        "returncode": proc.returncode,
        "output": (proc.stdout + "\n" + proc.stderr).strip()[-4000:],
    }


def run_daily_automation(
    *,
    store_path: str,
    cookie_path: str,
    export_dir: str,
    remote: str,
    sync: bool = True,
    dry_run: bool = False,
    send_mail: bool = True,
    export_onedrive: bool = True,
) -> tuple[int, dict]:
    now = datetime.now(SEOUL)
    target = now.date()
    year, semester = current_term(target)
    store = Store(store_path)
    state: dict = {
        "started_at": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "year": year,
        "semester": semester,
        "sync": {"status": "skipped"},
        "export": {},
        "onedrive": {},
        "mail": {"status": "skipped"},
    }
    exit_code = 0

    if sync and not dry_run:
        try:
            client = LearnUsClient(cookie_path)
            auth = ensure_session(
                client, state_path=Path(store_path) / "auth_state.json"
            )
            arc = Archiver(client, store)
            courses = [
                c for c in arc.sync_courses()
                if c.year == year and c.semester == semester
            ]
            errors = []
            for course in courses:
                try:
                    arc.sync_course(
                        course,
                        probe_vod=True,
                        fetch_subtitles=False,
                        fetch_files=True,
                        fetch_boards=True,
                        board_pages=3,
                    )
                except Exception as exc:
                    errors.append({"course": course.title, "error": str(exc)})
            store.commit()
            state["sync"] = {
                "status": "ok" if not errors else "partial",
                "courses": len(courses),
                "relogged": auth.relogged,
                "errors": errors,
            }
            if errors:
                exit_code = 1
        except Exception as exc:
            state["sync"] = {"status": "error", "message": str(exc)}
            exit_code = 1

    if export_onedrive:
        exported = export_onedrive_tree(
            store,
            export_dir,
            year=year,
            semester=semester,
            dry_run=dry_run,
        )
        state["export"] = dict(exported.__dict__)
        state["onedrive"] = (
            {"status": "dry_run", "remote": remote}
            if dry_run
            else _upload_with_rclone(Path(export_dir), remote)
        )
        if state["onedrive"]["status"] == "error":
            exit_code = 1
    else:
        state["export"] = {
            "status": "disabled",
            "message": "OneDrive 안정화 전 로컬 내보내기 보류",
        }
        state["onedrive"] = {
            "status": "disabled",
            "remote": remote,
            "message": "OneDrive 안정화 전 rclone 업로드 보류",
        }

    report = build_daily_report(store, target=target, year=year, semester=semester)
    body = render_report(report)
    report_dir = Path(store_path) / "reports"
    if not dry_run:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{target.isoformat()}.txt").write_text(body, encoding="utf-8")

    recipient = os.environ.get("YONSTUDY_REPORT_TO")
    smtp_host = os.environ.get("YONSTUDY_SMTP_HOST")
    smtp_user = os.environ.get("YONSTUDY_SMTP_USER")
    smtp_password = os.environ.get("YONSTUDY_SMTP_PASSWORD")
    smtp_ready = bool(recipient and smtp_host and (not smtp_user or smtp_password))
    if not send_mail:
        state["mail"] = {
            "status": "skipped",
            "to": recipient,
            "message": "09:00 전용 리포트 타이머에서 발송",
        }
    elif dry_run:
        state["mail"] = {"status": "dry_run", "to": recipient}
    elif smtp_ready:
        try:
            send_report(
                render_email_text(report),
                html_body=render_report_html(report),
                to=recipient,
                subject=f"[yonstudy] {target.month}월 {target.day}일 런어스 요약",
            )
            state["mail"] = {"status": "sent", "to": recipient}
        except Exception as exc:
            state["mail"] = {"status": "error", "to": recipient, "error": str(exc)}
            exit_code = 1
    else:
        state["mail"] = {
            "status": "not_configured",
            "to": recipient,
            "message": "SMTP 호스트·사용자·앱 비밀번호 설정 필요",
        }

    state["finished_at"] = datetime.now(SEOUL).strftime("%Y-%m-%dT%H:%M:%S%z")
    if not dry_run:
        Path(store_path, "automation_state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return exit_code, state


def run_scheduled_watch(
    *,
    store_path: str,
    cookie_path: str,
    limit: int = 1,
    dry_run: bool = False,
) -> tuple[int, dict]:
    """현재 학기의 정상 수강기간 내 미완료 영상을 한 번에 소량 처리한다.

    systemd의 RandomizedDelaySec는 서버 부하와 DB 작업 충돌을 분산하는 용도다.
    여기서는 정상 마감(open_to)이 지난 영상은 지각기간이 남아 있어도 자동 재생하지
    않고, 실제 재생 뒤 LearnUs 진도 리포트를 다시 읽어 100% 반영까지 검증한다.
    """
    from .autoplay import build_plan, run_plan

    now = datetime.now(SEOUL)
    target = now.date()
    year, semester = current_term(target)
    store = Store(store_path)
    state: dict = {
        "started_at": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "year": year,
        "semester": semester,
        "status": "starting",
        "jobs": [],
        "verification": [],
    }
    state_path = Path(store_path, "watch_state.json")

    def finish(code: int, status: str) -> tuple[int, dict]:
        state["status"] = status
        state["finished_at"] = datetime.now(SEOUL).strftime("%Y-%m-%dT%H:%M:%S%z")
        if not dry_run:
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return code, state

    course_rows = store.query(
        "SELECT course_id FROM course WHERE year=? AND semester=?",
        (year, semester),
    )
    course_ids = {int(r["course_id"]) for r in course_rows}
    courses_by_id = {}
    archiver = None

    if not dry_run:
        try:
            client = LearnUsClient(cookie_path)
            alive, _ = client.session_info()
            if not alive:
                state["message"] = "python3 cli.py login 실행 필요"
                return finish(1, "login_required")
            archiver = Archiver(client, store)
            current_courses = [
                c for c in archiver.sync_courses()
                if c.year == year and c.semester == semester
            ]
            courses_by_id = {c.course_id: c for c in current_courses}
            course_ids = set(courses_by_id)
        except Exception as exc:
            state["error"] = str(exc)
            return finish(1, "sync_error")

    plan = build_plan(store, course_ids=course_ids)
    if not plan:
        return finish(0, "nothing_to_watch")

    # 실행 직전에 후보 과목 하나만 새로 읽어 저장된 진도기간/진도를 갱신한다.
    # 매번 전체 학기의 모든 VOD를 조회하면 학기 후반 요청량이 지나치게 커진다.
    candidate_ids = {job.course_id for job in plan[: max(1, limit)]}
    if archiver is not None:
        try:
            for course_id in sorted(candidate_ids):
                course = courses_by_id.get(course_id)
                if course:
                    archiver.sync_course(
                        course,
                        probe_vod=True,
                        fetch_subtitles=False,
                        fetch_files=False,
                        fetch_boards=False,
                    )
            store.commit()
        except Exception as exc:
            state["error"] = str(exc)
            return finish(1, "refresh_error")
        plan = build_plan(store, course_ids=course_ids)

    selected = plan[: max(1, limit)]
    state["jobs"] = [
        {
            "cmid": job.cmid,
            "course": job.course_name,
            "title": job.title,
            "remaining_minutes": (job.remaining_sec + 59) // 60,
            "rate": job.rate,
            "normal_deadline": job.open_to,
        }
        for job in selected
    ]
    if dry_run:
        run_plan(selected, cookie_path, dry_run=True)
        return finish(0, "dry_run")

    code = run_plan(selected, cookie_path)
    if code != 0:
        return finish(code, "playback_error")

    # 재생 종료가 곧 출석 반영을 뜻하지 않으므로 대상 과목의 진도 리포트를 재조회한다.
    try:
        for course_id in sorted({job.course_id for job in selected}):
            course = courses_by_id.get(course_id)
            if course:
                archiver.sync_course(
                    course,
                    probe_vod=False,
                    fetch_subtitles=False,
                    fetch_files=False,
                    fetch_boards=False,
                )
        store.commit()
    except Exception as exc:
        state["error"] = str(exc)
        return finish(1, "verification_sync_error")

    verified = True
    for job in selected:
        rows = store.query(
            """
            SELECT v.progress_pct,v.watched_sec,a.completion
              FROM vod v JOIN activity a ON a.cmid=v.cmid
             WHERE v.cmid=?
            """,
            (job.cmid,),
        )
        row = dict(rows[0]) if rows else {}
        ok = bool((row.get("progress_pct") or 0) >= 100 or row.get("completion") == "y")
        verified = verified and ok
        state["verification"].append({"cmid": job.cmid, "ok": ok, **row})
    return finish(0 if verified else 1, "verified" if verified else "verification_failed")
