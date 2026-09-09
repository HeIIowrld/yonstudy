"""SQLite 메타데이터와 sha256 기반 blob을 관리한다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .filename_normalization import nfc
from .progress import progress_verified


SEOUL = ZoneInfo("Asia/Seoul")


def _now() -> str:
    """LearnUs 화면의 날짜와 같은 한국시간으로 저장한다."""
    return datetime.now(SEOUL).strftime("%Y-%m-%dT%H:%M:%S")

SCHEMA = """
CREATE TABLE IF NOT EXISTS course (
    course_id INTEGER PRIMARY KEY,
    year TEXT, semester TEXT, kind TEXT,
    title TEXT, name TEXT, code TEXT, section TEXT,
    slug TEXT, archived_at TEXT,
    enrolled INTEGER NOT NULL DEFAULT 1,
    unenrolled_at TEXT,
    detail_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    cmid INTEGER PRIMARY KEY,
    course_id INTEGER, modname TEXT, title TEXT, url TEXT,
    section_idx INTEGER, section_name TEXT,
    indent INTEGER, completion TEXT,
    open_from TEXT, open_to TEXT, late_until TEXT, duration TEXT, restricted INTEGER,
    seen_at TEXT,
    present INTEGER NOT NULL DEFAULT 1,
    removed_at TEXT
);

-- 동영상 강의: 재생/진도 관련 사실만 모아둔다.
CREATE TABLE IF NOT EXISTS vod (
    cmid INTEGER PRIMARY KEY,
    course_id INTEGER,
    uuid TEXT, hls_url TEXT, poster TEXT,
    subtitle_langs TEXT,
    duration_sec INTEGER,
    watched_sec INTEGER,
    progress_pct REAL,
    is_progress INTEGER,        -- 이 VOD 자체가 진도 추적 대상인가
    progress_period INTEGER,    -- 현재 진도 처리 기간인가
    can_log_progress INTEGER,   -- 지금 재생하면 진도가 잡히는가
    max_rate REAL,              -- 서버가 허용하는 최대 배속
    seek_restricted INTEGER,
    trackid TEXT, attempt INTEGER,
    checker_url TEXT,
    -- 왜 재생 정보를 못 얻었는지 남긴다. NULL만 남기면 파서 버그와 구분되지 않는다.
    status TEXT,        -- ok / empty(콘텐츠 없음) / no_access(열람 만료·제한) / error
    probed_at TEXT
);

-- 제출이 발생하는 모든 활동을 한 테이블로 모은다.
-- assign 뿐 아니라 turnitintooltwo / vpl / quiz / feedback / choice / forum 도 여기 들어간다.
CREATE TABLE IF NOT EXISTS submission (
    cmid INTEGER PRIMARY KEY,
    course_id INTEGER, modname TEXT, title TEXT,
    status TEXT, grading_status TEXT, due_at TEXT, last_modified TEXT,
    grade TEXT, fields_json TEXT, submitted INTEGER, seen_at TEXT,
    instructions TEXT,
    instructions_html TEXT
);

-- 첨부/제출/자료 파일. content는 blobs/ 아래에 sha256으로 저장.
CREATE TABLE IF NOT EXISTS file (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER, cmid INTEGER,
    role TEXT,             -- submission / introattachment / resource / subtitle / video
    name TEXT, url TEXT, sha256 TEXT, bytes INTEGER, saved_at TEXT,
    remote_path TEXT,
    remote_status TEXT,
    remote_saved_at TEXT,
    UNIQUE(url, role)
);

-- 게시판(ubboard) 글과 포럼(forum) 글. 공지사항도 여기 들어간다.
CREATE TABLE IF NOT EXISTS post (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER, cmid INTEGER, modname TEXT,
    post_id TEXT, thread_id TEXT,
    no TEXT, subject TEXT, writer TEXT, written_at TEXT, hits TEXT,
    replies INTEGER, url TEXT, body TEXT, fetched_at TEXT,
    checked_at TEXT,
    UNIQUE(cmid, modname, post_id)
);

CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cmid INTEGER, source TEXT,      -- learnus_auto / whisper / vibevoice …
    lang TEXT, path TEXT, segments INTEGER, created_at TEXT,
    UNIQUE(cmid, source, lang)
);

-- 대면 수업 시간표. 외부 녹음 파일을 강좌에 연결할 때 사용한다.
CREATE TABLE IF NOT EXISTS timetable_slot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL,
    weekday INTEGER NOT NULL,       -- Python weekday: 월=0 .. 일=6
    starts_at TEXT NOT NULL,        -- HH:MM
    ends_at TEXT NOT NULL,          -- HH:MM (다음 날 종료도 허용)
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    location TEXT,
    source TEXT NOT NULL,
    imported_at TEXT,
    UNIQUE(course_id, weekday, starts_at, ends_at, valid_from, valid_to, source)
);

-- 휴대폰/녹음기에서 가져온 원본과 시간표 분류 결과.
CREATE TABLE IF NOT EXISTS recording (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    original_name TEXT,
    metadata_title TEXT,
    source_path TEXT,
    source_bytes INTEGER,
    source_mtime_ns INTEGER,
    captured_at TEXT,
    timestamp_source TEXT,
    duration_sec REAL,
    course_id INTEGER,
    timetable_slot_id INTEGER,
    week INTEGER,
    lesson INTEGER,
    match_status TEXT NOT NULL,     -- matched / ambiguous / unclassified
    match_method TEXT,
    confidence REAL,
    path TEXT,
    details_json TEXT,
    imported_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crawl_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT, kind TEXT, ref TEXT, ok INTEGER, note TEXT
);

-- 직전 동기화와 비교해 새 활동·상태 변경·완료를 남긴다.
-- 일일 리포트는 현재 스냅샷만 추측하지 않고 이 이력을 기준으로 작성한다.
CREATE TABLE IF NOT EXISTS change_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT, kind TEXT,
    course_id INTEGER, cmid INTEGER,
    title TEXT, old_value TEXT, new_value TEXT,
    details TEXT
);

CREATE INDEX IF NOT EXISTS idx_activity_course ON activity(course_id);
CREATE INDEX IF NOT EXISTS idx_file_course ON file(course_id);
CREATE INDEX IF NOT EXISTS idx_vod_course ON vod(course_id);
CREATE INDEX IF NOT EXISTS idx_sub_course ON submission(course_id);
CREATE INDEX IF NOT EXISTS idx_post_course ON post(course_id);
CREATE INDEX IF NOT EXISTS idx_post_cmid ON post(cmid);
CREATE INDEX IF NOT EXISTS idx_change_at ON change_event(at);
CREATE INDEX IF NOT EXISTS idx_change_course ON change_event(course_id);
CREATE INDEX IF NOT EXISTS idx_timetable_course ON timetable_slot(course_id);
CREATE INDEX IF NOT EXISTS idx_timetable_weekday ON timetable_slot(weekday);
CREATE INDEX IF NOT EXISTS idx_recording_course ON recording(course_id);
CREATE INDEX IF NOT EXISTS idx_recording_captured ON recording(captured_at);
"""


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        (self.root / "courses").mkdir(parents=True, exist_ok=True)
        # 아카이빙 중에 조회/분석 명령을 같이 돌리는 일이 흔하다.
        # WAL + busy_timeout이면 읽기와 쓰기가 서로를 막지 않는다.
        self.db = sqlite3.connect(self.root / "db.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """스키마에 컬럼을 추가해도 기존 DB에는 반영되지 않는다
        (`CREATE TABLE IF NOT EXISTS`는 아무것도 바꾸지 않는다).
        SCHEMA에는 있는데 실제 테이블에 없는 컬럼을 찾아 붙인다."""
        for table, ddl in re.findall(
            r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA, re.S
        ):
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for line in ddl.splitlines():
                line = line.strip().rstrip(",")
                if not line or line.startswith("--") or line.upper().startswith("UNIQUE"):
                    continue
                name, _, rest = line.partition(" ")
                if name in have or not rest:
                    continue
                col_type = rest.split("--")[0].strip().rstrip(",")
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")

    # 파일 blob

    def put_blob(self, data: bytes) -> tuple[str, int]:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "blobs" / digest[:2] / digest[2:4] / digest
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.rename(path)
        return digest, len(data)

    def put_blob_file(self, source: str | Path) -> tuple[str, int]:
        """큰 파일을 메모리에 전부 올리지 않고 content-addressed blob으로 넣는다."""
        source = Path(source)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        hexdigest = digest.hexdigest()
        destination = self.blob_path(hexdigest)
        if destination.exists():
            return hexdigest, size

        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{hexdigest[:12]}.", suffix=".part", dir=destination.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(source, temporary)
            # mkstemp는 0600이므로, 아카이브에 하드링크된 파일도 읽을 수 있게 맞춘다.
            temporary.chmod(0o644)
            try:
                temporary.replace(destination)
            except FileExistsError:
                # 다른 프로세스가 같은 blob을 먼저 완성했으면 그 파일을 쓴다.
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return hexdigest, size

    def blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / digest[:2] / digest[2:4] / digest

    def link_into_course(self, digest: str, course_dir: str, filename: str) -> Path:
        """blob을 강좌 폴더에 사람이 읽을 이름으로 하드링크한다."""
        dest = self.root / "courses" / nfc(course_dir) / nfc(filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            src = self.blob_path(digest)
            try:
                os.link(src, dest)
            except OSError:
                dest.write_bytes(src.read_bytes())
        return dest

    def has_file(self, url: str, role: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM file WHERE url=? AND role=?", (url, role)
            ).fetchone()
            is not None
        )

    def file_record(self, url: str, role: str):
        return self.db.execute(
            "SELECT * FROM file WHERE url=? AND role=?", (url, role)
        ).fetchone()

    def update_file_remote(self, url: str, role: str, path: str, status: str) -> None:
        self.db.execute(
            """
            UPDATE file
               SET remote_path=?,remote_status=?,remote_saved_at=?
             WHERE url=? AND role=?
            """,
            (path, status, _now(), url, role),
        )

    def has_missing_file(self, cmid: int, role: str) -> bool:
        """로컬이나 remote에 저장되지 않아 다시 받아야 하는지 확인한다."""
        return (
            self.db.execute(
                """
                SELECT 1 FROM file
                 WHERE cmid=? AND role=?
                   AND (sha256 IS NULL OR remote_status IN ('error','missing'))
                 LIMIT 1
                """,
                (cmid, role),
            ).fetchone()
            is not None
        )

    # 메타데이터 저장

    def _upsert(self, table: str, key: str, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != key)
        self.db.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
            f"ON CONFLICT({key}) DO UPDATE SET {updates}",
            tuple(row.values()),
        )

    def save_course(self, row: dict) -> None:
        self._upsert("course", "course_id", row)

    def reconcile_enrollment(
        self, enrolled_course_ids: set[int], *, synced_at: str | None = None
    ) -> None:
        """강좌 목록 스냅샷에 없는 강좌를 비수강 상태로 전환한다.

        활동과 파일은 아카이브를 위해 그대로 보존하고, 현재 목록/자동화에서만
        제외할 수 있도록 강좌 상태만 바꾼다. 다시 목록에 나타난 강좌는
        ``save_course``에서 수강 상태로 복원된다.
        """
        synced_at = synced_at or _now()
        if enrolled_course_ids:
            placeholders = ",".join("?" for _ in enrolled_course_ids)
            self.db.execute(
                f"""
                UPDATE course
                   SET enrolled=0,unenrolled_at=COALESCE(unenrolled_at,?)
                 WHERE enrolled<>0 AND course_id NOT IN ({placeholders})
                """,
                (synced_at, *sorted(enrolled_course_ids)),
            )
        else:
            self.db.execute(
                """
                UPDATE course
                   SET enrolled=0,unenrolled_at=COALESCE(unenrolled_at,?)
                 WHERE enrolled<>0
                """,
                (synced_at,),
            )

    def reconcile_activities(
        self,
        course_id: int,
        present_cmids: set[int],
        *,
        synced_at: str | None = None,
    ) -> None:
        """정상적으로 파싱한 강좌 스냅샷에서 사라진 활동을 비활성화한다."""
        synced_at = synced_at or _now()
        params: tuple = (synced_at, course_id)
        clause = ""
        if present_cmids:
            placeholders = ",".join("?" for _ in present_cmids)
            clause = f" AND cmid NOT IN ({placeholders})"
            params += tuple(sorted(present_cmids))
        self.db.execute(
            f"""
            UPDATE activity
               SET present=0,removed_at=COALESCE(removed_at,?)
             WHERE course_id=? AND present<>0{clause}
            """,
            params,
        )

    def mark_course_detail_synced(
        self, course_id: int, *, synced_at: str | None = None
    ) -> None:
        self.db.execute(
            "UPDATE course SET detail_synced_at=? WHERE course_id=?",
            (synced_at or _now(), course_id),
        )

    def prune_monitor_state(self) -> None:
        """마지막 monitor 결과에서도 철회 강좌 행을 즉시 제거한다.

        ``monitor_state.json``은 이력 파일이지만 외부 상태판이 그대로 표시할 수
        있으므로, 강좌 목록만 갱신한 직후에도 이미 비수강 처리된 강좌가 대기열에
        남지 않게 한다. 손상된 상태 파일은 건드리지 않는다.
        """
        path = self.root / "monitor_state.json"
        if not path.is_file():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(state, dict):
            return

        active_courses = {
            int(row["course_id"])
            for row in self.query("SELECT course_id FROM course WHERE enrolled=1")
        }
        active_cmids = {
            int(row["cmid"])
            for row in self.query(
                """
                SELECT a.cmid
                  FROM activity a JOIN course c ON c.course_id=a.course_id
                 WHERE c.enrolled=1 AND a.present=1
                """
            )
        }

        changed = False

        def number(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        for key in ("new_videos", "viewing_queue", "attendance"):
            rows = state.get(key)
            if not isinstance(rows, list):
                continue
            kept = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                course_id = number(row.get("course_id"))
                cmid = number(row.get("cmid"))
                keep = (
                    course_id is not None and course_id in active_courses
                ) or (
                    course_id is None and cmid is not None and cmid in active_cmids
                )
                if keep:
                    kept.append(row)
            if len(kept) != len(rows):
                state[key] = kept
                changed = True
        if not changed:
            return
        state["roster_reconciled_at"] = _now()
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def save_activity(self, row: dict) -> None:
        old = self.db.execute(
            "SELECT * FROM activity WHERE cmid=?", (row["cmid"],)
        ).fetchone()
        if old is None:
            self.record_change(
                "activity_discovered", row.get("course_id"), row["cmid"],
                row.get("title"), None, row.get("completion"),
                {"modname": row.get("modname"), "open_from": row.get("open_from")},
            )
        else:
            old_completion = old["completion"]
            new_completion = row.get("completion")
            if old_completion != new_completion:
                self.record_change(
                    "completion_changed", row.get("course_id"), row["cmid"],
                    row.get("title"), old_completion, new_completion,
                    {"modname": row.get("modname")},
                )

            changed = {}
            for key in ("title", "open_from", "open_to", "late_until", "restricted"):
                if old[key] != row.get(key):
                    changed[key] = {"old": old[key], "new": row.get(key)}
            if changed:
                self.record_change(
                    "activity_updated", row.get("course_id"), row["cmid"],
                    row.get("title"), None, None, changed,
                )
        self._upsert("activity", "cmid", row)

    def save_vod(self, row: dict) -> None:
        old = self.db.execute(
            "SELECT progress_pct,watched_sec,duration_sec FROM vod WHERE cmid=?",
            (row["cmid"],),
        ).fetchone()
        old_done = bool(
            old
            and progress_verified(
                old["progress_pct"], old["watched_sec"], old["duration_sec"]
            )
        )
        new_pct = row.get("progress_pct", old["progress_pct"] if old else None)
        new_watched = row.get("watched_sec", old["watched_sec"] if old else None)
        new_duration = row.get("duration_sec", old["duration_sec"] if old else None)
        new_done = progress_verified(new_pct, new_watched, new_duration)
        completion = self.db.execute(
            "SELECT completion FROM activity WHERE cmid=?", (row["cmid"],)
        ).fetchone()
        # 완료 체크가 함께 올라온 경우에는 그 신호를 인정한다. 다만 이후의 부분
        # 갱신마다 같은 이벤트를 만들지 않도록 진도율이 100%에 도달한 순간만 본다.
        reached_pct_with_completion = bool(
            old
            and (old["progress_pct"] or 0) < 100
            and (new_pct or 0) >= 100
            and completion
            and completion["completion"] == "y"
        )
        if old is not None and not old_done and (
            new_done or reached_pct_with_completion
        ):
            title = self.db.execute(
                "SELECT title FROM activity WHERE cmid=?", (row["cmid"],)
            ).fetchone()
            self.record_change(
                "vod_completed", row.get("course_id"), row["cmid"],
                title[0] if title else None, "0", "1",
                {
                    "progress_pct": new_pct,
                    "watched_sec": new_watched,
                    "duration_sec": new_duration,
                },
            )
        self._upsert("vod", "cmid", row)

    def save_post(self, row: dict) -> None:
        row = dict(row)
        now = _now()
        row.setdefault("fetched_at", now)
        row.setdefault("checked_at", now)
        old = self.db.execute(
            "SELECT fetched_at FROM post WHERE cmid=? AND modname=? AND post_id=?",
            (row.get("cmid"), row.get("modname"), row.get("post_id")),
        ).fetchone()
        # forum의 t123 행은 토론 수집 완료를 나타내는 내부 마커다.
        synthetic_forum_marker = (
            row.get("modname") == "forum"
            and str(row.get("post_id") or "").startswith("t")
        )
        if old is None and not synthetic_forum_marker:
            self.record_change(
                "post_discovered", row.get("course_id"), row.get("cmid"),
                row.get("subject"), None, row.get("written_at"),
                {"url": row.get("url"), "writer": row.get("writer")},
            )
        elif old is not None and old["fetched_at"]:
            # fetched_at은 일일 리포트에서 최초 발견일로 사용한다. 주기적인 본문
            # 재확인이 과거 글을 새 글로 다시 알리지 않도록 발견 시각은 보존한다.
            row["fetched_at"] = old["fetched_at"]
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self.db.execute(
            f"INSERT OR REPLACE INTO post ({cols}) VALUES ({marks})", tuple(row.values())
        )

    def has_post(self, cmid: int, modname: str, post_id: str) -> bool:
        return self.post_record(cmid, modname, post_id) is not None

    def post_record(self, cmid: int, modname: str, post_id: str):
        return self.db.execute(
            "SELECT * FROM post WHERE cmid=? AND modname=? AND post_id=?",
            (cmid, modname, post_id),
        ).fetchone()

    def save_submission(self, row: dict) -> None:
        old = self.db.execute(
            "SELECT submitted FROM submission WHERE cmid=?", (row["cmid"],)
        ).fetchone()
        if old is not None and not old["submitted"] and row.get("submitted"):
            self.record_change(
                "submission_completed", row.get("course_id"), row["cmid"],
                row.get("title"), "0", "1", {"modname": row.get("modname")},
            )
        self._upsert("submission", "cmid", row)

    def record_change(
        self,
        kind: str,
        course_id: int | None,
        cmid: int | None,
        title: str | None,
        old_value,
        new_value,
        details,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO change_event
                (at,kind,course_id,cmid,title,old_value,new_value,details)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                _now(), kind, course_id, cmid, title,
                None if old_value is None else str(old_value),
                None if new_value is None else str(new_value),
                json.dumps(details, ensure_ascii=False) if details is not None else None,
            ),
        )

    def save_file(self, row: dict) -> None:
        row = dict(row)
        row.setdefault("saved_at", _now())
        old = self.db.execute(
            "SELECT 1 FROM file WHERE url=? AND role=?",
            (row.get("url"), row.get("role")),
        ).fetchone()
        # 자막과 본인 제출물은 새 수업 자료 알림에서 제외한다. 강의자료와
        # 게시판 첨부는 최초 발견 시점을 남겨 일일 리포트에서 안정적으로 잡는다.
        if old is None and row.get("role") in {"resource", "post", "introattachment"}:
            self.record_change(
                "file_discovered", row.get("course_id"), row.get("cmid"),
                row.get("name"), None, row.get("sha256"),
                {"url": row.get("url"), "role": row.get("role"), "bytes": row.get("bytes")},
            )
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in row if column not in {"url", "role"}
        )
        self.db.execute(
            f"INSERT INTO file ({cols}) VALUES ({marks}) "
            f"ON CONFLICT(url,role) DO UPDATE SET {updates}",
            tuple(row.values()),
        )

    def save_transcript(self, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self.db.execute(
            f"INSERT OR REPLACE INTO transcript ({cols}) VALUES ({marks})",
            tuple(row.values()),
        )

    def save_recording(self, row: dict) -> None:
        row = dict(row)
        now = _now()
        row.setdefault("imported_at", now)
        row.setdefault("updated_at", now)
        columns = ", ".join(row)
        marks = ", ".join("?" * len(row))
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in row
            if column not in {"sha256", "imported_at"}
        )
        self.db.execute(
            f"INSERT INTO recording ({columns}) VALUES ({marks}) "
            f"ON CONFLICT(sha256) DO UPDATE SET {updates}",
            tuple(row.values()),
        )

    def log(self, kind: str, ref: str, ok: bool, note: str = "") -> None:
        """진단용 기록. 다른 프로세스가 DB를 쓰고 있으면 조용히 건너뛴다 —
        로그 한 줄 때문에 긴 작업 전체가 죽으면 안 된다."""
        try:
            self.db.execute(
                "INSERT INTO crawl_log (at, kind, ref, ok, note) VALUES (?,?,?,?,?)",
                (_now(), kind, ref, int(ok), note[:500]),
            )
        except sqlite3.OperationalError:
            pass

    def commit(self) -> None:
        self.db.commit()

    # 조회

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def summary(self) -> dict:
        q = lambda s: self.db.execute(s).fetchone()[0]  # noqa: E731
        return {
            "courses": q("SELECT COUNT(*) FROM course"),
            "activities": q("SELECT COUNT(*) FROM activity"),
            "vods": q("SELECT COUNT(*) FROM vod"),
            "vods_done": q(
                """
                SELECT COUNT(*)
                  FROM vod v LEFT JOIN activity a ON a.cmid=v.cmid
                 WHERE a.completion='y'
                    OR (v.progress_pct>=100 AND v.duration_sec>0
                        AND v.watched_sec IS NOT NULL
                        AND v.watched_sec>=MAX(0,v.duration_sec-2))
                """
            ),
            "submissions": q("SELECT COUNT(*) FROM submission"),
            "submitted": q("SELECT COUNT(*) FROM submission WHERE submitted=1"),
            "files": q("SELECT COUNT(*) FROM file"),
            "file_bytes": q("SELECT COALESCE(SUM(bytes),0) FROM file"),
            "transcripts": q("SELECT COUNT(*) FROM transcript"),
            "recordings": q("SELECT COUNT(*) FROM recording"),
            "recordings_matched": q(
                "SELECT COUNT(*) FROM recording WHERE match_status='matched'"
            ),
            "posts": q("SELECT COUNT(*) FROM post"),
            "boards": q("SELECT COUNT(DISTINCT cmid) FROM post"),
        }

    def write_json(self, relpath: str, obj) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        return path
