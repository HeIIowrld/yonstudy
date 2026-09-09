"""일일 동기화, 원격 백업, 메일 리포트를 한 번에 실행한다."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .archive import Archiver, SessionExpired
from .auth import ensure_session
from .client import LearnUsClient
from .daily import (
    SEOUL, build_daily_report, current_term, render_email_text,
    render_report, render_report_html, send_report,
)
from .remote import RcloneRemote, sync_remote_tree
from .store import Store
from .video_archive import archive_course_vods


def run_daily_automation(
    *,
    store_path: str,
    cookie_path: str,
    export_dir: str,
    remote: str,
    sync: bool = True,
    dry_run: bool = False,
    send_mail: bool = True,
    export_onedrive: bool | None = None,
    upload_remote: bool | None = None,
) -> tuple[int, dict]:
    if upload_remote is None:
        upload_remote = True if export_onedrive is None else export_onedrive

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
        "upload": {},
        "video_archive": {"status": "skipped"},
        "mail": {"status": "skipped"},
    }
    exit_code = 0
    sink = None
    client = None
    sink_error = None
    if upload_remote and not dry_run:
        try:
            sink = RcloneRemote(remote)
        except Exception as exc:
            sink_error = str(exc)
            exit_code = 1

    if sync and not dry_run:
        try:
            client = LearnUsClient(cookie_path)
            auth = ensure_session(
                client, state_path=Path(store_path) / "auth_state.json"
            )
            arc = Archiver(client, store, file_sink=sink)
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
                        fetch_subtitles=True,
                        # remote 연결이 없으면 첨부파일은 다음 실행에서 다시 받는다.
                        fetch_files=sink is not None,
                        fetch_boards=True,
                        board_pages=0,
                    )
                except SessionExpired as exc:
                    errors.append({"course": course.title, "error": str(exc)})
                    break
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

    if upload_remote:
        state["export"] = {
            "status": "not_used",
            "mode": "direct-no-local-staging",
            "legacy_destination": export_dir,
        }
        if dry_run:
            state["upload"] = {"status": "dry_run", "remote": remote}
        elif sink is None:
            state["upload"] = {
                "status": "error", "remote": remote,
                "message": sink_error or "원격 저장소를 초기화하지 못했습니다",
            }
        else:
            try:
                direct = sync_remote_tree(store, sink, year=year, semester=semester)
                state["upload"] = {"status": "ok", **direct.__dict__}
            except Exception as exc:
                state["upload"] = {
                    "status": "error", "remote": remote, "message": str(exc),
                }
                exit_code = 1
            try:
                raw_limit = os.environ.get(
                    "YONSTUDY_VIDEO_ARCHIVE_LIMIT",
                    os.environ.get("YONSTUDY_ARCHIVE_ONLY_LIMIT", "0"),
                )
                configured_limit = int(raw_limit)
                archive_limit = configured_limit if configured_limit > 0 else None
                archived = archive_course_vods(
                    store, sink, year=year, semester=semester,
                    limit=archive_limit, client=client,
                )
                state["video_archive"] = {"status": "ok", **asdict(archived)}
                if archived.failed_files:
                    exit_code = 1
            except Exception as exc:
                state["video_archive"] = {
                    "status": "error", "message": str(exc),
                }
                exit_code = 1
    else:
        state["export"] = {
            "status": "disabled",
            "message": "로컬 대체 저장 없이 파일 수집을 생략",
        }
        state["upload"] = {
            "status": "disabled",
            "remote": remote,
            "message": "원격 저장 비활성화",
        }
        state["video_archive"] = {
            "status": "disabled",
            "message": "원격 저장 비활성화",
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
    # 2026-09 이전 상태 파일을 읽는 스크립트를 위한 호환 키다.
    state["onedrive"] = state["upload"]
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
    """현재 학기의 미완료 또는 완료 비추적 영상을 한 번에 소량 처리한다.

    systemd의 RandomizedDelaySec는 서버 부하와 DB 작업 충돌을 분산하는 용도다.
    여기서는 정상 마감(open_to)이 지난 영상은 지각기간이 남아 있어도 자동 재생하지
    않고, 실제 재생 뒤 LearnUs 진도 리포트를 다시 읽어 진도율과 최대 학습 위치를
    함께 검증한다.
    """
    from .autoplay import build_plan, run_plan
    from .progress import progress_verified

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
        "SELECT course_id FROM course WHERE year=? AND semester=? AND enrolled=1",
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

    plan = build_plan(
        store, course_ids=course_ids, include_untracked_once=True
    )
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
        plan = build_plan(
            store, course_ids=course_ids, include_untracked_once=True
        )

    # 직전 후보가 갱신 결과 기간 밖으로 바뀌면 성공한 재생으로 기록하지 않는다.
    if not plan:
        return finish(0, "nothing_to_watch")

    selected = plan[: max(1, limit)]
    state["jobs"] = [
        {
            "cmid": job.cmid,
            "course": job.course_name,
            "title": job.title,
            "remaining_minutes": (job.remaining_sec + 59) // 60,
            "rate": job.rate,
            "normal_deadline": job.open_to,
            "mode": "progress" if job.tracks_progress else "playback_once",
        }
        for job in selected
    ]
    if dry_run:
        run_plan(selected, cookie_path, dry_run=True)
        return finish(0, "dry_run")

    playback_results: dict[int, bool] = {}

    def record_result(job, ok: bool, note: str) -> None:
        playback_results[job.cmid] = ok
        kind = "watch" if job.tracks_progress else "playback_once"
        store.log(kind, str(job.cmid), ok, note)
        store.commit()

    code = run_plan(selected, cookie_path, on_result=record_result)
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
            SELECT v.progress_pct,v.watched_sec,v.duration_sec,a.completion
              FROM vod v JOIN activity a ON a.cmid=v.cmid
             WHERE v.cmid=?
            """,
            (job.cmid,),
        )
        row = dict(rows[0]) if rows else {}
        if job.tracks_progress:
            ok = bool(
                progress_verified(
                    row.get("progress_pct"),
                    row.get("watched_sec"),
                    row.get("duration_sec"),
                )
                or row.get("completion") == "y"
            )
            verification_source = "learnus_progress"
        else:
            ok = bool(playback_results.get(job.cmid))
            verification_source = "local_playback_once"
        verified = verified and ok
        state["verification"].append({
            "cmid": job.cmid,
            "ok": ok,
            "source": verification_source,
            **row,
        })
    return finish(0 if verified else 1, "verified" if verified else "verification_failed")
