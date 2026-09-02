"""LearnUs 백그라운드 작업용 세션 자동 복구.

자격증명은 코드/DB가 아니라 systemd의 root 전용 EnvironmentFile에서만 읽는다.
잘못된 비밀번호나 캡차로 계정이 잠기지 않도록 실패 시 지수형 백오프를 적용한다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


BACKOFF_SECONDS = (15 * 60, 60 * 60, 6 * 60 * 60, 24 * 60 * 60)


class AutoLoginUnavailable(RuntimeError):
    """자격증명 누락, 백오프 또는 SSO 거부로 자동 로그인이 불가능함."""


@dataclass(frozen=True)
class AuthStatus:
    sesskey: str | None
    relogged: bool


def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_failure(path: Path, *, attempts: int, error: Exception, now: datetime) -> None:
    delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "failed_attempts": attempts,
                "last_failed_at": now.isoformat(),
                "next_retry_at": (now + timedelta(seconds=delay)).isoformat(),
                "error": str(error)[:500],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def ensure_session(
    client,
    *,
    state_path: str | Path,
    username: str | None = None,
    password: str | None = None,
    now: datetime | None = None,
) -> AuthStatus:
    """활성 세션을 보장하고, 만료됐으면 환경변수 자격증명으로 한 번 복구한다."""
    alive, sesskey = client.session_info()
    state_path = Path(state_path)
    if alive:
        client.save()
        state_path.unlink(missing_ok=True)
        return AuthStatus(sesskey=sesskey, relogged=False)

    username = username or os.environ.get("LEARNUS_ID")
    password = password or os.environ.get("LEARNUS_PW")
    if not username or not password:
        raise AutoLoginUnavailable(
            "LearnUs 세션 만료 및 자동 로그인 자격증명 미설정: "
            "/etc/yonstudy/yonstudy.env에 LEARNUS_ID/LEARNUS_PW를 설정하세요"
        )

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    state = _read_state(state_path)
    next_retry_text = state.get("next_retry_at")
    if next_retry_text:
        try:
            next_retry = datetime.fromisoformat(next_retry_text)
            if next_retry.tzinfo is None:
                next_retry = next_retry.replace(tzinfo=timezone.utc)
            if now < next_retry:
                raise AutoLoginUnavailable(
                    f"이전 자동 로그인 실패로 {next_retry.isoformat()}까지 재시도 보류"
                )
        except ValueError:
            pass

    attempts = int(state.get("failed_attempts") or 0) + 1
    try:
        client.login(username, password)
        alive, sesskey = client.session_info()
        if not alive:
            raise RuntimeError("SSO 절차 후 LearnUs 세션이 활성화되지 않음")
        client.save()
        state_path.unlink(missing_ok=True)
        return AuthStatus(sesskey=sesskey, relogged=True)
    except Exception as exc:
        _write_failure(state_path, attempts=attempts, error=exc, now=now)
        raise AutoLoginUnavailable(f"LearnUs 자동 로그인 실패: {exc}") from exc
    finally:
        password = None
