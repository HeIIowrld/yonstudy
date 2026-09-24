"""첨부 노트북의 지침과 시작 코드를 실행 없이 과제 명세로 추출한다."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import quote, urlsplit

from .markdown_view import markdown_to_html


MAX_NOTEBOOK_BYTES = 10 * 1024 * 1024


def _reject_constant(value: str):
    raise ValueError(f"JSON에서 지원하지 않는 값입니다: {value}")


def _source(cell: dict, number: int) -> str:
    source = cell.get("source")
    if isinstance(source, list) and all(isinstance(line, str) for line in source):
        source = "".join(source)
    if not isinstance(source, str):
        raise ValueError(f"노트북 {number}번 셀의 source는 문자열 또는 문자열 목록이어야 합니다")
    return source


def _fenced(source: str, language: str = "") -> str:
    # A code cell may itself contain a Markdown fence in a string/comment.
    longest = max((len(match.group()) for match in re.finditer(r"`+", source)), default=0)
    fence = "`" * max(3, longest + 1)
    ending = "" if source.endswith("\n") else "\n"
    return f"{fence}{language}\n{source}{ending}{fence}"


def parse_notebook_spec(body: bytes, filename: str, source_url: str) -> tuple[str, str]:
    """nbformat 4의 셀 원문만 Markdown/HTML로 반환한다.

    markdown, code, raw 셀 순서와 코드 공백을 보존한다. 출력·실행 횟수·
    메타데이터·첨부 객체는 읽지 않는다. Markdown은 제목과 목록으로 표시하고
    코드·수식·원시 HTML은 실행 없이 보존한다.
    """
    if not isinstance(body, bytes):
        raise ValueError("노트북 본문은 bytes여야 합니다")
    if len(body) > MAX_NOTEBOOK_BYTES:
        raise ValueError("노트북 크기가 10 MiB 제한을 초과했습니다")
    if not isinstance(filename, str) or not filename.lower().endswith(".ipynb"):
        raise ValueError("지원하는 노트북 파일 형식은 .ipynb입니다")
    if not isinstance(source_url, str) or re.search(r"[\x00-\x20\x7f]", source_url):
        raise ValueError("노트북 원문 URL이 올바르지 않습니다")
    try:
        parsed_url = urlsplit(source_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError
    except ValueError as exc:
        raise ValueError("노트북 원문 URL은 HTTP 또는 HTTPS 주소여야 합니다") from exc
    try:
        notebook = json.loads(body.decode("utf-8-sig"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("노트북 JSON을 읽을 수 없습니다") from exc
    if not isinstance(notebook, dict):
        raise ValueError("노트북 JSON의 최상위 값은 객체여야 합니다")
    if type(notebook.get("nbformat")) is not int or notebook["nbformat"] != 4:
        raise ValueError("지원하는 노트북 버전은 nbformat 4입니다")
    minor = notebook.get("nbformat_minor", 0)
    if type(minor) is not int or minor < 0:
        raise ValueError("노트북 nbformat_minor는 0 이상의 정수여야 합니다")
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        raise ValueError("노트북 cells는 셀 목록이어야 합니다")

    filename_label = re.sub(r"\s+", " ", filename).strip()
    # Escape Markdown punctuation without changing filenames in the HTML view.
    markdown_name = re.sub(r"([\\`*\[\]<>])", r"\\\1", filename_label)
    markdown_url = quote(source_url, safe=":/?&=#%+;,@!$'~*-._")
    markdown_parts = [f"## 첨부 노트북: {markdown_name}", f"- 원문: [원본 노트북 (.ipynb)]({markdown_url})"]
    html_parts = [
        f"<section><h2>첨부 노트북: {html.escape(filename_label)}</h2>",
        f'<p>원문: <a href="{html.escape(source_url, quote=True)}">원본 노트북 (.ipynb)</a></p>',
    ]
    included = 0
    for number, cell in enumerate(cells, 1):
        if not isinstance(cell, dict):
            raise ValueError(f"노트북 {number}번 셀은 객체여야 합니다")
        kind = cell.get("cell_type")
        if not isinstance(kind, str) or kind not in {"markdown", "code", "raw"}:
            raise ValueError(f"노트북 {number}번 셀의 형식을 지원하지 않습니다")
        source = _source(cell, number)
        if not source.strip():
            continue
        included += 1
        if kind == "markdown":
            markdown_parts.append(source)
            html_parts.append('<div class="notebook-markdown">' + markdown_to_html(source, source_url) + '</div>')
        else:
            markdown_parts.append(_fenced(source, "text" if kind == "raw" else ""))
            html_parts.append(f"<pre><code>{html.escape(source)}</code></pre>")
    if not included:
        raise ValueError("노트북에 추출할 지침이나 코드가 없습니다")
    html_parts.append("</section>")
    return "\n\n".join(markdown_parts), "\n".join(html_parts)
