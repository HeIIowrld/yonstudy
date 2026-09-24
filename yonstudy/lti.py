"""읽기 전용 LTI 실행을 따라가 외부 과제 명세를 정규화한다."""

from __future__ import annotations

import html as html_mod
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .client import LEARNUS
from .html_content import clean_html, html_to_markdown


_ALLOWED_HOSTS = {
    "ys.learnus.org",
    "lti.int.turnitin.com",
    "lti.int.turnitinuk.com",
    "gradescope.com",
    "www.gradescope.com",
}
_REDIRECT_URL = re.compile(
    r"(?:let|var|const)\s+redirect_url\s*=\s*new\s+URL\s*\(\s*"
    r"(?P<value>\"(?:\\.|[^\"\\])*\")\s*\)",
    re.I,
)


class LtiArchiveError(RuntimeError):
    """안전하게 지원할 수 없는 LTI 실행 또는 과제 형식."""


@dataclass
class LtiAssignment:
    provider: str
    title: str
    source_url: str
    instructions: str
    instructions_html: str
    question_count: int
    total_points: str | None


@dataclass
class _Form:
    action: str
    method: str
    fields: dict[str, str]
    interactive: bool = False


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.current: _Form | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag.lower() == "form":
            self.current = _Form(
                action=values.get("action") or "",
                method=(values.get("method") or "get").lower(),
                fields={},
            )
        elif tag.lower() == "input" and self.current is not None:
            name = values.get("name")
            if name:
                self.current.fields[name] = values.get("value") or ""
                if (values.get("type") or "text").lower() not in {"hidden", "submit"}:
                    self.current.interactive = True
        elif tag.lower() in {"textarea", "select"} and self.current is not None:
            self.current.interactive = True

    def handle_endtag(self, tag):
        if tag.lower() == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


class _GradescopePropsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.props: str | None = None
        self.component: str | None = None

    def handle_starttag(self, _tag, attrs):
        values = dict(attrs)
        if values.get("data-react-class") in {"OnlineAssignmentSubmitter", "AssignmentSubmissionViewer"}:
            self.props = values.get("data-react-props")
            self.component = values.get("data-react-class")


class _AssignmentLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.label: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data):
        if self.href:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((self.href, "".join(self.label)))
            self.href = None


def _allowed(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in _ALLOWED_HOSTS


def _forms(page: str) -> list[_Form]:
    parser = _FormParser()
    parser.feed(page)
    parser.close()
    return parser.forms


def _auth_form(page: str, current_url: str) -> _Form | None:
    """페이지의 일반 제출 폼이 아니라 LTI/OIDC 전달 폼 하나만 고른다."""
    markers = {"id_token", "login_hint", "lti_message_hint", "state"}
    candidates: list[tuple[int, _Form]] = []
    for form in _forms(page):
        target = urljoin(current_url, form.action)
        keys = set(form.fields)
        if form.method == "post" and markers & keys and not _allowed(target):
            raise LtiArchiveError("허용하지 않은 호스트로 향하는 LTI 폼을 차단했습니다")
        if form.method != "post" or not _allowed(target):
            continue
        if form.interactive or not markers & keys or "debug" in keys:
            continue
        path = urlsplit(target).path.lower()
        score = sum(key in keys for key in markers)
        score += sum(word in path for word in ("oidc", "launch", "login", "callback", "auth"))
        candidates.append((score, form))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


def _gradescope_props(page: str) -> dict | None:
    parser = _GradescopePropsParser()
    parser.feed(page)
    parser.close()
    if not parser.props:
        return None
    try:
        props = json.loads(parser.props)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LtiArchiveError("Gradescope 과제 데이터를 해석하지 못했습니다") from exc
    if not isinstance(props, dict):
        return None
    if parser.component == "AssignmentSubmissionViewer":
        assignment = props.get("assignment")
        if not isinstance(assignment, dict) or assignment.get("submission_format") != "online":
            return None
        questions = props.get("questions")
        outline = props.get("outline")
        if not isinstance(questions, list) or not isinstance(outline, list):
            return None
        by_id = {str(q["id"]): q for q in questions if isinstance(q, dict) and "id" in q}

        def public_outline(items):
            result = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                question = by_id.get(str(item.get("id")), {})
                public = {key: question[key] for key in ("title", "weight", "content") if key in question}
                public.update({key: item[key] for key in ("id", "index", "parent_id") if key in item})
                if isinstance(item.get("children"), list):
                    public["children"] = public_outline(item["children"])
                result.append(public)
            return result

        # 제출 후 화면에는 답안과 성적도 있다. 문항 ID로 공개 문제 정보만 결합한다.
        return {"title": assignment.get("title"), "outline": public_outline(outline)}
    return props


def _existing_assignment_link(page: str, current_url: str, title: str) -> str | None:
    current = urlsplit(current_url)
    if current.hostname not in {"gradescope.com", "www.gradescope.com"}:
        return None
    course = re.fullmatch(r"/courses/(\d+)/?", current.path)
    if not course or not title:
        return None
    parser = _AssignmentLinkParser()
    parser.feed(page)
    parser.close()
    normalize = lambda text: " ".join(text.split()).casefold()
    candidates = set()
    for href, label in parser.links:
        target = urljoin(current_url, href)
        parsed = urlsplit(target)
        if (normalize(label) == normalize(title) and _allowed(target)
                and parsed.hostname == current.hostname
                and re.fullmatch(rf"/courses/{course.group(1)}/assignments/\d+/submissions/\d+/?", parsed.path)
                and not parsed.query):
            candidates.add(target)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _js_redirect(page: str, current_url: str) -> str | None:
    match = _REDIRECT_URL.search(page)
    if not match:
        return None
    try:
        target = urljoin(current_url, json.loads(match.group("value")))
    except (TypeError, json.JSONDecodeError) as exc:
        raise LtiArchiveError("LTI 중간 이동 URL을 해석하지 못했습니다") from exc
    if not _allowed(target):
        raise LtiArchiveError("허용하지 않은 호스트로 향하는 LTI 이동을 차단했습니다")
    return target


def _launch_page(client, cmid: int, max_steps: int = 10, title: str = "") -> tuple[str, str]:
    """OIDC 로그인 폼만 자동 제출하고 최종 과제 페이지에서 멈춘다."""
    current = f"{LEARNUS}/mod/lti/launch.php?id={cmid}&triggerview=0"
    page, final = client.fetch(current, referer=f"{LEARNUS}/mod/lti/view.php?id={cmid}")
    current = final
    visited_assignments: set[str] = set()

    for _ in range(max_steps):
        if not _allowed(current):
            raise LtiArchiveError("허용하지 않은 호스트로 향하는 LTI 이동을 차단했습니다")
        if _gradescope_props(page) is not None:
            return page, current

        existing = _existing_assignment_link(page, current, title)
        if existing:
            if existing in visited_assignments:
                raise LtiArchiveError("Gradescope에서 이 과제의 공개 명세를 열람할 수 없습니다")
            visited_assignments.add(existing)
            page, current = client.fetch(existing, referer=current)
            continue

        redirect = _js_redirect(page, current)
        if redirect:
            page, current = client.fetch(redirect, referer=current)
            continue

        form = _auth_form(page, current)
        if form is None:
            raise LtiArchiveError("지원하는 외부 과제 페이지를 찾지 못했습니다")
        target = urljoin(current, form.action)
        if not _allowed(target):
            raise LtiArchiveError("허용하지 않은 호스트로 향하는 LTI 폼을 차단했습니다")
        page, current = client.submit(target, form.fields, referer=current)

    raise LtiArchiveError("LTI 실행 단계가 예상보다 길어 중단했습니다")


def _value(item: dict) -> str:
    for key in ("text", "value", "content", "body", "description"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


_HTML_TAG = re.compile(
    r"</?(?:p|div|span|br|a|img|strong|b|em|i|u|s|pre|code|h[1-6]|"
    r"ul|ol|li|table|thead|tbody|tr|td|th|blockquote|sup|sub|hr|script|style)"
    r"(?:\s[^<>]*|/?)>", re.I,
)
_MARKDOWN_CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.S)


def _is_html(value: str) -> bool:
    # Markdown 코드 안의 <div>, C++ template, 비교 연산자는 HTML이 아니다.
    without_code = _MARKDOWN_CODE.sub("", value)
    return bool(_HTML_TAG.search(without_code))


def _plain_text(value: str) -> str:
    if _is_html(value):
        value = re.sub(r"<(script|style)\b.*?</\1>", "", value, flags=re.I | re.S)
        value = _HTML_TAG.sub("", value)
    return html_mod.unescape(value).strip()


def _question_contents(question: dict) -> list[dict]:
    for key in ("content", "contents"):
        value = question.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        if isinstance(value, str) and value.strip():
            return [{"type": "text", "value": value}]
    return []


_INPUT_LABELS = {
    "file_upload_input": "파일 업로드", "file_upload": "파일 업로드", "file": "파일 업로드",
    "free_response_input": "서술형 응답", "free_response": "서술형 응답",
    "text_input": "서술형 응답", "multiple_choice_input": "객관식 응답",
    "checkbox_input": "복수 선택 응답",
}


def _public_body(item: dict) -> str:
    kind = str(item.get("type") or "").lower()
    if kind in _INPUT_LABELS or kind.endswith("_input"):
        # 입력 블록의 value는 학생 답안일 수 있다. 공개 안내 필드만 읽는다.
        for key in ("prompt", "description", "label", "text"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if kind in {"", "text", "html", "markdown", "description", "instructions"}:
        return _value(item)
    return ""


def _body_markdown(value: str, source_url: str, heading_offset: int = 2) -> str:
    if _is_html(value):
        code: list[str] = []
        marker = "YONSTUDYCODE"
        while marker in value:
            marker += "X"

        def protect(match):
            code.append(match.group(0))
            return f"{marker}{len(code) - 1}END"

        protected = _MARKDOWN_CODE.sub(protect, value)
        rendered = html_to_markdown(protected, base_url=source_url, heading_offset=heading_offset)
        for number, original in enumerate(code):
            rendered = rendered.replace(f"{marker}{number}END", original)
        return rendered
    return value.strip()


def _question_points(question: dict):
    value = question.get("weight")
    return question.get("points") if value is None or value == "" else value


def _points(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _render_question_markdown(question: dict, number: str, source_url: str) -> list[str]:
    title = _plain_text(str(question.get("title") or f"문항 {number}"))
    points = _points(_question_points(question))
    level = min(2 + number.count("."), 6)
    heading = f"{'#' * level} 문항 {number}. {title}"
    if points:
        heading += f" — {points}점"
    lines = [heading, ""]
    for item in _question_contents(question):
        kind = str(item.get("type") or "").lower()
        body = _body_markdown(_public_body(item), source_url, heading_offset=level)
        if body:
            lines.extend([body, ""])
        if kind in _INPUT_LABELS:
            lines.extend([f"- 제출 항목: {_INPUT_LABELS[kind]}", ""])
    return lines


def _link_html(value: str, source_url: str = "") -> str:
    # 원문 Markdown 링크와 코드 범위를 먼저 읽어 URL이나 <>를 중복 해석하지 않는다.
    token = re.compile(r"`([^`\n]+)`|(!?)\[([^\]\n]*)\]\(([^\s]+)\)|(https?://[^\s<>]+)")
    parts: list[str] = []
    end = 0
    for match in token.finditer(value):
        parts.append(html_mod.escape(value[end:match.start()]))
        if match.group(1) is not None:
            parts.append(f"<code>{html_mod.escape(match.group(1))}</code>")
        else:
            address = match.group(4) or match.group(5)
            trailing = ""
            if match.group(5):
                address = address.rstrip(".,;:!?")
                while address.endswith(")") and address.count(")") > address.count("("):
                    address = address[:-1]
                trailing = match.group(5)[len(address):]
            address = html_mod.escape(urljoin(source_url, address), quote=True)
            label = html_mod.escape(match.group(3) if match.group(4) else html_mod.unescape(address))
            if match.group(2) == "!":
                parts.append(f'<img src="{address}" alt="{label}">')
            else:
                parts.append(f'<a href="{address}">{label}</a>')
            parts.append(html_mod.escape(trailing))
        end = match.end()
    parts.append(html_mod.escape(value[end:]))
    return clean_html("".join(parts).replace("\n", "<br>\n"), base_url=source_url)


def _body_html(value: str, source_url: str) -> str:
    def fragment_html(fragment):
        if _is_html(fragment):
            protected = re.sub(r"`([^`\n]+)`", lambda match: f"<code>{html_mod.escape(match.group(1))}</code>", fragment)
            return clean_html(protected, base_url=source_url)
        return f"<p>{_link_html(fragment, source_url)}</p>"

    parts: list[str] = []
    position = 0
    for match in re.finditer(r"(?m)^(`{3,}|~{3,})[^\n]*\n(.*?)^\1\s*$", value, flags=re.S):
        if value[position:match.start()].strip():
            parts.append(fragment_html(value[position:match.start()].strip()))
        parts.append(f"<pre><code>{html_mod.escape(match.group(2).rstrip(chr(10)))}</code></pre>")
        position = match.end()
    if value[position:].strip():
        parts.append(fragment_html(value[position:].strip()))
    return "\n".join(parts)


def _render_question_html(question: dict, number: str, source_url: str) -> list[str]:
    title = _plain_text(str(question.get("title") or f"문항 {number}"))
    points = _points(_question_points(question))
    suffix = f" — {html_mod.escape(points)}점" if points else ""
    level = min(2 + number.count("."), 6)
    lines = [f"<h{level}>문항 {number}. {html_mod.escape(title)}{suffix}</h{level}>"]
    for item in _question_contents(question):
        kind = str(item.get("type") or "").lower()
        body = _public_body(item)
        if body:
            lines.append(_body_html(body, source_url))
        if kind in _INPUT_LABELS:
            lines.append(f"<p><strong>제출 항목:</strong> {_INPUT_LABELS[kind]}</p>")
    return lines


def _children(question: dict) -> list[dict]:
    children = question.get("children")
    return [child for child in children if isinstance(child, dict)] if isinstance(children, list) else []


def _outline_tree(questions: list[dict]) -> list[dict]:
    """Gradescope의 평면 parent_id 구조를 원래 순서의 하위 문항으로 묶는다."""
    copied = [dict(question) for question in questions]
    by_id = {str(question["id"]): question for question in copied if "id" in question}
    roots = []
    for question in copied:
        parent = by_id.get(str(question.get("parent_id")))
        ancestor = parent
        seen = {id(question)}
        while ancestor is not None and id(ancestor) not in seen:
            seen.add(id(ancestor))
            ancestor = by_id.get(str(ancestor.get("parent_id")))
        if parent is not None and ancestor is None:
            parent["children"] = [*_children(parent), question]
        else:
            roots.append(question)
    return roots


def _walk_questions(questions: list[dict], prefix: str = ""):
    for number, question in enumerate(questions, 1):
        label = f"{prefix}{number}"
        yield label, question
        yield from _walk_questions(_children(question), prefix=f"{label}.")


def _total_points(questions: list[dict]) -> float | None:
    values: list[float] = []
    for question in questions:
        try:
            values.append(float(_question_points(question)))
        except (TypeError, ValueError):
            subtotal = _total_points(_children(question))
            if subtotal is not None:
                values.append(subtotal)
    return sum(values) if values else None


def parse_gradescope_assignment(page: str, source_url: str) -> LtiAssignment:
    """React 속성에서 과제 공개 명세만 추출한다.

    답안, 수강생 명단, 사용자 ID, CSRF 토큰과 제출 URL은 의도적으로 저장하지
    않는다. 저장되는 데이터는 제목·문항·배점·안내·입력 종류뿐이다.
    """
    props = _gradescope_props(page)
    if props is None:
        raise LtiArchiveError("Gradescope 온라인 과제 명세를 찾지 못했습니다")
    outline = props.get("outline")
    if not isinstance(outline, list):
        raise LtiArchiveError("Gradescope 문항 목록을 찾지 못했습니다")
    questions = _outline_tree([question for question in outline if isinstance(question, dict)])
    title = _plain_text(str(props.get("title") or "Gradescope 과제"))

    markdown: list[str] = []
    html: list[str] = []
    instructions = props.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        markdown.extend([_body_markdown(instructions, source_url), ""])
        html.append(_body_html(instructions, source_url))
    numbered_questions = list(_walk_questions(questions))
    for number, question in numbered_questions:
        markdown.extend(_render_question_markdown(question, number, source_url))
        html.extend(_render_question_html(question, number, source_url))

    total_points = _points(_total_points(questions))
    return LtiAssignment(
        provider="Gradescope",
        title=title,
        source_url=source_url,
        instructions="\n".join(markdown).strip(),
        instructions_html="\n".join(html),
        question_count=len(numbered_questions),
        total_points=total_points,
    )


def fetch_lti_assignment(client, cmid: int, title: str = "") -> LtiAssignment:
    page, source_url = _launch_page(client, cmid, title=title)
    return parse_gradescope_assignment(page, source_url)


__all__ = [
    "LtiArchiveError",
    "LtiAssignment",
    "fetch_lti_assignment",
    "parse_gradescope_assignment",
]
