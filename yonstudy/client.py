"""SSO 로그인과 요청 속도 제어를 담당하는 LearnUs HTTP 클라이언트."""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

LEARNUS = "https://ys.learnus.org"
INFRA = "https://infra.yonsei.ac.kr"
APP_ID = "ednetYonsei"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 15

# SSO 폼이 id와 name을 혼용하므로 둘 다 키로 쓴다.
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""(\w[\w-]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")


def parse_input_tags(html: str) -> dict[str, str]:
    """<input> 태그들의 id/name → value 매핑을 추출한다."""
    result: dict[str, str] = {}
    for tag in _INPUT_RE.findall(html):
        attrs: dict[str, str] = {}
        for m in _ATTR_RE.finditer(tag):
            attrs[m.group(1).lower()] = m.group(3) or m.group(4) or m.group(5) or ""
        value = attrs.get("value", "")
        for key in ("id", "name"):
            if attrs.get(key):
                result.setdefault(attrs[key], value)
    return result


def rsa_pkcs1v15_encrypt_hex(modulus_hex: str, exponent_hex: str, message: str) -> str:
    """LearnUs 로그인 폼과 같은 PKCS#1 v1.5 RSA 암호문을 만든다."""
    n = int(modulus_hex, 16)
    e = int(exponent_hex, 16)
    k = (n.bit_length() + 7) // 8

    data = message.encode("utf-8")
    if len(data) > k - 11:
        raise ValueError(f"메시지가 RSA 키({k}바이트)에 비해 너무 깁니다: {len(data)}바이트")

    # EM = 0x00 || 0x02 || PS(0이 아닌 난수, >=8바이트) || 0x00 || M
    ps_len = k - len(data) - 3
    ps = bytearray()
    while len(ps) < ps_len:
        b = secrets.token_bytes(ps_len - len(ps))
        ps.extend(x for x in b if x != 0)
    em = b"\x00\x02" + bytes(ps[:ps_len]) + b"\x00" + data

    cipher = pow(int.from_bytes(em, "big"), e, n)
    return cipher.to_bytes(k, "big").hex().upper()


def _extract_sso_error(html: str) -> str:
    """통합인증 실패 페이지에서 사람이 읽을 수 있는 사유를 뽑아낸다."""
    for pattern in (r"alert\(\s*['\"]([^'\"]{4,200})['\"]", r">\s*([^<>]{4,80}?습니다\.)\s*<"):
        m = re.search(pattern, html)
        if m:
            return m.group(1).strip().replace("\\n", " ")
    return ""


class LearnUsClient:
    """쿠키 세션을 들고 LearnUs에 요청한다. 요청 간격은 자동 스로틀된다."""

    def __init__(
        self,
        cookie_path: str,
        min_interval: float = 0.4,
        max_interval: float = 8.0,
        per_minute: int = 40,
    ):
        self.cookie_path = cookie_path
        self.base_interval = min_interval
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.base_per_minute = per_minute
        self.per_minute = per_minute
        self._last_request = 0.0
        self._penalty = 0
        self._ok_streak = 0
        self._window: list[float] = []
        self.jar = http.cookiejar.MozillaCookieJar(cookie_path)
        if os.path.exists(cookie_path):
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def _throttle(self) -> None:
        self._consume_token()
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    # 요청이 몰리면 HTTP 400이 반복되어 분당 횟수도 제한한다.

    def _consume_token(self) -> None:
        """최근 60초 요청 수가 상한을 넘으면 창이 빌 때까지 기다린다."""
        now = time.monotonic()
        self._window = [t for t in self._window if now - t < 60.0]
        if len(self._window) >= self.per_minute:
            sleep_for = 60.0 - (now - self._window[0]) + 0.1
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            self._window = [t for t in self._window if now - t < 60.0]
        self._window.append(now)

    def _penalize(self) -> None:
        self._penalty += 1
        self.min_interval = min(self.max_interval, max(self.min_interval * 2, 1.0))
        # 간격만 늘리지 말고 1분 당 상한도 함께 낮춘다.
        self.per_minute = max(10, int(self.per_minute * 0.6))

    def _reward(self) -> None:
        self._ok_streak += 1
        if self.min_interval > self.base_interval:
            self._penalty = max(0, self._penalty - 1)
            if self._penalty == 0:
                self.min_interval = max(self.base_interval, self.min_interval * 0.7)
        # 오래 무사하면 상한을 조금씩 회복한다.
        if self._ok_streak >= 120 and self.per_minute < self.base_per_minute:
            self.per_minute = min(self.base_per_minute, self.per_minute + 5)
            self._ok_streak = 0

    def cool_down(self, seconds: float) -> None:
        """차단이 확실할 때 길게 쉬고 창을 비운다."""
        time.sleep(seconds)
        self._window.clear()
        self._last_request = 0.0

    # ubboard_read에 읽은 글 목록이 쌓이면 Cookie 헤더가 서버 상한을 넘는다.
    # 세션에 필요한 쿠키는 남기고 이 추적용 쿠키는 매 요청 후 지운다.
    ESSENTIAL_COOKIES = {
        "MoodleSession", "JSESSIONID", "LEARNUS_HAVE_SSOLOGINED",
        "passni.keepLogin", "MOODLEID1_",
    }
    DISPOSABLE_COOKIES = {"ubboard_read"}
    MAX_COOKIE_HEADER = 4096

    def _prune_cookies(self) -> None:
        for cookie in list(self.jar):
            if cookie.name in self.DISPOSABLE_COOKIES:
                self.jar.clear(cookie.domain, cookie.path, cookie.name)

        header = sum(len(c.name) + len(c.value) + 3 for c in self.jar)
        if header <= self.MAX_COOKIE_HEADER:
            return
        # 그래도 크면 필수가 아닌 것 중 큰 순서로 버린다.
        droppable = sorted(
            (c for c in self.jar if c.name not in self.ESSENTIAL_COOKIES),
            key=lambda c: -len(c.value),
        )
        for cookie in droppable:
            self.jar.clear(cookie.domain, cookie.path, cookie.name)
            header -= len(cookie.name) + len(cookie.value) + 3
            if header <= self.MAX_COOKIE_HEADER:
                break

    @property
    def cookie_header_size(self) -> int:
        return sum(len(c.name) + len(c.value) + 3 for c in self.jar)

    def _guard(self, fn):
        """요청 한 건을 감싸 레이트 리밋과 쿠키 비대화에 대응한다."""
        try:
            result = fn()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 429, 503):
                self._penalize()
                self._prune_cookies()  # 400의 주원인이 쿠키 크기다. 먼저 줄이고 본다.
            raise
        self._reward()
        self._prune_cookies()
        return result

    def fetch(self, url: str, referer: str = LEARNUS) -> tuple[str, str]:
        """(본문, 최종 URL). 로그인 리다이렉트를 다른 실패와 구분하기 위해 필요하다."""
        self._throttle()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Referer": referer,
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
        )
        def go():
            with self.opener.open(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "ignore"), resp.geturl()

        return self._guard(go)

    def get_bytes(self, url: str, referer: str = LEARNUS) -> tuple[bytes, str]:
        """(본문 바이트, 최종 URL). 첨부파일 다운로드용."""
        self._throttle()
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Referer": referer}
        )
        def go():
            with self.opener.open(req, timeout=120) as resp:
                return resp.read(), resp.geturl()

        return self._guard(go)

    def request(
        self,
        url: str,
        data: dict | None = None,
        referer: str = LEARNUS,
        allow_redirect: bool = True,
    ) -> str:
        self._throttle()
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": UA,
                "Referer": referer,
                "Accept-Language": "ko-KR,ko;q=0.9",
                **(
                    {"Content-Type": "application/x-www-form-urlencoded"}
                    if body
                    else {}
                ),
            },
        )
        opener = self.opener
        if not allow_redirect:
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None

            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect
            )

        def go():
            with opener.open(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "ignore")

        # 오류 페이지를 정상 HTML로 넘기면 호출자가 이를 빈 게시판이나 미제출
        # 상태로 해석해 기존 데이터를 덮어쓸 수 있다. fetch/get_bytes와 마찬가지로
        # HTTP 상태 오류는 호출자에게 전달한다.
        return self._guard(go)

    # SSO 로그인

    def login(self, username: str, password: str) -> None:
        # LearnUs에서 SSO 시작 토큰을 받는다.
        step1 = parse_input_tags(
            self.request(f"{LEARNUS}/passni/sso/spLogin2.php", referer=LEARNUS)
        )
        if "S1" not in step1:
            raise RuntimeError("1단계 실패: S1을 찾지 못했습니다 (SSO 진입 실패)")

        # 통합인증 서버가 내려 준 challenge와 RSA 공개키를 쓴다.
        html2 = self.request(
            f"{INFRA}/sso/PmSSOService",
            data={
                "app_id": APP_ID,
                "retUrl": LEARNUS,
                "failUrl": LEARNUS,
                "baseUrl": LEARNUS,
                "S1": step1["S1"],
                "refererUrl": LEARNUS,
            },
            referer=LEARNUS,
        )
        challenge = re.search(r"var\s+ssoChallenge\s*=\s*'([^']+)'", html2)
        key = re.search(r"rsa\.setPublic\(\s*'([^']+)'\s*,\s*'([^']+)'", html2, re.I)
        if not challenge or not key:
            raise RuntimeError("2단계 실패: ssoChallenge / RSA 공개키를 찾지 못했습니다")

        # 자격증명을 암호화해 인증 결과를 받는다.
        e2 = rsa_pkcs1v15_encrypt_hex(
            key.group(1),
            key.group(2),
            json.dumps(
                {
                    "userid": username,
                    "userpw": password,
                    "ssoChallenge": challenge.group(1),
                },
                separators=(",", ":"),
            ),
        )
        html3 = self.request(
            f"{INFRA}/sso/PmSSOAuthService",
            data={
                "app_id": APP_ID,
                "retUrl": LEARNUS,
                "failUrl": LEARNUS,
                "baseUrl": LEARNUS,
                "loginType": "invokeID",
                "E2": e2,
                "refererUrl": LEARNUS,
            },
            referer=LEARNUS,
        )
        step3 = parse_input_tags(html3)
        # 인증 실패 시에도 서버는 E3/E4를 빈 값으로 돌려주므로 S2/CLTID로 판정한다.
        if not step3.get("S2") or not step3.get("CLTID"):
            reason = _extract_sso_error(html3)
            if step3.get("captcha_yn", "").upper() == "Y":
                reason = (reason + " / " if reason else "") + (
                    "캡차가 활성화되었습니다 — 브라우저에서 직접 한 번 로그인해 해제하세요"
                )
            raise RuntimeError(
                "3단계 실패: 통합인증 거부"
                + (f" ({reason})" if reason else "")
                + ". 반복 시도하면 계정이 잠기니 자격증명을 먼저 확인하세요."
            )

        # 인증 결과를 LearnUs에 넘겨 MoodleSession을 만든다.
        self.request(
            f"{LEARNUS}/passni/sso/spLoginData.php",
            data={
                "app_id": APP_ID,
                "retUrl": LEARNUS,
                "failUrl": LEARNUS,
                "baseUrl": LEARNUS,
                "E3": step3["E3"],
                "E4": step3["E4"],
                "S2": step3["S2"],
                "CLTID": step3["CLTID"],
                "refererUrl": LEARNUS,
            },
            referer=LEARNUS,
        )
        self.request(f"{LEARNUS}/passni/spLoginProcess.php", referer=LEARNUS)

    def session_info(self) -> tuple[bool, str | None]:
        """(로그인 여부, sesskey)를 반환한다.

        HTTP 오류는 세션 만료로 간주하지 않고 그대로 호출자에게 올린다.
        """
        html, _ = self.fetch(LEARNUS)
        alive = "/login/logout.php" in html
        m = re.search(r'sesskey":"([^"]+)', html)
        return alive, (m.group(1) if m else None)

    def save(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.cookie_path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        self.jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(self.cookie_path, 0o600)
