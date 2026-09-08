import io
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path

from yonstudy.client import LearnUsClient


class _HttpErrorOpener:
    def __init__(self, code=503, body=b"<html>busy</html>"):
        self.code = code
        self.body = body

    def open(self, request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            self.code,
            "server error",
            {},
            io.BytesIO(self.body),
        )


class LearnUsClientTests(unittest.TestCase):
    def test_request_propagates_http_errors(self):
        with tempfile.TemporaryDirectory() as root:
            client = LearnUsClient(str(Path(root) / "cookies.txt"), min_interval=0)
            client.opener = _HttpErrorOpener()

            with self.assertRaises(urllib.error.HTTPError) as raised:
                client.request("https://ys.learnus.org/test")

            self.assertEqual(raised.exception.code, 503)
            self.assertGreaterEqual(client.min_interval, 1.0)

    def test_save_creates_private_parent_for_fresh_install(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "new-store" / "learnus-cookies.txt"
            client = LearnUsClient(str(path))

            client.save()

            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
