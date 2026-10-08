import tempfile
import unicodedata
import unittest
from pathlib import Path

from yonstudy.filename_normalization import nfc, normalize_tree, safe_filename
from yonstudy.store import Store
from yonstudy.remote import RcloneRemote


class FilenameNormalizationTests(unittest.TestCase):
    def test_attachment_filename_keeps_id_and_extension_within_cloud_sync_limit(self):
        name = safe_filename("123_" + unicodedata.normalize("NFD", "강의안") * 50 + ".docx")

        self.assertTrue(name.startswith("123_"))
        self.assertTrue(name.endswith(".docx"))
        self.assertLessEqual(len(name.encode("utf-8")), 128)
        self.assertEqual(name, unicodedata.normalize("NFC", name))

    def test_filename_recovers_utf8_mojibake_and_preserves_latin_names(self):
        corrupted = "협동학습2.pdf".encode("utf-8").decode("latin1")

        self.assertEqual(safe_filename("42_" + corrupted), "42_협동학습2.pdf")
        self.assertEqual(safe_filename("42_café.pdf"), "42_café.pdf")

    def test_filename_removes_controls_that_block_cloud_uploads(self):
        self.assertEqual(safe_filename("42_bad\x85name.pdf"), "42_bad_name.pdf")

    def test_remote_names_keep_extensions_and_ids_for_every_attachment_role(self):
        sink = object.__new__(RcloneRemote)
        for role in ("resource", "post", "submission", "introattachment", "subtitle"):
            with self.subTest(role=role):
                path = sink.file_path(
                    year="2026", semester="2학기", course_slug="테스트",
                    activity_title="강의", file_id=42, name="강의안" * 80 + ".docx",
                    role=role, section_idx=1,
                )
                name = Path(path).name
                self.assertLessEqual(len(name.encode("utf-8")), 128)
                self.assertTrue(name.endswith(".docx"))
                self.assertIn("42", name)

    def test_remote_post_filename_fits_cloud_sync_ascii_boundary(self):
        sink = object.__new__(RcloneRemote)
        path = sink.post_path(
            year="2026", semester="2학기", course_slug="테스트",
            board_title="공지", post_id="123456", subject="A" * 200,
            written_at="2026-10-08",
        )
        name = Path(path).name
        self.assertLessEqual(len(name.encode("utf-8")), 128)
        self.assertTrue(name.startswith("20261008_123456_"))
        self.assertTrue(name.endswith(".md"))

    def test_nfc_combines_mac_style_korean_jamo(self):
        decomposed = unicodedata.normalize("NFD", "강의자료.pdf")

        self.assertEqual(nfc(decomposed), "강의자료.pdf")

    def test_normalize_tree_renames_nested_files_and_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / unicodedata.normalize("NFD", "수업자료")
            folder.mkdir()
            (folder / unicodedata.normalize("NFD", "강의안.pdf")).write_bytes(b"pdf")

            result = normalize_tree(root)

            self.assertEqual(result.renamed, 2)
            self.assertTrue((root / "수업자료" / "강의안.pdf").is_file())

    def test_dry_run_does_not_change_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = unicodedata.normalize("NFD", "과제.txt")
            (root / name).write_text("answer", encoding="utf-8")

            result = normalize_tree(root, dry_run=True)

            self.assertEqual(result.renamed, 1)
            self.assertTrue((root / name).exists())

    def test_existing_target_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            decomposed = unicodedata.normalize("NFD", "자료.txt")
            (root / decomposed).write_text("mac", encoding="utf-8")
            (root / "자료.txt").write_text("windows", encoding="utf-8")

            result = normalize_tree(root)

            self.assertEqual(result.renamed, 0)
            self.assertEqual(len(result.failures), 1)
            self.assertEqual((root / "자료.txt").read_text(encoding="utf-8"), "windows")
            self.assertEqual((root / decomposed).read_text(encoding="utf-8"), "mac")

    def test_archive_store_uses_nfc_for_course_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            digest, _ = store.put_blob(b"pdf")
            course_dir = unicodedata.normalize("NFD", "2026-2/한국어")
            filename = unicodedata.normalize("NFD", "강의안.pdf")

            target = store.link_into_course(digest, course_dir, filename)

            relative = target.relative_to(Path(temporary) / "courses")
            self.assertEqual(relative.parts, ("2026-2", "한국어", "강의안.pdf"))
            self.assertEqual(target.read_bytes(), b"pdf")


if __name__ == "__main__":
    unittest.main()
