import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from yonstudy.auth import AutoLoginUnavailable, ensure_session


class FakeClient:
    def __init__(self, states, login_error=None):
        self.states = iter(states)
        self.login_error = login_error
        self.logins = []
        self.saved = 0

    def session_info(self):
        return next(self.states)

    def login(self, username, password):
        self.logins.append((username, password))
        if self.login_error:
            raise self.login_error

    def save(self):
        self.saved += 1


class AutoLoginTests(unittest.TestCase):
    def test_expired_session_is_reauthenticated_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"LEARNUS_ID": "student", "LEARNUS_PW": "secret"}
        ):
            client = FakeClient([(False, None), (True, "sess")])
            result = ensure_session(client, state_path=Path(tmp) / "auth.json")
            self.assertTrue(result.relogged)
            self.assertEqual(result.sesskey, "sess")
            self.assertEqual(client.logins, [("student", "secret")])
            self.assertEqual(client.saved, 1)

    def test_missing_credentials_does_not_attempt_login(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            client = FakeClient([(False, None)])
            with self.assertRaisesRegex(AutoLoginUnavailable, "자격 증명 미설정"):
                ensure_session(client, state_path=Path(tmp) / "auth.json")
            self.assertEqual(client.logins, [])

    def test_failed_login_is_backed_off(self):
        now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            first = FakeClient([(False, None)], login_error=RuntimeError("denied"))
            with self.assertRaisesRegex(AutoLoginUnavailable, "자동 로그인 실패"):
                ensure_session(
                    first, state_path=path, username="student", password="wrong", now=now
                )
            second = FakeClient([(False, None)])
            with self.assertRaisesRegex(AutoLoginUnavailable, "재시도 보류"):
                ensure_session(
                    second, state_path=path, username="student", password="wrong", now=now
                )
            self.assertEqual(second.logins, [])


if __name__ == "__main__":
    unittest.main()
