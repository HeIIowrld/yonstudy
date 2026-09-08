#!/usr/bin/env python3
"""LearnUs 자료와 학습 현황을 정리하는 명령행 도구."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from yonstudy.archive import Archiver, SessionExpired  # noqa: E402
from yonstudy.client import LearnUsClient  # noqa: E402
from yonstudy.store import Store  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent
_LEGACY_COOKIES = Path("/root/ys.learnus.org_cookies.txt")
DEFAULT_COOKIES = os.environ.get(
    "LEARNUS_COOKIES",
    str(
        _LEGACY_COOKIES
        if _LEGACY_COOKIES.exists()
        else PROJECT_ROOT / "store/learnus-cookies.txt"
    ),
)
DEFAULT_STORE = os.environ.get("YONSTUDY_STORE", str(PROJECT_ROOT / "store"))
DEFAULT_EXPORT = os.environ.get(
    "YONSTUDY_EXPORT_DIR",
    os.environ.get("YONSTUDY_ONEDRIVE_LOCAL", str(PROJECT_ROOT / "exports/archive")),
)
DEFAULT_REMOTE = os.environ.get(
    "YONSTUDY_REMOTE",
    os.environ.get("YONSTUDY_ONEDRIVE_REMOTE", "remote:yonstudy"),
)


def get_client(args) -> LearnUsClient:
    from yonstudy.auth import ensure_session

    c = LearnUsClient(args.cookies, min_interval=args.interval, per_minute=args.per_minute)
    try:
        status = ensure_session(
            c, state_path=Path(args.store) / "auth_state.json"
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if status.relogged:
        print("LearnUs 세션 만료 감지 · 자동 재로그인 성공")
    return c


def cmd_login(args) -> int:
    c = LearnUsClient(args.cookies)
    alive, sesskey = c.session_info()
    if alive:
        print(f"이미 로그인되어 있습니다. sesskey={sesskey}")
        c.save()
        return 0
    username = os.environ.get("LEARNUS_ID") or input("학번: ").strip()
    password = os.environ.get("LEARNUS_PW") or getpass.getpass("비밀번호(입력 숨김): ")
    try:
        c.login(username, password)
    except Exception as exc:
        print(f"로그인 실패: {exc}", file=sys.stderr)
        return 1
    finally:
        del password
    alive, sesskey = c.session_info()
    if not alive:
        print("로그인 절차는 끝났으나 세션이 활성화되지 않았습니다.", file=sys.stderr)
        return 1
    c.save()
    print(f"로그인 성공. sesskey={sesskey}\n쿠키: {c.cookie_path}")
    return 0


def cmd_courses(args) -> int:
    store = Store(args.store)
    arc = Archiver(get_client(args), store)
    courses = arc.sync_courses()
    for c in sorted(courses, key=lambda x: (x.year, x.semester)):
        print(f"  {c.year} {c.semester:8s} cid={c.course_id:>7} {c.title}")
    return 0


def cmd_keepalive(args) -> int:
    """현재 세션에 읽기 요청을 보내 유휴 만료를 연장한다."""
    from yonstudy.auth import AutoLoginUnavailable, ensure_session

    c = LearnUsClient(args.cookies, min_interval=args.interval, per_minute=args.per_minute)
    try:
        status = ensure_session(
            c, state_path=Path(args.store) / "auth_state.json"
        )
    except AutoLoginUnavailable as exc:
        print(str(exc))
        return 1
    action = "자동 재로그인 완료" if status.relogged else "세션 정상"
    print(f"LearnUs {action} · keepalive 완료 · sesskey={status.sesskey}")
    return 0


def cmd_archive(args) -> int:
    store = Store(args.store)
    arc = Archiver(get_client(args), store)
    courses = arc.sync_courses()
    if args.year:
        courses = [c for c in courses if c.year in args.year]
    if args.semester:
        courses = [c for c in courses if c.semester in args.semester]
    if args.course:
        courses = [c for c in courses if c.course_id in args.course]
    courses.sort(key=lambda c: (c.year, c.semester), reverse=True)
    if args.limit:
        courses = courses[: args.limit]
    print(f"\n대상 강좌 {len(courses)}개 — 영상 본체는 받지 않습니다.")
    exit_code = 0
    for c in courses:
        try:
            arc.sync_course(
                c,
                probe_vod=not args.no_vod,
                fetch_subtitles=not args.no_subtitles,
                fetch_files=not args.no_files,
                fetch_boards=not args.no_boards,
                board_pages=args.board_pages,
            )
        except KeyboardInterrupt:
            print("\n중단됨")
            exit_code = 130
            break
        except SessionExpired as exc:
            # 세션이 끊긴 뒤 남은 강좌를 계속 도는 건 실패 로그만 쌓는다.
            print(f"\n{exc}\n남은 {len(courses) - courses.index(c)}개 강좌를 중단합니다. "
                  "`python3 cli.py login` 후 같은 명령을 다시 실행하면 이어서 받습니다.")
            exit_code = 1
            break
        except Exception as exc:
            print(f"  ! 실패: {exc}")
            store.log("course", str(c.course_id), False, str(exc))
            exit_code = 1
    store.commit()
    cmd_status(args)
    return exit_code


def cmd_status(args) -> int:
    s = Store(args.store).summary()
    print("\n=== 아카이브 현황 ===")
    print(f"  강좌            {s['courses']}")
    print(f"  활동            {s['activities']}")
    print(f"  동영상          {s['vods']}  (완주 확인: {s['vods_done']})")
    print(f"  제출활동        {s['submissions']}  (제출함: {s['submitted']})")
    print(f"  파일            {s['files']}  ({s['file_bytes']/1024/1024:.1f}MB)")
    print(f"  게시판/포럼 글  {s['posts']}  ({s['boards']}개 게시판)")
    print(f"  자막(전사)      {s['transcripts']}")
    return 0


def cmd_plan(args) -> int:
    from yonstudy.autoplay import build_plan
    from yonstudy.daily import SEOUL, current_term

    store = Store(args.store)
    year, semester = current_term(datetime.now(SEOUL).date())
    course_ids = {
        int(row["course_id"]) for row in store.query(
            "SELECT course_id FROM course WHERE year=? AND semester=? AND enrolled=1",
            (year, semester),
        )
    }
    plan = build_plan(
        store, course_ids=course_ids, include_untracked_once=True
    )
    if not plan:
        print(
            "자동수강 대상이 없습니다. "
            "(현재 수강 가능한 미완료 영상 또는 1회 재생 대상이 없음)"
        )
        return 0
    print(f"\n=== 자동수강 계획: {len(plan)}편 ===")
    for i, j in enumerate(plan, 1):
        print(
            f" {i:>3}. [{j.urgency:>6}] {j.course_name[:14]:14s} {j.title[:38]:38s} "
            f"남은 {j.remaining_sec//60}분  "
            f"{'정상마감 ' + j.open_to if j.open_to else '완주 1회 기록'}  {j.rate}x"
        )
    total = sum(j.remaining_sec for j in plan)
    print(f"\n  총 {total/3600:.1f}시간 분량 → 2배속 기준 약 {total/2/3600:.1f}시간 소요")
    return 0


def cmd_watch(args) -> int:
    from yonstudy.autoplay import build_plan, run_plan
    from yonstudy.daily import SEOUL, current_term
    from yonstudy.progress import progress_verified

    store = Store(args.store)
    year, semester = current_term(datetime.now(SEOUL).date())
    course_ids = {
        int(row["course_id"]) for row in store.query(
            "SELECT course_id FROM course WHERE year=? AND semester=? AND enrolled=1",
            (year, semester),
        )
    }
    plan = build_plan(
        store, course_ids=course_ids, include_untracked_once=True
    )
    if args.limit:
        plan = plan[: args.limit]

    def record_result(job, ok: bool, note: str) -> None:
        kind = "watch" if job.tracks_progress else "playback_once"
        store.log(kind, str(job.cmid), ok, note)
        store.commit()

    code = run_plan(
        plan, args.cookies, dry_run=args.dry_run, rate=args.rate,
        on_result=record_result,
    )
    if code != 0 or args.dry_run:
        return code

    tracked = [job for job in plan if job.tracks_progress]
    if not tracked:
        return 0

    # 수동 watch도 완주 후 서버 진도를 다시 읽는다. 이 단계가 없으면 로컬 DB가
    # 0%인 채 남아 같은 영상을 다음 계획에서 다시 고를 수 있다.
    try:
        archiver = Archiver(get_client(args), store)
        selected_course_ids = {job.course_id for job in tracked}
        courses = [
            course for course in archiver.sync_courses()
            if course.course_id in selected_course_ids
        ]
        for course in courses:
            archiver.sync_course(
                course, probe_vod=False, fetch_subtitles=False,
                fetch_files=False, fetch_boards=False,
            )
        store.commit()
    except Exception as exc:
        print(f"재생은 끝났지만 진도 재확인에 실패했습니다: {exc}", file=sys.stderr)
        return 1

    failed_verification = []
    for job in tracked:
        rows = store.query(
            """
            SELECT v.progress_pct,v.watched_sec,v.duration_sec,a.completion
              FROM vod v JOIN activity a ON a.cmid=v.cmid
             WHERE v.cmid=?
            """,
            (job.cmid,),
        )
        row = dict(rows[0]) if rows else {}
        if not (
            progress_verified(
                row.get("progress_pct"),
                row.get("watched_sec"),
                row.get("duration_sec"),
            )
            or row.get("completion") == "y"
        ):
            failed_verification.append(job.title)
    if failed_verification:
        print(
            "재생 완료 후 진도율과 최대 학습 위치의 완주를 확인하지 못했습니다: "
            + ", ".join(failed_verification),
            file=sys.stderr,
        )
        return 1
    print(f"진도 재확인 완료: {len(tracked)}편 모두 완주 확인")
    return 0


def cmd_report(args) -> int:
    from yonstudy.daily import (
        build_daily_report,
        current_term,
        render_report,
        render_email_text,
        render_report_html,
        report_as_json,
        send_report,
        SEOUL,
    )

    target = date.fromisoformat(args.date) if args.date else datetime.now(SEOUL).date()
    default_year, default_semester = current_term(target)
    year = args.year or default_year
    semester = args.semester or default_semester
    store = Store(args.store)

    sync_needed = args.sync
    if args.sync_if_stale and not sync_needed:
        try:
            state = json.loads(
                (Path(args.store) / "automation_state.json").read_text(encoding="utf-8")
            )
            finished = str(state.get("finished_at") or "")[:10]
            sync_status = (state.get("sync") or {}).get("status")
            sync_needed = finished != target.isoformat() or sync_status != "ok"
        except (OSError, json.JSONDecodeError):
            sync_needed = True

    if sync_needed:
        arc = Archiver(get_client(args), store)
        courses = [
            c for c in arc.sync_courses()
            if c.year == year and c.semester == semester
        ]
        print(f"{year} {semester} 강좌 {len(courses)}개를 리포트용으로 동기화합니다.")
        for course in courses:
            arc.sync_course(
                course,
                probe_vod=True,
                fetch_subtitles=False,
                fetch_files=args.sync_if_stale,
                fetch_boards=True,
                board_pages=3 if args.sync_if_stale else 1,
            )
        store.commit()

    report = build_daily_report(
        store,
        target=target,
        year=year,
        semester=semester,
        horizon_days=args.days,
    )
    body = report_as_json(report) if args.json else render_report(report)
    print(body, end="")
    if args.output:
        Path(args.output).write_text(body, encoding="utf-8")
        print(f"리포트 저장: {args.output}")

    recipient = args.email_to or os.environ.get("YONSTUDY_REPORT_TO")
    if recipient:
        smtp_host = os.environ.get("YONSTUDY_SMTP_HOST")
        smtp_user = os.environ.get("YONSTUDY_SMTP_USER")
        smtp_password = os.environ.get("YONSTUDY_SMTP_PASSWORD")
        smtp_ready = bool(smtp_host and (not smtp_user or smtp_password))
        sendmail_ready = not smtp_host and Path("/usr/sbin/sendmail").exists()
        if args.email_if_configured and not (smtp_ready or sendmail_ready):
            print(f"메일 설정 미완료 — 09:00 발송 보류: {recipient}")
            return 0
        subject = f"[yonstudy] {target.month}월 {target.day}일 런어스 요약"
        send_report(
            render_email_text(report),
            html_body=render_report_html(report),
            to=recipient,
            subject=subject,
        )
        print(f"리포트 메일 전송 요청 완료: {recipient}")
    return 0


def cmd_export(args) -> int:
    from yonstudy.daily import SEOUL, current_term
    from yonstudy.export import export_tree

    today = datetime.now(SEOUL).date()
    default_year, default_semester = current_term(today)
    result = export_tree(
        Store(args.store),
        args.destination,
        year=args.year or default_year,
        semester=args.semester or default_semester,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


# 이전 CLI 함수를 가져다 쓰는 코드와의 호환용이다.
cmd_export_onedrive = cmd_export


def cmd_upload(args) -> int:
    """이미 수집한 파일과 게시글을 rclone remote에 올린다."""
    from yonstudy.daily import SEOUL, current_term
    from yonstudy.remote import RcloneRemote, sync_remote_tree

    default_year, default_semester = current_term(datetime.now(SEOUL).date())
    year = args.year or default_year
    semester = args.semester or default_semester
    store = Store(args.store)
    if args.dry_run:
        counts = {
            row["role"]: row["count"]
            for row in store.query(
                """
                SELECT f.role,COUNT(*) AS count
                 FROM file f JOIN course c ON c.course_id=f.course_id
                 WHERE c.year=? AND c.semester=? AND c.enrolled=1
                   AND f.role IN ('resource','post','submission','introattachment')
                 GROUP BY f.role
                """,
                (year, semester),
            )
        }
        post_count = store.query(
            """
            SELECT COUNT(*) AS count
              FROM post p JOIN course c ON c.course_id=p.course_id
             WHERE c.year=? AND c.semester=? AND c.enrolled=1
               AND NOT (
                   p.modname='forum'
                   AND p.post_id LIKE 't%'
                   AND p.body IS NULL
               )
            """,
            (year, semester),
        )[0]["count"]
        print(json.dumps({
            "mode": "dry-run",
            "remote": args.remote,
            "year": year,
            "semester": semester,
            "files": counts,
            "posts": post_count,
        }, ensure_ascii=False, indent=2))
        return 0

    result = sync_remote_tree(
        store, RcloneRemote(args.remote), year=year, semester=semester
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 1 if result.missing_sources else 0


def cmd_automate(args) -> int:
    from yonstudy.automation import run_daily_automation

    code, state = run_daily_automation(
        store_path=args.store,
        cookie_path=args.cookies,
        export_dir=args.destination,
        remote=args.remote,
        sync=not args.no_sync,
        dry_run=args.dry_run,
        send_mail=not args.no_mail,
        upload_remote=not args.no_upload,
    )
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return code


def cmd_scheduled_watch(args) -> int:
    from yonstudy.automation import run_scheduled_watch

    code, state = run_scheduled_watch(
        store_path=args.store,
        cookie_path=args.cookies,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return code


def cmd_download(args) -> int:
    """동영상에서 오디오·슬라이드 프레임·(선택)영상 원본을 받는다."""
    from yonstudy import vod as V

    store = Store(args.store)
    want_video = args.video
    want_audio = not args.no_audio
    want_frames = not args.no_frames
    rows = V.pending(store, course_id=args.course, limit=args.limit,
                     want_audio=want_audio, want_frames=want_frames, want_video=want_video)
    if not rows:
        print("받을 것이 없습니다.")
        return 0

    hours, gb = V.estimate(rows)
    parts = ", ".join(f"{k} {v:.1f}GB" for k, v in gb.items())
    print(f"대상 {len(rows)}편 · 약 {hours:.0f}시간 → {parts} (합 {sum(gb.values()):.1f}GB)")
    free_gb = __import__("shutil").disk_usage(store.root).free / 1024**3
    print(f"디스크 여유 {free_gb:.1f}GB")
    if sum(gb.values()) > free_gb * 0.9:
        print("! 예상 용량이 디스크 여유에 근접합니다. --no-video 로 줄이거나 공간을 확보하세요.")
        if not args.force:
            return 1
    if args.dry_run:
        for r in rows[:20]:
            made = [k for k, v in (("audio", r["outputs"].audio),
                                   ("frames", r["outputs"].frames_dir),
                                   ("video", r["outputs"].video)) if v]
            print(f"  {r['course_name'][:14]:14s} {r['title'][:38]:38s} "
                  f"{(r['duration_sec'] or 0)//60:>3}분  → {'+'.join(made)}")
        return 0

    client = get_client(args)
    ok = failed = 0
    total = 0
    for i, r in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {r['course_name'][:12]} — {r['title'][:40]}", flush=True)
        res = V.download(r["hls_url"], r["outputs"], cmid=r["cmid"],
                         timeout=args.timeout)
        if not res.ok:
            # CDN 서명 만료일 수 있다. 뷰어를 다시 열어 주소를 갱신하고 한 번 더.
            fresh = V.refresh_hls_url(client, r["cmid"])
            if fresh and fresh != r["hls_url"]:
                store.db.execute("UPDATE vod SET hls_url=? WHERE cmid=?", (fresh, r["cmid"]))
                store.commit()
                res = V.download(fresh, r["outputs"], cmid=r["cmid"], timeout=args.timeout)
        if res.ok:
            ok += 1
            total += res.total_bytes
            print(f"    {res.seconds/60:.1f}분 · {'+'.join(res.made)} · "
                  f"{res.total_bytes/1024/1024:.1f}MB")
        else:
            failed += 1
            print(f"    실패: {res.error[:120]}")
            store.log("download", str(r["cmid"]), False, res.error)
    store.commit()
    print(f"\n완료 {ok}편, 실패 {failed}편, 총 {total/1024**3:.2f}GB")
    return 0 if failed == 0 else 1


def cmd_analyze(args) -> int:
    from yonstudy.studykit.report import analyze_lecture

    return analyze_lecture(Store(args.store), cmid=args.cmid, slides=args.slides)


def cmd_layout_plan(args) -> int:
    """평면 아카이브의 목표 경로만 계산한다. 파일 작업과 네트워크 요청은 하지 않는다."""
    from yonstudy.daily import SEOUL, current_term
    from yonstudy.flat_layout import build_flat_plan

    default_year, default_semester = current_term(datetime.now(SEOUL).date())
    year = args.year or default_year
    semester = args.semester or default_semester
    entries = build_flat_plan(
        Store(args.store), destination=args.destination, year=year, semester=semester
    )
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    payload = {
        "mode": "plan-only-no-files-written",
        "destination": args.destination,
        "year": year,
        "semester": semester,
        "entries": len(entries),
        "by_kind": counts,
        "source_not_ready": sum(not entry.source_ready for entry in entries),
        "sample": [entry.as_dict() for entry in entries[: args.limit]],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_monitor(args) -> int:
    """새 VOD와 온라인출석부를 갱신한다. 영상 재생·다운로드는 하지 않는다."""
    from yonstudy.monitor import run_monitor

    code, state = run_monitor(
        store=Store(args.store),
        cookie_path=args.cookies,
        course_ids=set(args.course or []) or None,
        dry_run=args.dry_run,
    )
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return code


def cmd_archive_only(args) -> int:
    """진도 비추적 VOD를 재생 없이 remote에 보관한다."""
    from dataclasses import asdict
    from yonstudy.daily import SEOUL, current_term
    from yonstudy.remote import RcloneRemote
    from yonstudy.video_archive import archive_untracked_vods

    now = datetime.now(SEOUL)
    year, semester = current_term(now.date())
    store = Store(args.store)
    client = None
    course_ids = set(args.course or []) or None
    if not args.dry_run:
        client = get_client(args)
        archiver = Archiver(client, store)
        courses = [
            course for course in archiver.sync_courses()
            if course.year == year and course.semester == semester
            and (not course_ids or course.course_id in course_ids)
        ]
        for course in courses:
            archiver.sync_course(
                course, probe_vod=True, fetch_subtitles=False,
                fetch_files=False, fetch_boards=False,
            )
        store.commit()

    sink = RcloneRemote(args.remote)
    result = archive_untracked_vods(
        store, sink, year=year, semester=semester,
        course_ids=course_ids, limit=args.limit,
        client=client, dry_run=args.dry_run,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 1 if result.failed_files else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="yonstudy", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cookies", default=DEFAULT_COOKIES)
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--interval", type=float, default=0.4, help="요청 간 최소 간격(초)")
    p.add_argument("--per-minute", type=int, default=40,
                   help="분당 요청 상한. LearnUs는 이걸 넘기면 몇 분간 HTTP 400으로 막는다")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login").set_defaults(fn=cmd_login)
    sub.add_parser("courses").set_defaults(fn=cmd_courses)
    sub.add_parser("keepalive", help="로그인 세션 유휴 만료 방지").set_defaults(fn=cmd_keepalive)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("plan").set_defaults(fn=cmd_plan)

    a = sub.add_parser("archive")
    a.add_argument("--year", nargs="*")
    a.add_argument("--semester", nargs="*", help="1학기/2학기/여름계절수업/겨울계절수업")
    a.add_argument("--course", nargs="*", type=int)
    a.add_argument("--limit", type=int)
    a.add_argument("--no-vod", action="store_true", help="VOD 뷰어 조회 생략")
    a.add_argument("--no-subtitles", action="store_true")
    a.add_argument("--no-files", action="store_true", help="첨부/제출 파일 내려받지 않음")
    a.add_argument("--no-boards", action="store_true", help="게시판/포럼 글 수집 생략")
    a.add_argument("--board-pages", type=int, default=3,
                   help="게시판당 목록 페이지 수 (기본 3, 0이면 전체)")
    a.set_defaults(fn=cmd_archive)

    w = sub.add_parser("watch")
    w.add_argument("--limit", type=int)
    w.add_argument("--rate", type=float, default=None, help="재생 배속 (기본: 서버 허용 최대)")
    w.add_argument("--dry-run", action="store_true")
    w.set_defaults(fn=cmd_watch)

    rep = sub.add_parser("report", help="이번 학기 진도/제출 + 오늘 업데이트 리포트")
    rep.add_argument("--sync", action="store_true", help="현재 학기를 먼저 가볍게 동기화")
    rep.add_argument(
        "--sync-if-stale",
        action="store_true",
        help="당일 전체 동기화가 성공하지 않았을 때만 자료를 갱신",
    )
    rep.add_argument("--date", help="리포트 날짜 YYYY-MM-DD (기본: 오늘)")
    rep.add_argument("--year", help="대상 연도 (기본: 날짜에서 계산)")
    rep.add_argument("--semester", help="대상 학기 (기본: 날짜에서 계산)")
    rep.add_argument("--days", type=int, default=14, help="할 일 미리보기 기간")
    rep.add_argument("--email-to", help="리포트를 받을 주소")
    rep.add_argument(
        "--email-if-configured",
        action="store_true",
        help="SMTP/sendmail이 준비된 경우에만 보내고, 미설정이면 정상 종료",
    )
    rep.add_argument("--output", help="텍스트/JSON 리포트 저장 경로")
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(fn=cmd_report)

    exp = sub.add_parser(
        "export", aliases=["export-onedrive"],
        help="강의자료·게시글을 로컬 폴더로 내보내기",
    )
    exp.add_argument("--destination", default=DEFAULT_EXPORT)
    exp.add_argument("--year")
    exp.add_argument("--semester")
    exp.add_argument("--dry-run", action="store_true")
    exp.set_defaults(fn=cmd_export)

    upload = sub.add_parser("upload", help="수집한 자료를 rclone remote에 업로드")
    upload.add_argument("--remote", default=DEFAULT_REMOTE)
    upload.add_argument("--year")
    upload.add_argument("--semester")
    upload.add_argument("--dry-run", action="store_true")
    upload.set_defaults(fn=cmd_upload)

    auto = sub.add_parser("automate", help="동기화·원격 백업·메일 리포트 일괄 실행")
    auto.add_argument("--destination", default=DEFAULT_EXPORT)
    auto.add_argument("--remote", default=DEFAULT_REMOTE)
    auto.add_argument("--no-sync", action="store_true")
    auto.add_argument("--no-mail", action="store_true")
    auto.add_argument(
        "--no-upload", "--no-onedrive",
        dest="no_upload",
        action="store_true",
        help="rclone remote 업로드를 생략",
    )
    auto.add_argument("--dry-run", action="store_true")
    auto.set_defaults(fn=cmd_automate)

    scheduled = sub.add_parser(
        "scheduled-watch",
        help="현재 학기 미완료 강의를 정상 마감 전에 소량 재생하고 진도 재검증",
    )
    scheduled.add_argument("--limit", type=int, default=1)
    scheduled.add_argument("--dry-run", action="store_true")
    scheduled.set_defaults(fn=cmd_scheduled_watch)

    dl = sub.add_parser("download")
    dl.add_argument("--course", type=int)
    dl.add_argument("--limit", type=int)
    dl.add_argument("--video", action="store_true", help="영상 원본(mp4)도 저장. 1080p 그대로라 용량이 크다")
    dl.add_argument("--no-audio", action="store_true")
    dl.add_argument("--no-frames", action="store_true", help="슬라이드 전환 프레임 추출 생략")
    dl.add_argument("--timeout", type=int, default=7200)
    dl.add_argument("--force", action="store_true", help="디스크 경고를 무시하고 진행")
    dl.add_argument("--dry-run", action="store_true")
    dl.set_defaults(fn=cmd_download)

    layout = sub.add_parser(
        "layout-plan",
        help="복사·다운로드 없이 평면형 주차 파일명 계획만 출력",
    )
    layout.add_argument("--destination", default="/root/yonstudy/exports/ArchiveFlat")
    layout.add_argument("--year")
    layout.add_argument("--semester")
    layout.add_argument("--limit", type=int, default=30, help="표시할 예시 경로 수")
    layout.set_defaults(fn=cmd_layout_plan)

    monitor = sub.add_parser(
        "monitor",
        help="재생 없이 새 VOD·온라인출석부·과목별 시청 순서를 갱신",
    )
    monitor.add_argument("--course", nargs="*", type=int)
    monitor.add_argument("--dry-run", action="store_true")
    monitor.set_defaults(fn=cmd_monitor)

    archive_only = sub.add_parser(
        "archive-only", help="진도 비추적 VOD를 재생 없이 remote에 원본 보관",
    )
    archive_only.add_argument("--course", nargs="*", type=int)
    archive_only.add_argument("--limit", type=int)
    archive_only.add_argument("--remote", default=DEFAULT_REMOTE)
    archive_only.add_argument("--dry-run", action="store_true")
    archive_only.set_defaults(fn=cmd_archive_only)

    an = sub.add_parser("analyze")
    an.add_argument("--cmid", type=int, required=True)
    an.add_argument("--slides", help="강의안 PDF 경로 (생략 시 같은 주차 자료에서 추측)")
    an.set_defaults(fn=cmd_analyze)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
