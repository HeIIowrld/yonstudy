"""SQLite 메타데이터와 sha256 기반 blob을 관리한다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


def _now() -> str:
    """LearnUs 화면의 날짜와 같은 한국시간으로 저장한다."""
    return datetime.now(SEOUL).strftime("%Y-%m-%dT%H:%M:%S")

SCHEMA = """
CREATE TABLE IF NOT EXISTS course (
    course_id INTEGER PRIMARY KEY,
    year TEXT, semester TEXT, kind TEXT,
    title TEXT, name TEXT, code TEXT, section TEXT,
    slug TEXT, archived_at TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    cmid INTEGER PRIMARY KEY,
    course_id INTEGER, modname TEXT, title TEXT, url TEXT,
    section_idx INTEGER, section_name TEXT,
    indent INTEGER, completion TEXT,
    open_from TEXT, open_to TEXT, late_until TEXT, duration TEXT, restricted INTEGER,
    seen_at TEXT
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
    grade TEXT, fields_json TEXT, submitted INTEGER, seen_at TEXT
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
    UNIQUE(cmid, modname, post_id)
);

CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cmid INTEGER, source TEXT,      -- learnus_auto / whisper / vibevoice …
    lang TEXT, path TEXT, segments INTEGER, created_at TEXT,
    UNIQUE(cmid, source, lang)
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

    def blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / digest[:2] / digest[2:4] / digest

    def link_into_course(self, digest: str, course_dir: str, filename: str) -> Path:
        """blob을 강좌 폴더에 사람이 읽을 이름으로 하드링크한다."""
        dest = self.root / "courses" / course_dir / filename
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
            "SELECT progress_pct FROM vod WHERE cmid=?", (row["cmid"],)
        ).fetchone()
        old_pct = old["progress_pct"] if old else None
        new_pct = row.get("progress_pct")
        if old is not None and (old_pct or 0) < 100 and (new_pct or 0) >= 100:
            title = self.db.execute(
                "SELECT title FROM activity WHERE cmid=?", (row["cmid"],)
            ).fetchone()
            self.record_change(
                "vod_completed", row.get("course_id"), row["cmid"],
                title[0] if title else None, str(old_pct or 0), str(new_pct), None,
            )
        self._upsert("vod", "cmid", row)

    def save_post(self, row: dict) -> None:
        row = dict(row)
        row.setdefault("fetched_at", _now())
        old = self.db.execute(
            "SELECT 1 FROM post WHERE cmid=? AND modname=? AND post_id=?",
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
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self.db.execute(
            f"INSERT OR REPLACE INTO post ({cols}) VALUES ({marks})", tuple(row.values())
        )

    def has_post(self, cmid: int, modname: str, post_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM post WHERE cmid=? AND modname=? AND post_id=?",
                (cmid, modname, post_id),
            ).fetchone()
            is not None
        )

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
            "vods_done": q("SELECT COUNT(*) FROM vod WHERE progress_pct>=100"),
            "submissions": q("SELECT COUNT(*) FROM submission"),
            "submitted": q("SELECT COUNT(*) FROM submission WHERE submitted=1"),
            "files": q("SELECT COUNT(*) FROM file"),
            "file_bytes": q("SELECT COALESCE(SUM(bytes),0) FROM file"),
            "transcripts": q("SELECT COUNT(*) FROM transcript"),
            "posts": q("SELECT COUNT(*) FROM post"),
            "boards": q("SELECT COUNT(DISTINCT cmid) FROM post"),
        }

    def write_json(self, relpath: str, obj) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        return path
