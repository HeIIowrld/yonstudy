import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cli
from yonstudy.archive import Archiver
from yonstudy.onedrive import RcloneOneDrive
from yonstudy.remote import RemoteStorageError, RcloneRemote, sync_remote_tree
from yonstudy.store import Store


class RcloneRemoteTests(unittest.TestCase):
    def test_legacy_onedrive_name_points_to_generic_remote(self):
        self.assertIs(RcloneOneDrive, RcloneRemote)

    def test_remote_must_use_rclone_name_syntax(self):
        with patch("yonstudy.remote.shutil.which", return_value="/usr/bin/rclone"):
            with self.assertRaisesRegex(RemoteStorageError, "remote"):
                RcloneRemote("missing-colon")

    def test_configured_remote_and_subdirectory_are_kept(self):
        completed = SimpleNamespace(returncode=0, stdout="nas:\n", stderr="")
        with (
            patch("yonstudy.remote.shutil.which", return_value="/usr/bin/rclone"),
            patch("yonstudy.remote.subprocess.run", return_value=completed),
        ):
            remote = RcloneRemote("nas:backup/yonstudy/")

        self.assertEqual(remote.remote, "nas:backup/yonstudy")
        self.assertEqual(
            remote._target("2026-2/course/file.pdf"),
            "nas:backup/yonstudy/2026-2/course/file.pdf",
        )

    def test_sync_streams_existing_blob_with_copyto_path(self):
        class MemoryRemote:
            remote = "nas:backup"

            def __init__(self):
                self.files = {}
                self.uploaded_from = None

            def file_path(self, **values):
                return f"2026-2/{values['course_slug']}/{values['name']}"

            def post_path(self, **_values):
                return "unused"

            def exists(self, relative, size=None):
                body = self.files.get(relative)
                return body is not None and (size is None or len(body) == size)

            def move(self, *_args, **_kwargs):
                return False

            def upload_file(self, relative, source, force=False):
                self.uploaded_from = Path(source)
                self.files[relative] = Path(source).read_bytes()
                return True

            def upload_bytes(self, relative, body, force=False):
                self.files[relative] = body
                return True

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_course({
                "course_id": 1,
                "year": "2026",
                "semester": "2학기",
                "name": "테스트",
                "title": "테스트",
                "slug": "TST_테스트",
            })
            store.save_activity({
                "cmid": 10,
                "course_id": 1,
                "modname": "ubfile",
                "title": "강의안",
            })
            digest, size = store.put_blob(b"lecture")
            store.save_file({
                "course_id": 1,
                "cmid": 10,
                "role": "resource",
                "name": "lecture.pdf",
                "url": "https://example.test/lecture",
                "sha256": digest,
                "bytes": size,
            })
            store.commit()

            remote = MemoryRemote()
            result = sync_remote_tree(
                store, remote, year="2026", semester="2학기"
            )

            self.assertEqual(result.uploaded_files, 1)
            self.assertEqual(remote.uploaded_from, store.blob_path(digest))
            self.assertEqual(
                remote.files["2026-2/TST_테스트/lecture.pdf"], b"lecture"
            )

    def test_withdrawn_course_is_skipped_but_classmate_attachment_is_kept(self):
        class MemoryRemote:
            remote = "nas:backup"

            def __init__(self):
                self.files = {}

            def file_path(self, **values):
                return f"2026-2/{values['course_slug']}/{values['name']}"

            def post_path(self, **values):
                return f"2026-2/{values['course_slug']}/{values['post_id']}.md"

            def exists(self, relative, size=None):
                return False

            def move(self, *_args, **_kwargs):
                return False

            def upload_file(self, relative, source, force=False):
                self.files[relative] = Path(source).read_bytes()
                return True

            def upload_bytes(self, relative, body, force=False):
                self.files[relative] = body
                return True

        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            for course_id, enrolled, slug in ((1, 1, "ACTIVE"), (2, 0, "OLD")):
                store.save_course({
                    "course_id": course_id, "year": "2026", "semester": "2학기",
                    "name": slug, "title": slug, "slug": slug,
                    "enrolled": enrolled,
                })
                store.save_activity({
                    "cmid": course_id * 10, "course_id": course_id,
                    "modname": "ubboard" if enrolled else "ubfile",
                    "title": "익명 Q&A" if enrolled else "예전 자료",
                })
                digest, size = store.put_blob(
                    b"classmate attachment" if enrolled else b"withdrawn material"
                )
                store.save_file({
                    "course_id": course_id, "cmid": course_id * 10,
                    "role": "post" if enrolled else "resource",
                    "name": "answer.pdf" if enrolled else "old.pdf",
                    "url": f"https://example.test/{course_id}",
                    "sha256": digest, "bytes": size,
                })
            store.commit()

            remote = MemoryRemote()
            result = sync_remote_tree(
                store, remote, year="2026", semester="2학기"
            )

            self.assertEqual(result.courses, 1)
            self.assertEqual(result.board_attachments, 1)
            self.assertEqual(result.material_files, 0)
            self.assertIn("2026-2/ACTIVE/answer.pdf", remote.files)
            self.assertNotIn("2026-2/OLD/old.pdf", remote.files)

    def test_missing_remote_file_can_be_collected_locally_again(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.save_file({
                "course_id": 1,
                "cmid": 10,
                "role": "resource",
                "name": "lecture.pdf",
                "url": "https://example.test/lecture",
                "sha256": "hash-from-old-remote",
                "bytes": 100,
                "remote_path": "2026-2/TST/lecture.pdf",
                "remote_status": "missing",
            })
            store.commit()
            existing = store.file_record(
                "https://example.test/lecture", "resource"
            )

            current = Archiver(
                None, store, verbose=False
            )._file_is_current(
                SimpleNamespace(year="2026", semester="2학기", slug="TST"),
                SimpleNamespace(cmid=10, title="강의안"),
                existing,
                "resource",
                "lecture.pdf",
            )

            self.assertFalse(current)

    def test_upload_dry_run_does_not_require_rclone_connection(self):
        with tempfile.TemporaryDirectory() as root:
            output = StringIO()
            args = SimpleNamespace(
                store=root,
                year="2026",
                semester="2학기",
                remote="not-configured:anywhere",
                dry_run=True,
            )

            with redirect_stdout(output):
                code = cli.cmd_upload(args)

            self.assertEqual(code, 0)
            self.assertIn('"mode": "dry-run"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
