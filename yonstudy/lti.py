"""읽기 전용 LTI 실행을 따라가 외부 과제 명세를 정규화한다."""

from __future__ import annotations

import html as html_mod
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .client import LEARNUS


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

    def handle_starttag(self, _tag, attrs):
        values = dict(attrs)
        if values.get("data-react-class") == "OnlineAssignmentSubmitter":
            self.props = values.get("data-react-props")


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
    return props if isinstance(props, dict) else None


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


def _launch_page(client, cmid: int, max_steps: int = 10) -> tuple[str, str]:
    """OIDC 로그인 폼만 자동 제출하고 최종 과제 페이지에서 멈춘다."""
    current = f"{LEARNUS}/mod/lti/launch.php?id={cmid}&triggerview=0"
    page, final = client.fetch(current, referer=f"{LEARNUS}/mod/lti/view.php?id={cmid}")
    current = final

    for _ in range(max_steps):
        if not _allowed(current):
            raise LtiArchiveError("허용하지 않은 호스트로 향하는 LTI 이동을 차단했습니다")
        if _gradescope_props(page) is not None:
            return page, current

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


def _plain_text(value: str) -> str:
    if "<" not in value or ">" not in value:
        return html_mod.unescape(value).strip()
    # Gradescope의 안내 텍스트에는 간단한 HTML이 들어올 수 있다. 스크립트와
    # 스타일을 버리고 블록 경계만 줄바꿈으로 보존한다.
    value = re.sub(r"<(script|style)\b.*?</\1>", "", value, flags=re.I | re.S)
    value = re.sub(r"<br\s*/?>|</(?:p|div|li|h[1-6])>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    lines = [re.sub(r"[ \t]+", " ", html_mod.unescape(line)).strip()
             for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _question_contents(question: dict) -> list[dict]:
    for key in ("content", "contents"):
        value = question.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    return []


def _points(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _render_question_markdown(question: dict, number: int) -> list[str]:
    title = _plain_text(str(question.get("title") or f"문항 {number}"))
    points = _points(question.get("weight") or question.get("points"))
    heading = f"## 문항 {number}. {title}"
    if points:
        heading += f" — {points}점"
    lines = [heading, ""]
    for item in _question_contents(question):
        kind = str(item.get("type") or "").lower()
        body = _plain_text(_value(item))
        if kind in {"file_upload_input", "file_upload", "file"}:
            lines.extend(["- 제출 항목: 파일 업로드", ""])
        elif kind in {"free_response_input", "free_response", "text_input"}:
            lines.extend(["- 제출 항목: 서술형 응답", ""])
        elif body:
            lines.extend([body, ""])
    return lines


def _link_html(value: str) -> str:
    escaped = html_mod.escape(value)
    return re.sub(
        r"(https?://[^\s<]+)",
        lambda match: f'<a href="{html_mod.escape(html_mod.unescape(match.group(1)), quote=True)}">{match.group(1)}</a>',
        escaped,
    ).replace("\n", "<br>\n")


def _render_question_html(question: dict, number: int) -> list[str]:
    title = _plain_text(str(question.get("title") or f"문항 {number}"))
    points = _points(question.get("weight") or question.get("points"))
    suffix = f" — {html_mod.escape(points)}점" if points else ""
    lines = [f"<h2>문항 {number}. {html_mod.escape(title)}{suffix}</h2>"]
    for item in _question_contents(question):
        kind = str(item.get("type") or "").lower()
        body = _plain_text(_value(item))
        if kind in {"file_upload_input", "file_upload", "file"}:
            lines.append("<p><strong>제출 항목:</strong> 파일 업로드</p>")
        elif kind in {"free_response_input", "free_response", "text_input"}:
            lines.append("<p><strong>제출 항목:</strong> 서술형 응답</p>")
        elif body:
            lines.append(f"<p>{_link_html(body)}</p>")
    return lines


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
    questions = [question for question in outline if isinstance(question, dict)]
    title = _plain_text(str(props.get("title") or "Gradescope 과제"))

    markdown: list[str] = []
    html: list[str] = []
    total = 0.0
    has_total = False
    for number, question in enumerate(questions, 1):
        markdown.extend(_render_question_markdown(question, number))
        html.extend(_render_question_html(question, number))
        try:
            total += float(question.get("weight") or question.get("points"))
            has_total = True
        except (TypeError, ValueError):
            pass

    total_points = _points(total) if has_total else None
    return LtiAssignment(
        provider="Gradescope",
        title=title,
        source_url=source_url,
        instructions="\n".join(markdown).strip(),
        instructions_html="\n".join(html),
        question_count=len(questions),
        total_points=total_points,
    )


def fetch_lti_assignment(client, cmid: int) -> LtiAssignment:
    page, source_url = _launch_page(client, cmid)
    return parse_gradescope_assignment(page, source_url)


__all__ = [
    "LtiArchiveError",
    "LtiAssignment",
    "fetch_lti_assignment",
    "parse_gradescope_assignment",
]
