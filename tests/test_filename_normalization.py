import tempfile
import unicodedata
import unittest
from pathlib import Path

from yonstudy.filename_normalization import nfc, normalize_tree
from yonstudy.store import Store


class FilenameNormalizationTests(unittest.TestCase):
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
