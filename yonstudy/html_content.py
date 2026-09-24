"""과제 HTML의 구조와 코드 공백을 보존하는 공통 변환기 (표준 라이브러리만 사용)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_IGNORE = {"script", "style", "noscript", "form", "button", "input", "textarea", "select", "iframe", "object", "embed"}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[Element | str] = field(default_factory=list)

    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    def inner_html(self) -> str:
        return "".join(html.escape(child, quote=False) if isinstance(child, str) else child.outer_html() for child in self.children)

    def outer_html(self) -> str:
        attrs = "".join(f' {key}="{html.escape(value or "", quote=True)}"' for key, value in self.attrs.items())
        opening = f"<{self.tag}{attrs}>"
        return opening if self.tag in _VOID else opening + self.inner_html() + f"</{self.tag}>"

    def find_all(self, matcher) -> list[Element]:
        result = []
        for child in self.children:
            if isinstance(child, Element):
                if matcher(child.tag, child.attrs):
                    result.append(child)
                result.extend(child.find_all(matcher))
        return result


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        # Moodle editors sometimes omit the closing tag of a paragraph/cell.
        closes = {"li": {"li"}, "p": {"p"}, "tr": {"tr"}, "td": {"td", "th"}, "th": {"td", "th"}}
        barriers = {"li": {"ul", "ol"}, "tr": {"table"}, "td": {"tr", "table"}, "th": {"tr", "table"}, "p": {"div", "section"}}
        if tag in closes:
            for index in range(len(self.stack) - 1, 0, -1):
                if self.stack[index].tag in closes[tag]:
                    del self.stack[index:]
                    break
                if self.stack[index].tag in barriers[tag]:
                    break
        element = Element(tag, dict(attrs))
        self.stack[-1].children.append(element)
        if tag not in _VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _tree(source: str) -> Element:
    parser = _TreeParser()
    parser.feed(source)
    parser.close()
    return parser.root


def find_elements(source: str, matcher) -> list[Element]:
    return _tree(source).find_all(matcher)


def _url(value: str, base_url: str) -> str:
    value = value.strip()
    if not value or re.sub(r"[\x00-\x20]", "", value).lower().startswith(("javascript:", "vbscript:", "data:")):
        return ""
    resolved = urljoin(base_url, value)
    return resolved if urlsplit(resolved).scheme.lower() in {"", "http", "https", "mailto"} else ""


def _math(node: Element) -> str | None:
    if node.tag == "script" and (node.attrs.get("type") or "").lower().startswith("math/tex"):
        delimiter = "$$" if "mode=display" in (node.attrs.get("type") or "").replace(" ", "") else "$"
        return delimiter + node.text().strip() + delimiter
    if node.tag == "math":
        annotations = node.find_all(lambda tag, attrs: tag == "annotation" and "tex" in (attrs.get("encoding") or "").lower())
        if annotations:
            delimiter = "$$" if node.attrs.get("display") == "block" else "$"
            return delimiter + annotations[0].text().strip() + delimiter
    return None


def _icon(node: Element) -> bool:
    return node.tag == "img" and "icon" in (node.attrs.get("class") or "").split() and "/theme/image.php/" in (node.attrs.get("src") or "")


def _clean_node(node: Element | str, base_url: str) -> str:
    if isinstance(node, str):
        return html.escape(node, quote=False)
    if (math := _math(node)) is not None:
        return html.escape(math)
    if node.tag in _IGNORE or _icon(node):
        return ""
    # Keep document semantics, including merged table cells and MathML, but not
    # event handlers or page styling that depends on the original application.
    allowed = {"href", "src", "alt", "title", "start", "value", "colspan", "rowspan", "scope", "class", "display", "encoding"}
    attrs = {}
    for key, value in node.attrs.items():
        if key not in allowed or value is None:
            continue
        if key in {"href", "src"}:
            value = _url(value, base_url)
            if not value:
                continue
        attrs[key] = value
    if node.tag == "img" and "src" not in attrs and node.attrs.get("data-src"):
        attrs["src"] = _url(node.attrs["data-src"], base_url)
    content = "".join(_clean_node(child, base_url) for child in node.children)
    if not node.tag:
        return content
    attr_text = "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in attrs.items())
    opening = f"<{node.tag}{attr_text}>"
    return opening if node.tag in _VOID else opening + content + f"</{node.tag}>"


def clean_html(fragment: str, base_url: str = "") -> str:
    return _clean_node(_tree(fragment), base_url).strip()


def _literal_text(node: Element | str) -> str:
    """Keep code whitespace and mathematical notation nested inside literals."""
    if isinstance(node, str):
        return node
    if node.tag in _IGNORE or _icon(node):
        return ""
    if node.tag == "br":
        return "\n"
    value = "".join(_literal_text(child) for child in node.children)
    if node.tag in {"sup", "sub"} and value:
        # LeetCode uses HTML inside code spans, e.g. 10<sup>4</sup>.
        # Flattening that markup to "104" changes the problem constraints.
        marker = "^" if node.tag == "sup" else "_"
        if not re.fullmatch(r"[+-]?(?:\d+|[^\W\d_])", value):
            value = f"({value})"
        return marker + value
    return value


class _Markdown:
    def __init__(self, base_url: str, heading_offset: int):
        self.base_url = base_url
        self.heading_offset = heading_offset

    def children(self, node: Element) -> str:
        return "".join(self.render(child) for child in node.children)

    def render(self, node: Element | str) -> str:
        if isinstance(node, str):
            return re.sub(r"\s+", " ", node)
        tag = node.tag
        if (math := _math(node)) is not None:
            return math
        if tag in _IGNORE or _icon(node):
            return ""
        if tag == "pre":
            code = _literal_text(node).replace("\r\n", "\n").replace("\r", "\n")
            language = ""
            for item in [node] + node.find_all(lambda tag, attrs: tag == "code"):
                match = re.search(r"(?:^|\s)(?:language|lang)-([\w+.-]+)", item.attrs.get("class") or "")
                if match:
                    language = match.group(1)
                    break
            fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", code)), default=0))
            return f"\n\n{fence}{language}\n{code}" + ("" if code.endswith("\n") else "\n") + f"{fence}\n\n"
        if tag in {"code", "kbd", "samp"}:
            value = _literal_text(node).replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
            fence = "`" * max(1, max((len(m.group()) + 1 for m in re.finditer(r"`+", value)), default=0))
            pad = " " if value.startswith(("`", " ")) or value.endswith(("`", " ")) else ""
            return f"{fence}{pad}{value}{pad}{fence}"
        if tag in {"ol", "ul"}:
            return self.list(node)
        if tag == "table":
            return self.table(node)
        body = self.children(node)
        if re.fullmatch(r"h[1-6]", tag):
            level = max(1, min(6, int(tag[1]) + self.heading_offset))
            return "\n\n" + "#" * level + " " + body.strip() + "\n\n"
        if tag in {"p", "div", "section", "article", "header", "footer", "dl", "dt", "dd"}:
            return "\n\n" + body.strip(" \n") + "\n\n" if body.strip() else ""
        if tag == "br":
            return "\n"
        if tag == "hr":
            return "\n\n---\n\n"
        if tag in {"b", "strong", "i", "em", "del", "s"}:
            marker = {"b": "**", "strong": "**", "i": "*", "em": "*", "del": "~~", "s": "~~"}[tag]
            if not body.strip():
                return body
            return (" " if body[0].isspace() else "") + marker + body.strip() + marker + (" " if body[-1].isspace() else "")
        if tag == "a":
            url = _url(node.attrs.get("href") or "", self.base_url)
            if not url:
                return body
            target = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            return f"[{body.strip() or url}]({target})"
        if tag == "img":
            url = _url(node.attrs.get("src") or node.attrs.get("data-src") or "", self.base_url)
            alt = (node.attrs.get("alt") or node.attrs.get("title") or "이미지").replace("[", "\\[").replace("]", "\\]")
            target = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            return f"![{alt}]({target})" if url else f"[{alt}]"
        if tag == "blockquote":
            return "\n\n" + "\n".join("> " + line for line in body.strip().splitlines()) + "\n\n"
        if tag in {"sub", "sup", "math"}:
            return _clean_node(node, self.base_url)
        return body

    def list(self, node: Element) -> str:
        try:
            number = int(node.attrs.get("start") or "1")
        except ValueError:
            number = 1
        items = []
        for child in node.children:
            if not isinstance(child, Element) or child.tag != "li":
                continue
            try:
                number = int(child.attrs.get("value") or str(number))
            except ValueError:
                pass
            prefix = f"{number}. " if node.tag == "ol" else "- "
            content = self.children(child).strip(" \n")
            lines = content.splitlines() or [""]
            items.append(prefix + lines[0] + "".join("\n" + " " * len(prefix) + line for line in lines[1:]))
            number += 1
        return "\n\n" + "\n".join(items) + "\n\n"

    def table(self, node: Element) -> str:
        # GFM cannot express spans or nested tables: keep semantic HTML there.
        if node.find_all(lambda tag, attrs: tag == "table" or any(attrs.get(key) not in {None, "1"} for key in ("colspan", "rowspan"))):
            return "\n\n" + _clean_node(node, self.base_url) + "\n\n"
        rows = []
        for row in node.find_all(lambda tag, attrs: tag == "tr"):
            cells = [self.children(cell).strip().replace("|", "\\|").replace("\n", "<br>")
                     for cell in row.children if isinstance(cell, Element) and cell.tag in {"th", "td"}]
            if cells:
                rows.append(cells)
        if not rows:
            return self.children(node)
        width = max(map(len, rows))
        rows = [row + [""] * (width - len(row)) for row in rows]
        rows.insert(1, ["---"] * width)
        caption = "\n\n".join(self.children(x).strip() for x in node.children if isinstance(x, Element) and x.tag == "caption")
        return "\n\n" + (caption + "\n\n" if caption else "") + "\n".join("| " + " | ".join(row) + " |" for row in rows) + "\n\n"


def html_to_markdown(fragment: str, base_url: str = "", heading_offset: int = 0) -> str:
    # Normalize only blank space introduced between blocks. Never strip code
    # lines or collapse blank lines inside a fenced code block.
    value = _Markdown(base_url, heading_offset).render(_tree(fragment))
    lines = value.splitlines()
    result = []
    fence = None
    for line in lines:
        match = re.match(r"^\s*(`{3,})([^`]*)$", line)
        if match and (fence is None or (len(match[1]) >= fence and not match[2].strip())):
            fence = len(match[1]) if fence is None else None
            result.append(line)
        elif fence is not None:
            result.append(line)
        elif line.strip():
            result.append(line.rstrip())
        elif result and result[-1] != "":
            result.append("")
    return "\n".join(result).strip()
