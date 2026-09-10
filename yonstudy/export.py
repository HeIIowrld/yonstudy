"""LearnUs 자료와 게시글을 보통 파일과 폴더로 내보낸다."""

from __future__ import annotations

import html as html_mod
import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .daily import SEOUL
from .filename_normalization import nfc


TERM_CODES = {
    "1학기": "1",
    "2학기": "2",
    "여름계절수업": "S",
    "겨울계절수업": "W",
}


def term_folder(year: str, semester: str) -> str:
    return f"{year}-{TERM_CODES.get(semester, semester)}"


def _safe(value: str | None, fallback: str = "이름없음", limit: int = 180) -> str:
    value = nfc(re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value or "")).strip(" .")
    value = value or fallback
    return value.encode("utf-8")[:limit].decode("utf-8", "ignore").rstrip(" .") or fallback


def _same_file(path: Path, source: Path, size: int | None) -> bool:
    if not path.is_file():
        return False
    if size is not None and path.stat().st_size != size:
        return False
    # copy2가 원본 mtime을 보존한다. 매일 수 GB를 다시 해시하지 않아도 새 blob은
    # 크기 또는 mtime 차이로 다시 복사된다.
    return path.stat().st_mtime_ns == source.stat().st_mtime_ns


@dataclass
class ExportResult:
    destination: str
    courses: int = 0
    material_files: int = 0
    board_attachments: int = 0
    assignment_files: int = 0
    subtitle_files: int = 0
    posts: int = 0
    course_indexes: int = 0
    assignment_specs: int = 0
    copied_files: int = 0
    copied_bytes: int = 0
    missing_blobs: int = 0


def course_archive_root(course: dict) -> PurePosixPath:
    return PurePosixPath(
        _safe(term_folder(course["year"], course["semester"])),
        _safe(course.get("slug") or course.get("title") or course.get("name")),
    )


def course_index_path(course: dict) -> str:
    return str(course_archive_root(course) / "강좌정보.md")


def assignment_spec_path(course: dict, assignment: dict, extension: str = ".md") -> str:
    from .flat_layout import canonical_filename, week_number

    title = assignment.get("title") or f"과제_{assignment['cmid']}"
    week = week_number(
        assignment.get("section_idx"), assignment.get("section_name"), title
    )
    filename = canonical_filename(
        week=week,
        lesson=0,
        kind="과제명세",
        title=title,
        stable_id=f"cmid{assignment['cmid']}",
        extension=extension,
    )
    return str(course_archive_root(course) / "과제자료" / _safe(title) / filename)


def _submission_fields(assignment: dict) -> dict:
    try:
        value = json.loads(assignment.get("fields_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def render_assignment_markdown(course: dict, assignment: dict) -> bytes:
    fields = _submission_fields(assignment)
    start = fields.get("Start date") or fields.get("시작 일시") or "알 수 없음"
    due = assignment.get("due_at") or fields.get("Due date") or "알 수 없음"
    post_date = fields.get("Post date") or "알 수 없음"
    activity_url = assignment.get("url") or "없음"
    metadata = [
        f"# {assignment.get('title') or '(제목 없음)'}", "",
        f"- 과목: {course.get('title') or course.get('name')}",
        f"- 주차: {assignment.get('section_name') or '알 수 없음'}",
        f"- 유형: {assignment.get('modname') or '알 수 없음'}",
    ]
    if fields.get("Provider"):
        metadata.append(f"- 제공자: {fields['Provider']}")
    if fields.get("Maximum marks"):
        metadata.append(f"- 총점: {fields['Maximum marks']}점")
    if fields.get("External provider"):
        metadata.append(f"- 외부 제공자: {fields['External provider']}")
    if fields.get("External contest"):
        metadata.append(f"- 외부 콘테스트: {fields['External contest']}")
    if fields.get("External problem count"):
        metadata.append(f"- 외부 문제 수: {fields['External problem count']}개")
    metadata.extend([
        f"- 상태: {assignment.get('status') or '알 수 없음'}",
        f"- 시작: {start}",
        f"- 마감: {due}",
        f"- 성적 게시: {post_date}",
        f"- 원문: {activity_url}", "",
        "## 과제 명세", "",
        assignment.get("instructions") or "(본문이 없거나 수집하지 못했습니다.)", "",
    ])
    body = "\n".join(metadata)
    return body.encode("utf-8")


def render_assignment_html(course: dict, assignment: dict) -> bytes:
    fields = _submission_fields(assignment)
    start = fields.get("Start date") or fields.get("시작 일시") or "알 수 없음"
    due = assignment.get("due_at") or fields.get("Due date") or "알 수 없음"
    post_date = fields.get("Post date") or "알 수 없음"
    esc = lambda value: html_mod.escape(str(value or "알 수 없음"))  # noqa: E731
    instructions = assignment.get("instructions_html") or (
        f"<p>{esc(assignment.get('instructions') or '본문이 없거나 수집하지 못했습니다.')}</p>"
    )
    provider = (
        f"<li>제공자: {esc(fields['Provider'])}</li>" if fields.get("Provider") else ""
    )
    maximum = (
        f"<li>총점: {esc(fields['Maximum marks'])}점</li>"
        if fields.get("Maximum marks") else ""
    )
    external = "".join(
        f"<li>{label}: {esc(fields[key])}{suffix}</li>"
        for key, label, suffix in (
            ("External provider", "외부 제공자", ""),
            ("External contest", "외부 콘테스트", ""),
            ("External problem count", "외부 문제 수", "개"),
        )
        if fields.get(key)
    )
    body = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>{esc(assignment.get('title') or '과제 명세')}</title></head><body>
<h1>{esc(assignment.get('title') or '(제목 없음)')}</h1>
<ul>
<li>과목: {esc(course.get('title') or course.get('name'))}</li>
<li>주차: {esc(assignment.get('section_name'))}</li>
<li>유형: {esc(assignment.get('modname'))}</li>
{provider}
{maximum}
{external}
<li>상태: {esc(assignment.get('status'))}</li>
<li>시작: {esc(start)}</li>
<li>마감: {esc(due)}</li>
<li>성적 게시: {esc(post_date)}</li>
<li>원문: <a href="{esc(assignment.get('url') or '')}">{esc(assignment.get('url') or '없음')}</a></li>
</ul><hr>
{instructions}
</body></html>
"""
    return body.encode("utf-8")


def render_course_index(course: dict, activities: list[dict]) -> bytes:
    lines = [
        f"# {course.get('name') or course.get('title') or '(강좌명 없음)'}", "",
        f"- 강좌명: {course.get('title') or course.get('name')}",
        f"- 학기: {course.get('year')} {course.get('semester')}",
        f"- 강좌 ID: {course.get('course_id')}",
        f"- 최종 상세 동기화: {course.get('detail_synced_at') or '알 수 없음'}", "",
        "## 현재 활동", "",
    ]
    if not activities:
        lines.append("- 확인된 활동 없음")
    for activity in activities:
        status = activity.get("submission_status")
        if activity.get("modname") == "vod":
            status = activity.get("vod_status") or status
        suffix = f" · {status}" if status else ""
        lines.append(
            f"- [{activity.get('section_name') or '강의 개요'}] "
            f"[{activity.get('modname') or '기타'}] {activity.get('title') or '(제목 없음)'}"
            f"{suffix} · {activity.get('url') or 'URL 없음'}"
        )
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def export_tree(
    store,
    destination: str | Path,
    *,
    year: str,
    semester: str,
    dry_run: bool = False,
) -> ExportResult:
    """자료/게시판을 내보낸다. 기존 대상 파일은 지우지 않는 증분 복사다."""
    # flat_layout은 term_folder를 이 모듈에서 가져오므로 순환 import를 피하기 위해
    # 실행 시점에 공통 강의 자료 파일명 함수를 불러온다.
    from .flat_layout import resource_filename

    root = Path(destination)
    result = ExportResult(destination=str(root))
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

    for course in courses:
        course = dict(course)
        term = _safe(term_folder(course["year"], course["semester"]))
        course_name = _safe(course["slug"] or course["title"] or course["name"])
        course_dir = root / term / course_name
        if not dry_run:
            for category in ("게시판_첨부", "QNA_공지", "과제자료", "제출물", "자막"):
                (course_dir / category).mkdir(parents=True, exist_ok=True)

        activities = [dict(row) for row in store.query(
            """
            SELECT a.cmid,a.modname,a.title,a.url,a.section_idx,a.section_name,
                   a.completion,s.status AS submission_status,v.status AS vod_status
              FROM activity a
              LEFT JOIN submission s ON s.cmid=a.cmid
              LEFT JOIN vod v ON v.cmid=a.cmid
             WHERE a.course_id=? AND a.present=1
             ORDER BY a.section_idx,a.cmid
            """,
            (course["course_id"],),
        )]
        index_body = render_course_index(course, activities)
        index_target = root / course_index_path(course)
        result.course_indexes += 1
        if not index_target.is_file() or index_target.read_bytes() != index_body:
            result.copied_files += 1
            result.copied_bytes += len(index_body)
            if not dry_run:
                index_target.parent.mkdir(parents=True, exist_ok=True)
                index_target.write_bytes(index_body)

        files = store.query(
            """
            SELECT f.id,f.cmid,f.role,f.name,f.sha256,f.bytes,f.saved_at,
                   a.title AS activity_title,a.section_idx,a.section_name,a.open_from
              FROM file f LEFT JOIN activity a ON a.cmid=f.cmid
             WHERE f.course_id=?
               AND f.role IN ('resource','post','submission','introattachment','subtitle')
             ORDER BY f.role,f.cmid,f.name
            """,
            (course["course_id"],),
        )
        for row in files:
            base_name = row["name"] or f"파일_{row['cmid']}"
            if row["role"] == "resource":
                result.material_files += 1
                rel = Path(resource_filename(
                    section_idx=row["section_idx"], section_name=row["section_name"],
                    activity_title=row["activity_title"], name=base_name,
                    file_id=row["id"], open_from=row["open_from"],
                    saved_at=row["saved_at"],
                ))
            elif row["role"] == "post":
                result.board_attachments += 1
                board = _safe(row["activity_title"], f"게시판_{row['cmid']}")
                rel = Path("게시판_첨부") / board / _safe(f"{row['id']}_{base_name}")
            elif row["role"] == "subtitle":
                result.subtitle_files += 1
                rel = Path("자막") / _safe(f"{row['id']}_{base_name}")
            else:
                result.assignment_files += 1
                category = "제출물" if row["role"] == "submission" else "과제자료"
                activity = _safe(row["activity_title"], f"과제_{row['cmid']}")
                rel = Path(category) / activity / _safe(f"{row['id']}_{base_name}")
            source = store.blob_path(row["sha256"]) if row["sha256"] else None
            if source is None or not source.is_file():
                result.missing_blobs += 1
                continue
            target = course_dir / rel
            if _same_file(target, source, row["bytes"]):
                continue
            result.copied_files += 1
            result.copied_bytes += row["bytes"] or source.stat().st_size
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

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
            board = _safe(post["board_title"], f"게시판_{post['cmid']}")
            day = re.sub(r"\D", "", (post["written_at"] or "")[:10]) or "날짜없음"
            filename = _safe(
                f"{day}_{post['post_id']}_{post['subject']}",
                f"글_{post['post_id']}",
                140,
            ) + ".md"
            target = course_dir / "QNA_공지" / board / filename
            body = "\n".join(
                [
                    f"# {post['subject'] or '(제목 없음)'}",
                    "",
                    f"- 과목: {course['title'] or course['name']}",
                    f"- 게시판: {post['board_title'] or post['modname']}",
                    f"- 작성자: {post['writer'] or '알 수 없음'}",
                    f"- 작성일: {post['written_at'] or '알 수 없음'}",
                    f"- 원문: {post['url'] or '없음'}",
                    "",
                    post["body"] or "(본문을 수집하지 못했습니다.)",
                    "",
                ]
            )
            encoded = body.encode("utf-8")
            if target.is_file() and target.read_bytes() == encoded:
                continue
            result.copied_files += 1
            result.copied_bytes += len(encoded)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(encoded)

        assignments = [dict(row) for row in store.query(
            """
            SELECT s.*,a.url,a.section_idx,a.section_name
              FROM submission s JOIN activity a ON a.cmid=s.cmid
             WHERE s.course_id=? AND a.present=1
             ORDER BY a.section_idx,s.cmid
            """,
            (course["course_id"],),
        )]
        result.assignment_specs += len(assignments)
        for assignment in assignments:
            for extension, body in (
                (".md", render_assignment_markdown(course, assignment)),
                (".html", render_assignment_html(course, assignment)),
            ):
                target = root / assignment_spec_path(course, assignment, extension)
                if target.is_file() and target.read_bytes() == body:
                    continue
                result.copied_files += 1
                result.copied_bytes += len(body)
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)

    manifest = {
        **asdict(result),
        "year": year,
        "semester": semester,
        "generated_at": datetime.now(SEOUL).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "incremental-no-delete",
    }
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        (root / "yonstudy-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result


# 예전 이름으로 불러오는 사용자를 위해 남겨 둔다.
export_onedrive_tree = export_tree
