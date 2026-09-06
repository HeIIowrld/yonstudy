"""재생 없이 새 VOD·온라인출석부·과목별 시청 순서를 점검한다."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .archive import Archiver
from .client import LearnUsClient
from .auth import ensure_session
from .daily import SEOUL, current_term


def _seconds_label(value: int | None) -> str | None:
    if value is None:
        return None
    value = max(0, int(value))
    hours, rest = divmod(value, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def attendance_snapshot(store, *, year: str, semester: str) -> list[dict]:
    """온라인출석부의 콘텐츠 길이·최대 학습위치·진도율을 같은 행으로 반환한다."""
    rows = store.query(
        """
        SELECT c.course_id,c.name AS course_name,a.cmid,a.title,a.section_idx,
               a.section_name,a.url,a.open_from,a.open_to,a.late_until,a.completion,
               v.duration_sec,v.watched_sec,v.progress_pct,v.is_progress,v.probed_at,
               EXISTS(
                   SELECT 1 FROM crawl_log l
                    WHERE l.kind='playback_once' AND l.ref=CAST(v.cmid AS TEXT) AND l.ok=1
               ) AS played_once
          FROM activity a JOIN course c ON c.course_id=a.course_id
          JOIN vod v ON v.cmid=a.cmid
         WHERE c.year=? AND c.semester=? AND a.modname='vod'
         ORDER BY c.name,a.section_idx,a.cmid
        """,
        (year, semester),
    )
    out = []
    for source in rows:
        row = dict(source)
        duration = row.get("duration_sec")
        watched = row.get("watched_sec")
        if row.get("is_progress") == 0:
            progress_ok = bool(row.get("played_once"))
            position_ok = bool(row.get("played_once"))
        else:
            progress_ok = (row.get("progress_pct") or 0) >= 100
            position_ok = bool(
                duration is not None and watched is not None
                and watched >= max(0, duration - 2)
            )
        row.update(
            duration_label=_seconds_label(duration),
            max_position_label=_seconds_label(watched),
            progress_ok=progress_ok,
            position_ok=position_ok,
            verified=progress_ok and position_ok,
        )
        out.append(row)
    return out


def viewing_queue(store, *, year: str, semester: str, now: datetime | None = None) -> list[dict]:
    """현재 열려 있고 미완료인 영상만 과목/주차/차시 순으로 정렬한다."""
    now = now or datetime.now(SEOUL)
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    rows = attendance_snapshot(store, year=year, semester=semester)
    queue = []
    for row in rows:
        if row.get("is_progress") == 0:
            continue
        if row["verified"] or row.get("completion") == "y":
            continue
        opens = row.get("open_from")
        normal = row.get("open_to")
        final = row.get("late_until") or normal
        if opens and opens > now_text:
            continue
        if final and final < now_text:
            continue
        duration = row.get("duration_sec") or 0
        watched = row.get("watched_sec") or 0
        row = dict(row)
        row.update(
            period="지각기간" if normal and normal < now_text else "정상기간",
            remaining_minutes=(max(0, duration - watched) + 59) // 60 if duration else None,
        )
        queue.append(row)
    return sorted(
        queue,
        key=lambda row: (
            row["course_name"], row.get("section_idx") or 0,
            row.get("open_from") or "", row["cmid"],
        ),
    )


def run_monitor(
    *,
    store,
    cookie_path: str,
    course_ids: set[int] | None = None,
    dry_run: bool = False,
) -> tuple[int, dict]:
    """현재 학기를 읽기 전용으로 갱신하고 상태 파일을 만든다. 재생/다운로드는 없다."""
    now = datetime.now(SEOUL)
    year, semester = current_term(now.date())
    state_path = Path(store.root, "monitor_state.json")
    previous = {}
    if state_path.is_file():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    since = previous.get("finished_at") or now.strftime("%Y-%m-%dT00:00:00%z")
    state: dict = {
        "started_at": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "year": year,
        "semester": semester,
        "mode": "read-only-no-playback-no-download",
        "sync": {"status": "skipped" if dry_run else "starting"},
    }

    if not dry_run:
        try:
            client = LearnUsClient(cookie_path)
            auth = ensure_session(
                client, state_path=Path(store.root) / "auth_state.json"
            )
            archiver = Archiver(client, store)
            courses = [
                course for course in archiver.sync_courses()
                if course.year == year and course.semester == semester
                and (not course_ids or course.course_id in course_ids)
            ]
            errors = []
            for course in courses:
                try:
                    archiver.sync_course(
                        course,
                        probe_vod=True,
                        fetch_subtitles=False,
                        fetch_files=False,
                        fetch_boards=False,
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
        except Exception as exc:
            state["sync"] = {"status": "error", "message": str(exc)}

    new_videos = [
        dict(row) for row in store.query(
            """
            SELECT e.at,e.cmid,e.title,c.name AS course_name,a.open_from,a.open_to,a.url
              FROM change_event e JOIN course c ON c.course_id=e.course_id
              LEFT JOIN activity a ON a.cmid=e.cmid
             WHERE c.year=? AND c.semester=? AND e.kind='activity_discovered'
               AND a.modname='vod' AND e.at>?
             ORDER BY e.at,e.id
            """,
            (year, semester, since[:19]),
        )
    ]
    attendance = attendance_snapshot(store, year=year, semester=semester)
    queue = viewing_queue(store, year=year, semester=semester, now=now)
    if course_ids:
        attendance = [row for row in attendance if row["course_id"] in course_ids]
        queue = [row for row in queue if row["course_id"] in course_ids]
        new_videos = [row for row in new_videos if row["cmid"] in {x["cmid"] for x in attendance}]
    state.update(
        new_videos=new_videos,
        viewing_queue=queue,
        attendance=attendance,
        finished_at=datetime.now(SEOUL).strftime("%Y-%m-%dT%H:%M:%S%z"),
    )
    if not dry_run:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    code = 0 if state.get("sync", {}).get("status") in {"ok", "partial", "skipped"} else 1
    return code, state
