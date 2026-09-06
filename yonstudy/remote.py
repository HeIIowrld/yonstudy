"""rclone remote에 강의 자료와 게시글을 저장한다."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .daily import SEOUL
from .export import _safe, term_folder
from .flat_layout import canonical_filename, lesson_number, resource_filename, week_number


class RemoteStorageError(RuntimeError):
    pass


@dataclass
class RemoteSyncResult:
    destination: str
    mode: str = "incremental-rclone"
    courses: int = 0
    material_files: int = 0
    board_attachments: int = 0
    assignment_files: int = 0
    posts: int = 0
    uploaded_files: int = 0
    uploaded_bytes: int = 0
    skipped_files: int = 0
    moved_files: int = 0
    missing_sources: int = 0


class RcloneRemote:
    """rclone이 설정한 원격 저장소에 파일을 올린다."""

    def __init__(self, remote: str, *, timeout: int = 6 * 3600):
        self.remote = remote.rstrip("/")
        self.timeout = timeout
        self.exe = shutil.which("rclone")
        self._listings: dict[str, dict[str, int]] = {}
        if not self.exe:
            raise RemoteStorageError("rclone이 설치되어 있지 않습니다")
        if ":" not in self.remote or self.remote.startswith(":"):
            raise RemoteStorageError(
                "remote는 'rclone-remote:경로' 형식으로 지정해야 합니다"
            )
        remote_name = self.remote.split(":", 1)[0] + ":"
        try:
            proc = subprocess.run(
                [self.exe, "listremotes"], capture_output=True, text=True,
                timeout=30, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteStorageError("rclone remote 목록 조회 시간 초과") from exc
        if proc.returncode != 0:
            raise RemoteStorageError(
                (proc.stderr or proc.stdout).strip() or "rclone 설정 조회 실패"
            )
        if remote_name not in set(proc.stdout.splitlines()):
            raise RemoteStorageError(
                f"rclone remote가 설정되어 있지 않습니다: {remote_name}"
            )

    def _target(self, relative: str) -> str:
        return f"{self.remote}/{relative.lstrip('/')}"

    def _load_term(self, term: str) -> dict[str, int]:
        if term in self._listings:
            return self._listings[term]
        proc = subprocess.run(
            [self.exe, "lsjson", self._target(term), "--recursive", "--files-only"],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip()
            if (
                "directory not found" in message.lower()
                or "not found" in message.lower()
            ):
                listing: dict[str, int] = {}
            else:
                raise RemoteStorageError(message or f"원격 목록 조회 실패: {term}")
        else:
            try:
                items = json.loads(proc.stdout or "[]")
            except json.JSONDecodeError as exc:
                raise RemoteStorageError(
                    f"원격 목록 응답을 해석하지 못했습니다: {term}"
                ) from exc
            listing = {
                f"{term}/{item['Path']}": int(item.get("Size") or 0)
                for item in items if not item.get("IsDir") and item.get("Path")
            }
        self._listings[term] = listing
        return listing

    def exists(self, relative: str, size: int | None = None) -> bool:
        relative = str(PurePosixPath(relative))
        term = relative.split("/", 1)[0]
        remote_size = self._load_term(term).get(relative)
        return remote_size is not None and (size is None or remote_size == int(size))

    def upload_bytes(self, relative: str, body: bytes, *, force: bool = False) -> bool:
        """업로드했으면 True, 같은 크기의 원격 파일을 건너뛰었으면 False."""
        relative = str(PurePosixPath(relative))
        if not force and self.exists(relative, len(body)):
            return False
        try:
            proc = subprocess.run(
                [self.exe, "rcat", self._target(relative), "--size", str(len(body))],
                input=body, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteStorageError(f"원격 업로드 시간 초과: {relative}") from exc
        if proc.returncode != 0:
            output = (proc.stdout + b"\n" + proc.stderr).decode(
                "utf-8", "replace"
            ).strip()
            raise RemoteStorageError(output[-2000:] or f"원격 업로드 실패: {relative}")
        if "/" in relative:
            term = relative.split("/", 1)[0]
            self._load_term(term)[relative] = len(body)
        return True

    def upload_file(self, relative: str, source: str | Path, *, force: bool = False) -> bool:
        """큰 파일은 메모리에 올리지 않고 전송한다."""
        relative = str(PurePosixPath(relative))
        source = Path(source)
        size = source.stat().st_size
        if not force and self.exists(relative, size):
            return False
        try:
            proc = subprocess.run(
                [self.exe, "copyto", str(source), self._target(relative)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteStorageError(f"원격 업로드 시간 초과: {relative}") from exc
        if proc.returncode != 0:
            output = (proc.stdout + b"\n" + proc.stderr).decode(
                "utf-8", "replace"
            ).strip()
            raise RemoteStorageError(output[-2000:] or f"원격 업로드 실패: {relative}")
        term = relative.split("/", 1)[0]
        self._load_term(term)[relative] = size
        return True

    def move(self, source: str, target: str, *, size: int | None = None) -> bool:
        """기존 원격 파일을 새 경로로 옮겼으면 True, 원본이 없으면 False."""
        source = str(PurePosixPath(source))
        target = str(PurePosixPath(target))
        if source == target:
            return self.exists(target, size)
        if self.exists(target, size):
            return True
        if not self.exists(source, size):
            return False
        try:
            proc = subprocess.run(
                [self.exe, "moveto", self._target(source), self._target(target)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteStorageError(f"원격 파일 이동 시간 초과: {source}") from exc
        if proc.returncode != 0:
            output = (proc.stdout + b"\n" + proc.stderr).decode(
                "utf-8", "replace"
            ).strip()
            raise RemoteStorageError(output[-2000:] or f"원격 파일 이동 실패: {source}")
        term = source.split("/", 1)[0]
        listing = self._load_term(term)
        moved_size = listing.pop(source, int(size or 0))
        listing[target] = moved_size
        return True

    def file_path(
        self, *, year: str, semester: str, course_slug: str,
        activity_title: str, file_id: int, name: str, role: str,
        section_idx: int | None = None, section_name: str | None = None,
        open_from: str | None = None, saved_at: str | None = None,
    ) -> str:
        term = _safe(term_folder(year, semester))
        course = _safe(course_slug)
        filename = _safe(f"{file_id}_{name}")
        if role == "resource":
            filename = resource_filename(
                section_idx=section_idx, section_name=section_name,
                activity_title=activity_title, name=name, file_id=file_id,
                open_from=open_from, saved_at=saved_at,
            )
            parts = (term, course, filename)
        elif role == "post":
            parts = (term, course, "게시판_첨부", _safe(activity_title), filename)
        elif role == "submission":
            parts = (term, course, "제출물", _safe(activity_title), filename)
        elif role == "introattachment":
            parts = (term, course, "과제자료", _safe(activity_title), filename)
        else:
            parts = (term, course, "기타", _safe(activity_title), filename)
        return str(PurePosixPath(*parts))

    def video_path(
        self, *, year: str, semester: str, course_slug: str,
        title: str, cmid: int, section_idx: int | None = None,
        section_name: str | None = None,
    ) -> str:
        """평면형 개인 아카이브 규칙에 맞는 강의영상 경로."""
        week = week_number(section_idx, section_name, title)
        lesson = lesson_number(title)
        filename = canonical_filename(
            week=week, lesson=lesson, kind="강의영상", title=title,
            stable_id=f"cmid{cmid}", extension=".mp4",
        )
        return str(PurePosixPath(
            _safe(term_folder(year, semester)), _safe(course_slug), filename,
        ))

    def post_path(
        self, *, year: str, semester: str, course_slug: str,
        board_title: str, post_id: str, subject: str, written_at: str | None,
    ) -> str:
        day = "".join(c for c in (written_at or "")[:10] if c.isdigit()) or "날짜없음"
        filename = _safe(f"{day}_{post_id}_{subject}", f"글_{post_id}", 140) + ".md"
        return str(PurePosixPath(
            _safe(term_folder(year, semester)), _safe(course_slug),
            "QNA_공지", _safe(board_title), filename,
        ))


def sync_remote_tree(
    store, sink: RcloneRemote, *, year: str, semester: str
) -> RemoteSyncResult:
    """DB에 있는 한 학기 자료와 글을 remote와 맞춘다."""
    result = RemoteSyncResult(destination=sink.remote)
    courses = store.query(
        """
        SELECT course_id,year,semester,name,title,slug
          FROM course WHERE year=? AND semester=? ORDER BY name
        """,
        (year, semester),
    )
    result.courses = len(courses)

    for course in courses:
        files = store.query(
            """
            SELECT f.*,a.title AS activity_title,a.section_idx,a.section_name,a.open_from
              FROM file f LEFT JOIN activity a ON a.cmid=f.cmid
             WHERE f.course_id=?
               AND f.role IN ('resource','post','submission','introattachment')
             ORDER BY f.role,f.cmid,f.name
            """,
            (course["course_id"],),
        )
        for row in files:
            role = row["role"]
            if role == "resource":
                result.material_files += 1
            elif role == "post":
                result.board_attachments += 1
            else:
                result.assignment_files += 1
            desired = sink.file_path(
                year=course["year"], semester=course["semester"],
                course_slug=course["slug"] or course["title"] or course["name"],
                activity_title=row["activity_title"] or f"활동_{row['cmid']}",
                file_id=row["id"], name=row["name"] or f"파일_{row['cmid']}",
                role=role, section_idx=row["section_idx"],
                section_name=row["section_name"], open_from=row["open_from"],
                saved_at=row["saved_at"],
            )
            # 강의자료는 예전 `강의자료/ID_이름` 경로를 재사용하지 않고
            # 과목 루트의 주차 접두어 경로로 한 번만 이동한다.
            previous = row["remote_path"]
            if role == "resource" and previous and previous != desired:
                if sink.move(previous, desired, size=row["bytes"]):
                    store.update_file_remote(row["url"], role, desired, "ok")
                    result.moved_files += 1
                    continue
            relative = desired if role == "resource" else (previous or desired)
            if sink.exists(relative, row["bytes"]):
                store.update_file_remote(row["url"], role, relative, "ok")
                result.skipped_files += 1
                continue
            source = store.blob_path(row["sha256"]) if row["sha256"] else None
            if source is None or not source.is_file():
                result.missing_sources += 1
                store.update_file_remote(row["url"], role, relative, "missing")
                continue
            size = row["bytes"] or source.stat().st_size
            if sink.upload_file(relative, source):
                result.uploaded_files += 1
                result.uploaded_bytes += size
            else:
                result.skipped_files += 1
            store.update_file_remote(row["url"], role, relative, "ok")

        posts = store.query(
            """
            SELECT p.post_id,p.modname,p.subject,p.writer,p.written_at,p.url,p.body,
                   p.cmid,a.title AS board_title
              FROM post p LEFT JOIN activity a ON a.cmid=p.cmid
             WHERE p.course_id=?
               AND NOT (p.modname='forum' AND p.post_id LIKE 't%' AND p.body IS NULL)
             ORDER BY p.cmid,p.written_at,p.id
            """,
            (course["course_id"],),
        )
        result.posts += len(posts)
        for post in posts:
            relative = sink.post_path(
                year=course["year"], semester=course["semester"],
                course_slug=course["slug"] or course["title"] or course["name"],
                board_title=post["board_title"] or f"게시판_{post['cmid']}",
                post_id=post["post_id"], subject=post["subject"] or "제목 없음",
                written_at=post["written_at"],
            )
            body = "\n".join([
                f"# {post['subject'] or '(제목 없음)'}", "",
                f"- 과목: {course['title'] or course['name']}",
                f"- 게시판: {post['board_title'] or post['modname']}",
                f"- 작성자: {post['writer'] or '알 수 없음'}",
                f"- 작성일: {post['written_at'] or '알 수 없음'}",
                f"- 원문: {post['url'] or '없음'}", "",
                post["body"] or "(본문을 수집하지 못했습니다.)", "",
            ]).encode("utf-8")
            if sink.upload_bytes(relative, body):
                result.uploaded_files += 1
                result.uploaded_bytes += len(body)
            else:
                result.skipped_files += 1

    manifest = {
        **asdict(result), "year": year, "semester": semester,
        "generated_at": datetime.now(SEOUL).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "direct-no-local-staging-incremental-no-delete",
    }
    manifest_body = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    # manifest는 학기 목록 밖의 루트 파일이므로 매번 최신 상태로 덮어쓴다.
    sink.upload_bytes("yonstudy-manifest.json", manifest_body, force=True)
    store.commit()
    return result


# 0.1에서 공개한 결과 타입 이름이다.
DirectSyncResult = RemoteSyncResult

__all__ = [
    "DirectSyncResult",
    "RemoteStorageError",
    "RemoteSyncResult",
    "RcloneRemote",
    "sync_remote_tree",
]
