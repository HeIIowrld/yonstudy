"""노트북 지침용 작은 Markdown 읽기 뷰. HTML·코드·수식은 실행하지 않는다.

제목, 문단, 중첩 목록, 인용, 코드, 표와 일반적인 인라인 표기를 지원한다.
전체 CommonMark 구현은 아니며 지원하지 않는 문법은 원문으로 남긴다.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlsplit


_LIST = re.compile(r"^( *)([-+*]|\d+[.)])\s+(.*)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^`]*)$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)(?:\s+#+\s*)?$")
_INLINE = re.compile(
    r"(?P<escape>\\[\\`*{}\[\]()#+.!_>~-])"
    r"|(?P<code>(?P<ticks>`+)(?P<codebody>[^`]+?)(?P=ticks)(?!`))"
    r"|(?P<math>\$[^$\n]+\$)"
    r'|(?P<link>!?\[(?P<label>[^\]\n]*)\]\((?P<url><[^>\n]*>|(?:[^\s()]+|\([^()]*\))+)(?:\s+"[^"\n]*")?\))'
    r"|(?P<strong>\*\*(?P<strongbody>.+?)\*\*|__(?P<strongunder>.+?)__)"
    r"|(?P<em>\*(?P<embody>[^*\n]+)\*|(?<!\w)_(?P<emunder>[^_\n]+)_(?!\w))"
    r"|(?P<del>~~(?P<delbody>.+?)~~)"
    r"|(?P<autolink><https?://[^<>\s]+>)"
)


def safe_url(value: str, base_url: str = "") -> str:
    """Render only document links, never executable/data URLs."""
    value = html.unescape(value.strip().strip("<>"))
    if re.search(r"[\x00-\x20\x7f]", value):
        return ""
    try:
        resolved = urljoin(base_url, value)
        scheme = urlsplit(resolved).scheme.lower()
    except ValueError:
        return ""
    return resolved if scheme in {"", "http", "https", "mailto"} else ""


def _inline(source: str, base_url: str, depth: int = 0) -> str:
    if depth > 10:
        return html.escape(source)
    parts = []
    end = 0
    for match in _INLINE.finditer(source):
        parts.append(html.escape(source[end:match.start()]))
        token = match.group()
        if match.group("escape"):
            rendered = html.escape(token[1:])
        elif match.group("code"):
            rendered = "<code>" + html.escape(match.group("codebody").replace("\n", " ")) + "</code>"
        elif match.group("math"):
            rendered = '<span class="math">' + html.escape(token) + "</span>"
        elif match.group("link"):
            label = match.group("label")
            url = safe_url(match.group("url"), base_url)
            if token.startswith("!") and url and not url.startswith("mailto:"):
                rendered = f'<img src="{html.escape(url, quote=True)}" alt="{html.escape(label, quote=True)}" loading="lazy">'
            elif url:
                rendered = f'<a href="{html.escape(url, quote=True)}">{_inline(label, base_url, depth + 1)}</a>'
            else:
                rendered = html.escape(token)
        elif match.group("autolink"):
            url = safe_url(token, base_url)
            rendered = f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>' if url else html.escape(token)
        else:
            if match.group("strong"):
                tag, body = "strong", match.group("strongbody") or match.group("strongunder")
            elif match.group("em"):
                tag, body = "em", match.group("embody") or match.group("emunder")
            else:
                tag, body = "del", match.group("delbody")
            rendered = f"<{tag}>" + _inline(body, base_url, depth + 1) + f"</{tag}>"
        parts.append(rendered)
        end = match.end()
    parts.append(html.escape(source[end:]))
    return "".join(parts).replace("  \n", "<br>\n")


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _table_separator(line: str) -> bool:
    return "|" in line and all(re.fullmatch(r":?-{3,}:?", cell) for cell in _table_cells(line))


def _starts_block(line: str) -> bool:
    return bool(_HEADING.match(line) or _FENCE.match(line) or _LIST.match(line)
                or line.lstrip().startswith((">", "$$"))
                or re.fullmatch(r" {0,3}(?:-{3,}|\*{3,}|_{3,})\s*", line))


def markdown_to_html(source: str, base_url: str = "", *, _depth: int = 0) -> str:
    if _depth > 20:
        return "<pre>" + html.escape(source) + "</pre>"
    lines = source.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if match := _FENCE.match(line):
            fence, language = match.groups()
            i += 1
            code = []
            while i < len(lines) and not re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", lines[i]):
                code.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            attr = f' class="language-{language.strip()}"' if re.fullmatch(r"[\w+.-]+", language.strip()) else ""
            result.append(f"<pre><code{attr}>" + html.escape("\n".join(code)) + "</code></pre>")
        elif match := _HEADING.match(line):
            level, body = len(match.group(1)), match.group(2)
            result.append(f"<h{level}>" + _inline(body, base_url) + f"</h{level}>")
            i += 1
        elif re.fullmatch(r" {0,3}(?:-{3,}|\*{3,}|_{3,})\s*", line):
            result.append("<hr>")
            i += 1
        elif line.lstrip().startswith("$$"):
            math = [line]
            i += 1
            if line.count("$$") < 2:
                while i < len(lines):
                    math.append(lines[i])
                    i += 1
                    if "$$" in math[-1]:
                        break
            result.append('<pre class="math">' + html.escape("\n".join(math)) + "</pre>")
        elif line.lstrip().startswith(">"):
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(re.sub(r"^ *> ?", "", lines[i]))
                i += 1
            result.append("<blockquote>" + markdown_to_html("\n".join(quote), base_url, _depth=_depth + 1) + "</blockquote>")
        elif match := _LIST.match(line):
            indent, marker = len(match.group(1)), match.group(2)
            ordered = marker[0].isdigit()
            tag = "ol" if ordered else "ul"
            start = f' start="{int(marker[:-1])}"' if ordered and int(marker[:-1]) != 1 else ""
            items = []
            while i < len(lines):
                item = _LIST.match(lines[i])
                if not item or len(item.group(1)) != indent or item.group(2)[0].isdigit() != ordered:
                    break
                content = [item.group(3)]
                i += 1
                while i < len(lines):
                    following = lines[i]
                    if not following.strip():
                        if i + 1 >= len(lines) or len(lines[i + 1]) - len(lines[i + 1].lstrip()) <= indent:
                            break
                        content.append("")
                        i += 1
                        continue
                    whitespace = len(following) - len(following.lstrip())
                    if whitespace <= indent:
                        break
                    # Notebook authors commonly indent child lists by two spaces.
                    content.append(following[min(indent + 2, whitespace):])
                    i += 1
                items.append("<li>" + markdown_to_html("\n".join(content), base_url, _depth=_depth + 1) + "</li>")
            result.append(f"<{tag}{start}>" + "\n".join(items) + f"</{tag}>")
        elif i + 1 < len(lines) and "|" in line and _table_separator(lines[i + 1]):
            headings = _table_cells(line)
            result.append('<div class="table-scroll"><table><thead><tr>' + "".join("<th>" + _inline(cell, base_url) + "</th>" for cell in headings) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                result.append("<tr>" + "".join("<td>" + _inline(cell, base_url) + "</td>" for cell in _table_cells(lines[i])) + "</tr>")
                i += 1
            result.append("</tbody></table></div>")
        else:
            paragraph = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
                if i + 1 < len(lines) and _table_separator(lines[i + 1]):
                    break
                paragraph.append(lines[i])
                i += 1
            result.append("<p>" + _inline("\n".join(paragraph), base_url) + "</p>")
    return "\n".join(result)
