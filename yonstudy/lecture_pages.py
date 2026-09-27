"""전사본별·주차별 요약을 탐색 가능한 Markdown 문서로 만든다."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .export import _reading_page, _safe, course_archive_root
from .flat_layout import MEDIA_DIR, SUMMARY_DIR
from .markdown_view import markdown_to_html


def week_for_source(source: str) -> int:
    """명시적 주차를 우선하고, 짧은 녹음 파일명의 첫 번호만 보수적으로 사용한다."""
    name = Path(source).name
    if re.match(r"(?i)^\s*(?:lec|lecture)\b", name):
        return 0
    for pattern in (
        r"(?i)^W0*(\d{1,2})[-_]L\d{1,2}",
        r"(?i)(?<!\d)0*(\d{1,2})\s*주차",
        r"(?i)\bweek\s*0*(\d{1,2})\b",
        r"(?i)^[^\d]{1,18}[\s_-]*0*(\d{1,2})(?:[\s_-]*\d{1,2})?(?=\.|$|[\s_-])",
    ):
        match = re.search(pattern, name)
        if match:
            week = int(match.group(1))
            if 1 <= week <= 20:
                return week
    return 0


def week_label(week: int) -> str:
    return f"{week:02d}주차" if week else "주차미확인"


def week_for_asset(source: str) -> int:
    """명시적 주차와 강의자료의 `NN-NN` 배포 순서만 사용한다."""
    parts = PurePosixPath(source).parts
    for part in parts[:-1]:
        match = re.search(r"(?i)(?<!\d)(\d{1,2})\s*주차|\bweek\s*(\d{1,2})\b", part)
        if match:
            week = int(match.group(1) or match.group(2))
            if 1 <= week <= 20:
                return week
    week = week_for_source(source)
    if week:
        return week
    if parts[0] == "강의자료":
        match = re.match(r"^(\d{2})-\d{2}(?:\D|$)", parts[-1])
        if match and 1 <= int(match.group(1)) <= 20:
            return int(match.group(1))
    return 0


def _file_week_label(week: int) -> str:
    return week_label(week) if week else "99_주차미확인"


def stable_source(source: str) -> str:
    """미디어 보관 폴더 이동은 전사·주차 요약의 내용 변경으로 취급하지 않는다."""
    prefix = MEDIA_DIR + "/"
    return source[len(prefix):] if source.startswith(prefix) else source


def resolve_source(source: str, inventory: set[str]) -> str:
    """요약 DB가 아직 예전 경로를 가리켜도 현재 원본을 연결한다."""
    if source in inventory:
        return source
    moved = MEDIA_DIR + "/" + stable_source(source)
    return moved if moved in inventory else source


def _lecture_name(source: str) -> str:
    name = Path(source).name
    name = re.sub(r"\.(?:ko|en|ja|zh|fr|de|es)(?:[-_][a-z0-9]+)?\.(?:srt|vtt)$", "", name, flags=re.I)
    return re.sub(r"\.(?:srt|vtt)$", "", name, flags=re.I)


def lecture_page_path(course: dict, source: str) -> str:
    week = week_for_source(source)
    title = _safe(_lecture_name(source), limit=105)
    identity = hashlib.sha1(stable_source(source).encode("utf-8")).hexdigest()[:10]
    return str(course_archive_root(course) / SUMMARY_DIR / f"{_file_week_label(week)}__10_{title}__{identity}_요약.md")


def week_page_path(course: dict, week: int) -> str:
    name = f"W{week:02d}-00__주차학습_자동생성.md" if week else "W99-00__주차미확인_자동생성.md"
    return str(course_archive_root(course) / SUMMARY_DIR / name)


def week_html_path(course: dict, week: int) -> str:
    return str(course_archive_root(course) / PurePosixPath(week_page_path(course, week)).name.replace(".md", ".html"))


def lecture_index_path(course: dict) -> str:
    return str(course_archive_root(course) / SUMMARY_DIR / "00_학습목차_자동생성.md")


def week_digest(lectures: list[dict]) -> str:
    rows = [(week_for_source(row["source"]), stable_source(row["source"]), row["summary"], row.get("important_points", []))
            for row in lectures]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def _md(value: str) -> str:
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", value)


def _link(path: str) -> str:
    return quote(path, safe="/")


def _weeks_from_summary(summary: dict, lectures: list[dict]) -> dict[int, dict]:
    if summary.get("weeks_source_hash") != week_digest(lectures):
        return {}
    try:
        rows = json.loads(summary.get("weeks_json") or "[]")
        return {int(row["week"]): row for row in rows}
    except (ValueError, TypeError, KeyError):
        return {}


_MEDIA = {".mp4": "영상", ".mkv": "영상", ".mov": "영상", ".webm": "영상",
          ".m4a": "녹음", ".mp3": "녹음", ".wav": "녹음", ".flac": "녹음", ".aac": "녹음"}
_MATERIAL = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".hwp", ".hwpx", ".ipynb"}
_SEPARATE = {"과제자료", "제출물", "QNA_공지", "게시판_첨부", "강의요약", "tmp", "output"}


def _asset_kind(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if not parts or any(part.startswith(".") for part in parts) or parts[0] in _SEPARATE:
        return None
    if path in {"강좌정보.md", "강좌내용요약_자동생성.md", "폴더안내.md"}:
        return None
    if parts[0] == "강의자료" or (len(parts) == 1 and "__강의자료__" in parts[0]):
        return "강의자료"
    if parts[-1].startswith(("00_학습목차_자동생성", "W99-00__주차미확인_자동생성")):
        return None
    if re.match(r"W\d\d-00__주차학습_자동생성\.(?:md|html)$", parts[-1]):
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _MEDIA:
        return _MEDIA[suffix]
    if suffix in {".srt", ".vtt"}:
        return "전사본"
    if suffix in _MATERIAL:
        return "강의자료"
    if suffix == ".md" and (len(parts) == 1 or parts[0] in {"md", "강의자료", "자료"}):
        return "학습노트"
    return None


def _html_document(markdown: bytes, title: str) -> bytes:
    return _reading_page(title, markdown_to_html(markdown.decode("utf-8")))


def _from_summary_folder(markdown: bytes) -> bytes:
    """루트 HTML과 동일한 내용을 강의요약 폴더의 Markdown에서도 연결한다."""
    return re.sub(rb"\]\(([^)]+)\)", lambda match: b"](../" + match.group(1) + b")", markdown)


def generated_lecture_documents(
    course: dict, summary: dict | None, *, inventory: list[str] | tuple[str, ...] = (),
) -> dict[str, bytes]:
    """원본 파일 목록을 바탕으로 루트 HTML과 `강의요약/`의 상세 문서를 만든다."""
    lectures = json.loads(summary["lectures_json"]) if summary else []
    inventory_set = set(inventory)
    groups: dict[int, list[dict]] = {}
    for row in lectures:
        groups.setdefault(week_for_source(row["source"]), []).append(row)
    assets: dict[int, dict[str, list[str]]] = {}
    for path in sorted(set(inventory), key=str.casefold):
        kind = _asset_kind(path)
        if kind:
            assets.setdefault(week_for_asset(path), {}).setdefault(kind, []).append(path)
    for row in lectures:
        kinds = assets.setdefault(week_for_source(row["source"]), {})
        for kind, original in (("전사본", row["source"]), ("영상·녹음", row.get("media"))):
            path = resolve_source(original, inventory_set) if original else None
            if path and all(path not in paths for paths in kinds.values()):
                kinds.setdefault(kind, []).append(path)
    all_weeks = sorted(set(groups) | set(assets), key=lambda n: (n == 0, n))
    weeks = _weeks_from_summary(summary, lectures) if summary else {}
    docs: dict[str, bytes] = {}
    title = course.get("name") or course.get("title") or "강좌"
    index = [f"# {_md(title)} · 학습목차", "",
             "> 강의·녹음·전사본·자료를 파일명 기준으로 묶은 탐색 지도입니다. 주차가 불명확한 자료는 맨 아래에 둡니다.", ""]
    if summary:
        links = ["[강좌 전체 내용 요약](강의요약/강좌내용요약_자동생성.md)"]
        if "과제자료/index.html" in inventory:
            links.append("[과제 목록](과제자료/index.html)")
        index.extend([" · ".join(links), ""])
    for week in all_weeks:
        label = week_label(week)
        rows = groups.get(week, [])
        found = assets.get(week, {})
        weekly = weeks.get(week)
        week_file = PurePosixPath(week_page_path(course, week)).name
        counts = " · ".join(f"{kind} {len(paths)}" for kind, paths in found.items() if paths)
        week_html = week_file.removesuffix(".md") + ".html"
        index.extend([f"## [{label}]({_link(week_html)})", ""])
        if weekly:
            index.extend([_md(weekly["overview"]), ""])
            index.extend(f"- {_md(point)}" for point in weekly["important_points"])
            index.append("")
        elif rows:
            index.extend(["주차 통합 요약 생성 대기 중입니다. 강의별 요약을 확인하세요.", ""])
        index.extend([f"자료: {counts or '분류된 자료 없음'}", "", f"[→ {label} 자료·영상·요약 모두 보기]({_link(week_html)})", ""])

        lines = [f"# {_md(title)} · {label} 학습", "",
                 "[전체 학습목차](00_학습목차_자동생성.html)" + (" · [강좌 요약](강의요약/강좌내용요약_자동생성.md)" if summary else ""), ""]
        if weekly:
            lines.extend(["## 이번 주에 다룬 내용", "", _md(weekly["overview"]), "", "## 복습할 핵심", ""])
            lines.extend(f"- {_md(point)}" for point in weekly["important_points"])
            lines.append("")
        elif rows:
            lines.extend(["> 주차 통합 요약이 아직 없습니다. 강의별 요약을 확인하세요.", ""])
        if rows:
            lines.extend(["## 강의·녹음별 내용", ""])
            for lecture in rows:
                source = lecture["source"]
                source_path = resolve_source(source, inventory_set)
                name = _lecture_name(source)
                page = PurePosixPath(lecture_page_path(course, source)).name
                lines.append(f"- [{_md(name)}]({_link('강의요약/' + page.removesuffix('.md') + '.html')}): {_md(lecture['summary'])}")
                detail = [f"# {_md(name)} · 강의 요약", "",
                          f"[{label} 학습](../{_link(week_html)}) · [전체 학습목차](../00_학습목차_자동생성.html)", "",
                          f"- 과목: {_md(title)}", f"- 주차: {label} (파일명 기준)",
                          f"- 전사본: [원문 열기]({_link('../' + source_path)})"]
                if lecture.get("media"):
                    detail.append(f"- 영상·녹음: [원본 열기]({_link('../' + resolve_source(lecture['media'], inventory_set))})")
                detail.extend(["", "## 이 강의에서 다룬 내용", "", _md(lecture["summary"]), "", "## 중요한 내용", ""])
                detail.extend(f"- {_md(point)}" for point in lecture.get("important_points", []))
                if lecture.get("alias_of"):
                    canonical = PurePosixPath(lecture_page_path(course, lecture["alias_of"]))
                    detail.extend(["", f"> 동일한 전사 내용의 다른 파일입니다. [대표 요약]({_link(canonical.name)})도 볼 수 있습니다."])
                detail.append("")
                detail_body = "\n".join(detail).encode("utf-8")
                docs[lecture_page_path(course, source)] = detail_body
                docs[lecture_page_path(course, source).removesuffix(".md") + ".html"] = _html_document(detail_body, f"{name} · 강의 요약")
            lines.append("")
        for kind in ("영상", "녹음", "영상·녹음", "강의자료", "전사본", "학습노트"):
            paths = sorted(found.get(kind, []), key=str.casefold)
            if paths:
                lines.extend([f"## {kind}", ""])
                lines.extend(f"- [{_md(PurePosixPath(path).name)}]({_link(path)})" for path in paths)
                lines.append("")
        body = "\n".join(lines).encode("utf-8")
        docs[week_page_path(course, week)] = _from_summary_folder(body)
        docs[week_html_path(course, week)] = _html_document(body, f"{title} · {label} 학습")

    folders = Counter(path.split("/", 1)[0] for path in inventory if "/" in path and not path.startswith("."))
    other = [(name, count) for name, count in sorted(folders.items())
             if name not in {"강의요약", "tmp", "__pycache__"}]
    if other:
        index.extend(["## 별도 자료 폴더", "", "과제·공지·작업 파일은 원래 폴더에서 확인하세요.", ""])
        index.extend(f"- [{_md(name)}]({_link(name + '/')}) · 파일 {count}개" for name, count in other)
        index.append("")
    if not all_weeks:
        index.extend(["아직 주차를 분류할 수 있는 강의 자료가 없습니다.", ""])
    body = "\n".join(index).encode("utf-8")
    docs[lecture_index_path(course)] = _from_summary_folder(body)
    docs[str(course_archive_root(course) / "00_학습목차_자동생성.html")] = _html_document(body, f"{title} · 학습목차")
    docs[str(course_archive_root(course) / "강의요약" / "index.md")] = (
        "# 강의별 요약\n\n주차별 목록과 강의 자료 링크는 [학습목차](../00_학습목차_자동생성.html)에서 확인하세요.\n"
    ).encode("utf-8")
    return docs
