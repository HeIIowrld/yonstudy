"""과제에 명시된 LeetCode 링크에서 공개 문제 명세만 보충한다."""

from __future__ import annotations

import base64
import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from .client import UA
from .html_content import clean_html, find_elements, html_to_markdown
from .lti import LtiAssignment


ORIGIN = "https://leetcode.com"
TIMEOUT = 20
_PROBLEM_LINK = re.compile(
    r"https?://(?:www\.)?leetcode\.com/problems/([a-z0-9]+(?:-[a-z0-9]+)*)(?=[/\s?#<>\]\)\"']|$)",
    re.I,
)
_QUERY = """query PublicAssignmentQuestion($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    title titleSlug content difficulty
    codeSnippets { lang langSlug code }
  }
}"""
_START = "<!-- yonstudy:leetcode:start -->"
_END = "<!-- yonstudy:leetcode:end -->"
_MAX_IMAGES = 8
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_IMAGE_TOTAL_BYTES = 8 * 1024 * 1024


def _public_asset_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.netloc.lower() == "assets.leetcode.com"
    except ValueError:
        return False


class _AssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not _public_asset_url(newurl):
            raise urllib.error.URLError("공개 이미지 저장 범위를 벗어난 주소입니다")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _image_signature_matches(mime: str, content: bytes) -> bool:
    return (
        (mime == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime == "image/jpeg" and content.startswith(b"\xff\xd8\xff"))
        or (mime == "image/gif" and content.startswith((b"GIF87a", b"GIF89a")))
        or (mime == "image/webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP")
    )


class LeetCodeArchiveError(RuntimeError):
    """연결된 공개 문제 명세를 가져올 수 없음."""


@dataclass
class LeetCodeProblem:
    slug: str
    title: str
    url: str
    difficulty: str
    statement: str
    statement_html: str
    starter_code: str = ""
    language: str = "python3"


def problem_slugs(assignment: LtiAssignment) -> list[str]:
    """원문에 직접 명시된 문제만 순서대로 중복 제거한다."""
    text = _without_previous(assignment.instructions) + "\n" + _without_previous(assignment.instructions_html)
    return list(dict.fromkeys(match.group(1).lower() for match in _PROBLEM_LINK.finditer(text)))


class LeetCodeClient:
    def __init__(self):
        # LearnUs/Gradescope 로그인 쿠키를 공개 문제 사이트로 전달하지 않는다.
        self.opener = urllib.request.build_opener()
        self.image_opener = urllib.request.build_opener(_AssetRedirectHandler())

    def _embed_public_images(self, statement_html: str) -> str:
        """본문에 포함된 공개 예시 그림을 크기를 제한해 오프라인 HTML에 보관한다."""
        cached: dict[str, str | None] = {}
        remaining = _MAX_IMAGE_TOTAL_BYTES
        for node in find_elements(statement_html, lambda tag, attrs: tag == "img"):
            url = node.attrs.get("src") or ""
            if not _public_asset_url(url):
                continue
            if url not in cached:
                if len(cached) >= _MAX_IMAGES or remaining <= 0:
                    continue
                cached[url] = None
                request = urllib.request.Request(url, headers={"User-Agent": UA})
                try:
                    with self.image_opener.open(request, timeout=10) as response:
                        if not _public_asset_url(response.geturl()):
                            continue
                        mime = response.headers.get_content_type().lower()
                        limit = min(_MAX_IMAGE_BYTES, remaining)
                        content = response.read(limit + 1)
                    remaining = max(0, remaining - len(content))
                    if len(content) > limit or not _image_signature_matches(mime, content):
                        continue
                    cached[url] = f"data:{mime};base64," + base64.b64encode(content).decode("ascii")
                except (urllib.error.URLError, OSError, ValueError):
                    # 그림 오류 때문에 읽을 수 있는 문제 본문을 버리지 않는다.
                    continue
            if cached[url]:
                original = node.outer_html()
                node.attrs["src"] = cached[url]
                statement_html = statement_html.replace(original, node.outer_html())
        return statement_html

    def fetch_problem(self, slug: str, language: str = "python3") -> LeetCodeProblem:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise LeetCodeArchiveError("잘못된 LeetCode 문제 주소입니다")
        url = f"{ORIGIN}/problems/{slug}/"
        payload = json.dumps({"query": _QUERY, "variables": {"titleSlug": slug}}).encode()
        request = urllib.request.Request(
            f"{ORIGIN}/graphql/", data=payload,
            headers={"User-Agent": UA, "Content-Type": "application/json", "Referer": url},
        )
        for attempt in range(2):
            try:
                with self.opener.open(request, timeout=TIMEOUT) as response:
                    data = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if attempt == 0 and exc.code in {429, 500, 502, 503, 504}:
                    continue
                raise LeetCodeArchiveError(f"공개 문제 요청 실패 (HTTP {exc.code})") from exc
            except (urllib.error.URLError, OSError) as exc:
                if attempt == 0:
                    continue
                raise LeetCodeArchiveError("공개 문제 서버에 연결하지 못했습니다") from exc
            except (ValueError, UnicodeError) as exc:
                raise LeetCodeArchiveError("공개 문제 응답을 해석하지 못했습니다") from exc

        question = data.get("data", {}).get("question") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
        if not isinstance(question, dict) or question.get("titleSlug") != slug:
            raise LeetCodeArchiveError("공개 문제 명세를 찾지 못했습니다")
        content = question.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LeetCodeArchiveError("공개 문제 본문이 비어 있거나 접근할 수 없습니다")
        statement_html = clean_html(content, base_url=url)
        statement = html_to_markdown(statement_html, base_url=url, heading_offset=3)
        if not statement.strip():
            raise LeetCodeArchiveError("공개 문제 본문이 비어 있습니다")
        # Markdown에는 원본 그림 주소를 유지하고 HTML에만 검증한 그림을 넣는다.
        statement_html = self._embed_public_images(statement_html)
        starter = ""
        snippets = question.get("codeSnippets")
        for snippet in snippets if isinstance(snippets, list) else []:
            if isinstance(snippet, dict) and snippet.get("langSlug") == language:
                starter = snippet.get("code") if isinstance(snippet.get("code"), str) else ""
                break
        return LeetCodeProblem(
            slug=slug, title=str(question.get("title") or slug), url=url,
            difficulty=str(question.get("difficulty") or ""), statement=statement,
            statement_html=statement_html, starter_code=starter, language=language,
        )


def _without_previous(value: str) -> str:
    return re.sub(re.escape(_START) + r".*?" + re.escape(_END), "", value, flags=re.S).strip()


def _problem_markers(slug: str) -> tuple[str, str]:
    return f"<!-- yonstudy:leetcode:problem:{slug}:start -->", f"<!-- yonstudy:leetcode:problem:{slug}:end -->"


def _previous_problem(value: str, slug: str) -> str:
    start, end = _problem_markers(slug)
    match = re.search(re.escape(start) + r".*?" + re.escape(end), value, flags=re.S)
    return match.group(0) if match else ""


def enrich_with_leetcode(
    assignment: LtiAssignment, client: LeetCodeClient | None = None, language: str = "python3",
) -> tuple[LtiAssignment, list[str]]:
    """원래 안내·제출 항목을 유지하며 본문, 예제, 제약 조건과 시작 코드를 덧붙인다."""
    slugs = problem_slugs(assignment)
    base = replace(
        assignment,
        instructions=_without_previous(assignment.instructions),
        instructions_html=_without_previous(assignment.instructions_html),
    )
    if base == assignment:
        base = assignment
    if not slugs:
        return base, []
    client = client or LeetCodeClient()
    problems: list[tuple[str, LeetCodeProblem | None]] = []
    warnings: list[str] = []
    for slug in slugs:
        try:
            problems.append((slug, client.fetch_problem(slug, language=language)))
        except LeetCodeArchiveError as exc:
            retained = bool(_previous_problem(assignment.instructions, slug)
                            and _previous_problem(assignment.instructions_html, slug))
            suffix = " (이전 수집본 유지)" if retained else ""
            warnings.append(f"LeetCode {slug}: {exc}{suffix}")
            if retained:
                problems.append((slug, None))
    if not problems:
        return base, warnings

    markdown = [_START, "## LeetCode 문제 명세", ""]
    rendered = [_START, "<section><h2>LeetCode 문제 명세</h2>"]
    for slug, problem in problems:
        if problem is None:
            markdown.extend([_previous_problem(assignment.instructions, slug), ""])
            rendered.append(_previous_problem(assignment.instructions_html, slug))
            continue
        start, end = _problem_markers(slug)
        markdown.append(start)
        rendered.append(start)
        markdown.extend([f"### {problem.title}", "", f"- 원문: [{problem.title}]({problem.url})"])
        rendered.extend([
            f"<section><h3>{html.escape(problem.title)}</h3>",
            f'<p>원문: <a href="{html.escape(problem.url, quote=True)}">{html.escape(problem.title)}</a></p>',
        ])
        if problem.difficulty:
            markdown.append(f"- 난이도: {problem.difficulty}")
            rendered.append(f"<p>난이도: {html.escape(problem.difficulty)}</p>")
        markdown.extend(["", problem.statement, ""])
        rendered.append(problem.statement_html)
        if problem.starter_code:
            fence = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", problem.starter_code)), default=0))
            code_language = "python" if problem.language in {"python", "python3"} else problem.language
            markdown.extend([f"#### 시작 코드 ({problem.language})", "", f"{fence}{code_language}", problem.starter_code, fence, ""])
            rendered.extend([
                f"<h4>시작 코드 ({html.escape(problem.language)})</h4>",
                f"<pre><code>{html.escape(problem.starter_code)}</code></pre>",
            ])
        rendered.append("</section>")
        markdown.extend([end, ""])
        rendered.append(end)
    markdown.append(_END)
    rendered.extend(["</section>", _END])
    return replace(
        assignment,
        instructions=_without_previous(assignment.instructions) + "\n\n" + "\n".join(markdown),
        instructions_html=_without_previous(assignment.instructions_html) + "\n\n" + "\n".join(rendered),
    ), warnings
