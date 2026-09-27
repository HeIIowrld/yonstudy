"""rclone 원격 저장소에 강의 자료와 게시글을 저장한다."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .daily import SEOUL
from .assignment_summary import current_summary
from .export import (
    _safe,
    assignment_index_entries,
    assignment_summary_path,
    assignment_spec_path,
    course_archive_root,
    course_index_path,
    course_summary_path,
    render_assignment_html,
    render_assignment_markdown,
    render_assignment_summaries,
    render_assignments_index,
    render_course_index,
    term_folder,
)
from .flat_layout import (
    canonical_filename,
    lesson_number,
    material_path,
    media_path,
    resource_filename,
    video_filename,
    week_number,
)


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
    subtitle_files: int = 0
    posts: int = 0
    course_indexes: int = 0
    assignment_specs: int = 0
    assignment_indexes: int = 0
    uploaded_files: int = 0
    uploaded_bytes: int = 0
    skipped_files: int = 0
    moved_files: int = 0
    conflict_files: int = 0
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
        source_exists = self.exists(source, size)
        target_exists = self.exists(target)
        if target_exists:
            # 원본과 대상이 둘 다 있으면 크기만으로 동일성을 가정하지 않는다.
            return not self.exists(source) and self.exists(target, size)
        if not source_exists:
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

    def move_media(self, source: str, target: str, *, size: int | None = None) -> bool:
        """완성 자막이 있는 영상과 그 SRT/VTT를 새 파일명으로 함께 옮긴다."""
        source = str(PurePosixPath(source))
        target = str(PurePosixPath(target))
        term = source.split("/", 1)[0]
        listing = self._load_term(term)
        source_path = PurePosixPath(source)
        target_path = PurePosixPath(target)
        source_prefix = f"{source_path.stem}."
        sidecars = []
        for relative, sidecar_size in listing.items():
            sidecar = PurePosixPath(relative)
            if sidecar.parent != source_path.parent:
                continue
            if not sidecar.name.startswith(source_prefix):
                continue
            if sidecar.suffix.casefold() not in {".srt", ".vtt"}:
                continue
            sidecars.append((relative, sidecar_size))
        # 실행 중인 데스크톱 전사기가 잡고 있을 수 있으므로 자막 완성 전에는 이동하지 않는다.
        if not sidecars:
            return False
        moved = self.move(source, target, size=size)
        if not moved:
            return False

        for relative, sidecar_size in sidecars:
            sidecar = PurePosixPath(relative)
            remainder = sidecar.name[len(source_path.stem) :]
            desired = str(target_path.with_name(f"{target_path.stem}{remainder}"))
            self.move(relative, desired, size=sidecar_size)
        return True

    def rmdir_empty(self, relative: str) -> bool:
        """검증된 빈 하위 폴더 하나만 제거한다."""
        relative = str(PurePosixPath(relative))
        listing = self._load_term(relative.split("/", 1)[0])
        if any(path.startswith(relative + "/") for path in listing):
            return False
        proc = subprocess.run(
            [self.exe, "rmdir", self._target(relative)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        return proc.returncode == 0

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
            parts = (term, course, *material_path(filename).parts)
        elif role == "post":
            parts = (term, course, "게시판_첨부", _safe(activity_title), filename)
        elif role == "submission":
            parts = (term, course, "제출물", _safe(activity_title), filename)
        elif role == "introattachment":
            parts = (term, course, "과제자료", _safe(activity_title), filename)
        elif role == "subtitle":
            parts = (term, course, "자막", filename)
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
        filename = video_filename(week=week, lesson=lesson, title=title, cmid=cmid)
        return str(PurePosixPath(
            _safe(term_folder(year, semester)), _safe(course_slug), *media_path(filename).parts,
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


def sync_generated_pages(store, sink: RcloneRemote, *, year: str, semester: str) -> dict:
    """미디어·원본 자료에는 손대지 않고 생성된 학습 문서만 업로드한다."""
    counts = {"course_indexes": 0, "course_summaries": 0,
              "assignment_specs": 0, "assignment_indexes": 0,
              "assignment_summaries": 0, "lecture_indexes": 0,
              "week_summaries": 0, "lecture_summaries": 0}
    term_entries = []
    courses = store.query(
        "SELECT course_id,year,semester,name,title,slug,detail_synced_at "
        "FROM course WHERE year=? AND semester=? AND enrolled=1 ORDER BY name",
        (year, semester),
    )
    for course_row in courses:
        course = dict(course_row)
        activities = [dict(row) for row in store.query(
            "SELECT a.cmid,a.modname,a.title,a.url,a.section_idx,a.section_name,a.completion,"
            "s.cmid AS assignment_cmid,s.status AS submission_status,v.status AS vod_status,"
            "pref.requirement AS submission_requirement,pref.reason AS submission_reason "
            "FROM activity a LEFT JOIN submission s ON s.cmid=a.cmid "
            "LEFT JOIN vod v ON v.cmid=a.cmid "
            "LEFT JOIN assignment_preference pref ON pref.cmid=a.cmid "
            "WHERE a.course_id=? AND a.present=1 ORDER BY a.section_idx,a.cmid",
            (course["course_id"],),
        )]
        summary_rows = store.query("SELECT * FROM course_summary WHERE course_id=?", (course["course_id"],))
        content_summary = dict(summary_rows[0]) if summary_rows else None
        prefix = str(course_archive_root(course)) + "/"
        listing = sink._load_term(term_folder(year, semester)) if hasattr(sink, "_load_term") else {}
        inventory = [path[len(prefix):] for path in listing if path.startswith(prefix)]
        sink.upload_bytes(course_index_path(course), render_course_index(course, activities, content_summary), force=True)
        counts["course_indexes"] += 1
        if content_summary:
            from .course_summary import render_course_summary
            sink.upload_bytes(course_summary_path(course), render_course_summary(course, content_summary, inventory=inventory), force=True)
            counts["course_summaries"] += 1
        from .lecture_pages import generated_lecture_documents
        for relative, body in generated_lecture_documents(course, content_summary, inventory=inventory).items():
            sink.upload_bytes(relative, body, force=True)
            if "/강의요약/" in relative and relative.endswith("_요약.md"):
                counts["lecture_summaries"] += 1
            elif "/강의요약/" not in relative and "__주차" in relative:
                counts["week_summaries"] += 1
            else:
                counts["lecture_indexes"] += 1
        assignments = [dict(row) for row in store.query(
            "SELECT s.*,a.url,a.section_idx,a.section_name,"
            "summary.source_hash AS summary_source_hash,summary.one_line,"
            "summary.deliverables_json,summary.requirements_json,"
            "pref.requirement AS submission_requirement,pref.reason AS submission_reason "
            "FROM submission s JOIN activity a ON a.cmid=s.cmid "
            "LEFT JOIN assignment_summary summary ON summary.cmid=s.cmid "
            "LEFT JOIN assignment_preference pref ON pref.cmid=a.cmid "
            "WHERE s.course_id=? AND a.present=1 ORDER BY a.section_idx,s.cmid",
            (course["course_id"],),
        )]
        assignments = [current_summary(row) for row in assignments]
        summary_body = render_assignment_summaries(course, assignments)
        if summary_body:
            sink.upload_bytes(assignment_summary_path(course), summary_body, force=True)
            counts["assignment_summaries"] += 1
        for assignment in assignments:
            sink.upload_bytes(
                assignment_spec_path(course, assignment, ".md"),
                render_assignment_markdown(course, assignment), force=True,
            )
            sink.upload_bytes(
                assignment_spec_path(course, assignment, ".html"),
                render_assignment_html(course, {**assignment, "index_path": "../index.html"}),
                force=True,
            )
            counts["assignment_specs"] += 1
        if assignments:
            index_base = str(course_archive_root(course) / "과제자료")
            sink.upload_bytes(
                str(PurePosixPath(index_base) / "index.html"),
                render_assignments_index(
                    assignment_index_entries(course, assignments, base=index_base),
                    title=f"{course.get('title') or course.get('name')} · 과제 읽기",
                ), force=True,
            )
            counts["assignment_indexes"] += 1
            term_entries.extend(assignment_index_entries(
                course, assignments, base=_safe(term_folder(year, semester)),
            ))
    if term_entries:
        sink.upload_bytes(
            str(PurePosixPath(_safe(term_folder(year, semester))) / "과제목록.html"),
            render_assignments_index(term_entries, title=f"{year} {semester} · 과제 읽기"),
            force=True,
        )
        counts["assignment_indexes"] += 1
    return counts


def sync_study_maps(
    store, sink: RcloneRemote, *, year: str, semester: str, dry_run: bool = False,
) -> dict:
    """이미 저장된 한 학기 전체 자료를 색인한다. 원본 파일은 변경하지 않는다."""
    from .lecture_pages import generated_lecture_documents

    term = term_folder(year, semester)
    listing = sink._load_term(term)
    result = {"term": term, "courses": 0, "source_files": 0, "study_pages": 0}
    for row in store.query(
        "SELECT course_id,year,semester,name,title,slug FROM course "
        "WHERE year=? AND semester=? AND enrolled=1 ORDER BY name",
        (year, semester),
    ):
        course = dict(row)
        prefix = str(course_archive_root(course)) + "/"
        inventory = [path[len(prefix):] for path in listing if path.startswith(prefix)]
        if not inventory:
            continue
        summary_rows = store.query("SELECT * FROM course_summary WHERE course_id=?", (course["course_id"],))
        summary = dict(summary_rows[0]) if summary_rows else None
        documents = generated_lecture_documents(course, summary, inventory=inventory)
        result["courses"] += 1
        result["source_files"] += len(inventory)
        result["study_pages"] += len(documents) + 1 + bool(summary)
        if not dry_run:
            from .export import course_index_body
            sink.upload_bytes(course_index_path(course), course_index_body(store, course), force=True)
            if summary:
                from .course_summary import render_course_summary
                sink.upload_bytes(course_summary_path(course), render_course_summary(course, summary, inventory=inventory), force=True)
            for relative, body in documents.items():
                sink.upload_bytes(relative, body, force=True)
    return result


def organize_remote_archive(
    store, sink: RcloneRemote, *, year: str, semester: str,
    dry_run: bool = True, include_media: bool = False,
) -> dict:
    """원격 원본을 종류별 한 단계 폴더로 이동하고 DB·학습 링크를 맞춘다."""
    from collections import Counter
    from .archive_layout import move_conflicts, plan_course_moves

    term = term_folder(year, semester)
    listing = sink._load_term(term)
    courses = [dict(row) for row in store.query(
        "SELECT course_id,year,semester,name,title,slug FROM course "
        "WHERE year=? AND semester=? AND enrolled=1 ORDER BY name",
        (year, semester),
    )]
    planned = [move for course in courses for move in plan_course_moves(
        course, listing, include_media=include_media,
    )]
    conflicts = move_conflicts(planned, listing)
    result = {
        "term": term, "courses": len(courses), "planned": dict(Counter(move.kind for move in planned)),
        "conflicts": len(conflicts), "conflict_examples": [move.source for move in conflicts[:10]],
        "moved": 0, "backed_up": 0, "skipped": 0, "empty_dirs_removed": 0,
    }
    if dry_run or conflicts:
        return result

    for move in planned:
        if move.kind == "generated-backup":
            continue
        if not sink.move(move.source, move.target, size=move.size):
            result["skipped"] += 1
            continue
        store.db.execute(
            "UPDATE file SET remote_path=? WHERE course_id IN "
            "(SELECT course_id FROM course WHERE year=? AND semester=?) AND remote_path=?",
            (move.target, year, semester, move.source),
        )
        if move.kind == "media":
            for recording in store.query("SELECT id,path FROM recording WHERE path IS NOT NULL"):
                old_path = recording["path"]
                if old_path == move.source or old_path.endswith("/" + move.source):
                    prefix = old_path[:-len(move.source)]
                    store.db.execute(
                        "UPDATE recording SET path=? WHERE id=?",
                        (prefix + move.target, recording["id"]),
                    )
        store.commit()
        result["moved"] += 1

    # 주차별 자료 폴더가 비었다면 그 폴더만 제거한다. 다른 사용자 폴더는 건드리지 않는다.
    old_week_dirs = {
        str(PurePosixPath(move.source).parent)
        for move in planned if move.kind == "material" and "/강의자료/" in move.source
        and PurePosixPath(move.source).parent != PurePosixPath(move.target).parent
    }
    remove_empty = getattr(sink, "rmdir_empty", None)
    if remove_empty is not None:
        for directory in sorted(old_week_dirs, key=lambda path: (-path.count("/"), path)):
            if remove_empty(directory):
                result["empty_dirs_removed"] += 1

    # 새 요약 문서와 링크를 먼저 게시한 뒤에만 이전 루트 Markdown을 백업한다.
    sync_study_maps(store, sink, year=year, semester=semester)
    listing = sink._load_term(term)
    for move in planned:
        if move.kind != "generated-backup":
            continue
        course_root = move.source.rsplit("/", 1)[0]
        current = course_root + "/강의요약/" + PurePosixPath(move.source).name
        if current not in listing or not sink.move(move.source, move.target, size=move.size):
            result["skipped"] += 1
            continue
        result["backed_up"] += 1
    return result


def sync_remote_tree(
    store, sink: RcloneRemote, *, year: str, semester: str
) -> RemoteSyncResult:
    """DB에 있는 한 학기 자료와 글을 원격 저장소와 맞춘다."""
    result = RemoteSyncResult(destination=sink.remote)
    courses = store.query(
        """
        SELECT course_id,year,semester,name,title,slug,archived_at,detail_synced_at
          FROM course
         WHERE year=? AND semester=? AND enrolled=1
         ORDER BY name
        """,
        (year, semester),
    )
    result.courses = len(courses)
    term_index_entries = []

    for course in courses:
        course = dict(course)
        activities = [dict(row) for row in store.query(
            """
            SELECT a.cmid,a.modname,a.title,a.url,a.section_idx,a.section_name,
                   a.completion,s.cmid AS assignment_cmid,
                   s.status AS submission_status,v.status AS vod_status,
                   pref.requirement AS submission_requirement,pref.reason AS submission_reason
              FROM activity a
              LEFT JOIN submission s ON s.cmid=a.cmid
              LEFT JOIN vod v ON v.cmid=a.cmid
              LEFT JOIN assignment_preference pref ON pref.cmid=a.cmid
             WHERE a.course_id=? AND a.present=1
             ORDER BY a.section_idx,a.cmid
            """,
            (course["course_id"],),
        )]
        summary_rows = store.query("SELECT * FROM course_summary WHERE course_id=?", (course["course_id"],))
        content_summary = dict(summary_rows[0]) if summary_rows else None
        index_body = render_course_index(course, activities, content_summary)
        if sink.upload_bytes(course_index_path(course), index_body, force=True):
            result.uploaded_files += 1
            result.uploaded_bytes += len(index_body)
        else:
            result.skipped_files += 1
        result.course_indexes += 1

        files = store.query(
            """
            SELECT f.*,a.title AS activity_title,a.section_idx,a.section_name,a.open_from
              FROM file f LEFT JOIN activity a ON a.cmid=f.cmid
             WHERE f.course_id=?
               AND f.role IN ('resource','post','submission','introattachment','subtitle')
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
            elif role == "subtitle":
                result.subtitle_files += 1
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
            # 예전 경로의 강의 자료는 새 평면 `강의자료/` 보관함으로 한 번만 이동한다.
            previous = row["remote_path"]
            if role == "resource" and previous and previous != desired:
                if sink.move(previous, desired, size=row["bytes"]):
                    store.update_file_remote(row["url"], role, desired, "ok")
                    result.moved_files += 1
                    continue
                if sink.exists(previous, row["bytes"]):
                    result.skipped_files += 1
                    continue
                if sink.exists(desired):
                    result.conflict_files += 1
                    store.log("archive_layout", str(row["id"]), False, f"대상 경로에 다른 크기의 파일이 있습니다: {desired}")
                    continue
            relative = desired if role == "resource" else (previous or desired)
            if sink.exists(relative, row["bytes"]):
                store.update_file_remote(row["url"], role, relative, "ok")
                result.skipped_files += 1
                continue
            if sink.exists(relative):
                result.conflict_files += 1
                store.log("remote_sync", str(row["id"]), False, f"대상 경로에 다른 크기의 파일이 있습니다: {relative}")
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

        assignments = [dict(row) for row in store.query(
            """
            SELECT s.*,a.url,a.section_idx,a.section_name,
                   summary.source_hash AS summary_source_hash,
                   summary.one_line,summary.deliverables_json,summary.requirements_json,
                   pref.requirement AS submission_requirement,pref.reason AS submission_reason
              FROM submission s JOIN activity a ON a.cmid=s.cmid
              LEFT JOIN assignment_summary summary ON summary.cmid=s.cmid
              LEFT JOIN assignment_preference pref ON pref.cmid=a.cmid
             WHERE s.course_id=? AND a.present=1
             ORDER BY a.section_idx,s.cmid
            """,
            (course["course_id"],),
        )]
        assignments = [current_summary(assignment) for assignment in assignments]
        result.assignment_specs += len(assignments)
        summary_body = render_assignment_summaries(course, assignments)
        if summary_body:
            if sink.upload_bytes(assignment_summary_path(course), summary_body, force=True):
                result.uploaded_files += 1
                result.uploaded_bytes += len(summary_body)
            else:
                result.skipped_files += 1
        for assignment in assignments:
            for extension, body in (
                (".md", render_assignment_markdown(course, assignment)),
                (".html", render_assignment_html(course, {**assignment, "index_path": "../index.html"})),
            ):
                relative = assignment_spec_path(course, assignment, extension)
                # 과제 본문·기한은 같은 길이로 수정될 수도 있어 내용이 변할 수 있는
                # 생성 문서는 크기만으로 동일하다고 판단하지 않는다.
                if sink.upload_bytes(relative, body, force=True):
                    result.uploaded_files += 1
                    result.uploaded_bytes += len(body)
                else:
                    result.skipped_files += 1

        if assignments:
            index_base = str(course_archive_root(course) / "과제자료")
            body = render_assignments_index(
                assignment_index_entries(course, assignments, base=index_base),
                title=f"{course.get('title') or course.get('name')} · 과제 읽기",
            )
            result.assignment_indexes += 1
            if sink.upload_bytes(str(PurePosixPath(index_base) / "index.html"), body, force=True):
                result.uploaded_files += 1
                result.uploaded_bytes += len(body)
            else:
                result.skipped_files += 1
            term_index_entries.extend(assignment_index_entries(
                course, assignments, base=_safe(term_folder(year, semester))
            ))

        from .lecture_pages import generated_lecture_documents
        prefix = str(course_archive_root(course)) + "/"
        listing = sink._load_term(term_folder(year, semester)) if hasattr(sink, "_load_term") else {}
        inventory = [path[len(prefix):] for path in listing if path.startswith(prefix)]
        if content_summary:
            from .course_summary import render_course_summary
            summary_body = render_course_summary(course, content_summary, inventory=inventory)
            if sink.upload_bytes(course_summary_path(course), summary_body, force=True):
                result.uploaded_files += 1
                result.uploaded_bytes += len(summary_body)
            else:
                result.skipped_files += 1
        for relative, body in generated_lecture_documents(course, content_summary, inventory=inventory).items():
            if sink.upload_bytes(relative, body, force=True):
                result.uploaded_files += 1
                result.uploaded_bytes += len(body)
            else:
                result.skipped_files += 1

    if term_index_entries:
        body = render_assignments_index(term_index_entries, title=f"{year} {semester} · 과제 읽기")
        relative = str(PurePosixPath(_safe(term_folder(year, semester))) / "과제목록.html")
        result.assignment_indexes += 1
        if sink.upload_bytes(relative, body, force=True):
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
