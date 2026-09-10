import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cli
from yonstudy.export import export_onedrive_tree, term_folder
from yonstudy.store import Store


class OneDriveExportTests(unittest.TestCase):
    def test_term_folder_codes(self):
        self.assertEqual(term_folder("2026", "1학기"), "2026-1")
        self.assertEqual(term_folder("2026", "2학기"), "2026-2")
        self.assertEqual(term_folder("2026", "여름계절수업"), "2026-S")
        self.assertEqual(term_folder("2026", "겨울계절수업"), "2026-W")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.save_course(
            {
                "course_id": 1,
                "year": "2026",
                "semester": "2학기",
                "name": "테스트과목",
                "title": "테스트과목 (TST1000)",
                "slug": "TST1000_테스트과목",
            }
        )
        self.store.save_activity(
            {
                "cmid": 10,
                "course_id": 1,
                "modname": "ubboard",
                "title": "Q&A 게시판",
                "url": "https://example.test/board/10",
                "restricted": 0,
            }
        )
        self.store.save_activity(
            {
                "cmid": 11,
                "course_id": 1,
                "modname": "ubfile",
                "title": "Lecture 1-2",
                "section_idx": 1,
                "section_name": "1주차",
                "url": "https://example.test/resource/11",
                "restricted": 0,
            }
        )

    def tearDown(self):
        self.tmp.cleanup()
        self.out.cleanup()

    def test_incremental_material_and_post_export(self):
        digest, size = self.store.put_blob(b"lecture material")
        self.store.save_file(
            {
                "course_id": 1,
                "cmid": 11,
                "role": "resource",
                "name": "강의안.pdf",
                "url": "https://example.test/file/1",
                "sha256": digest,
                "bytes": size,
            }
        )
        self.store.save_post(
            {
                "course_id": 1,
                "cmid": 10,
                "modname": "ubboard",
                "post_id": "42",
                "subject": "질문입니다",
                "writer": "학생",
                "written_at": "2026-09-01 12:00",
                "url": "https://example.test/post/42",
                "body": "질문 본문",
            }
        )
        self.store.commit()

        first = export_onedrive_tree(
            self.store, self.out.name, year="2026", semester="2학기"
        )
        self.assertEqual(first.material_files, 1)
        self.assertEqual(first.posts, 1)
        self.assertEqual(first.copied_files, 3)
        course_dir = Path(self.out.name) / "2026-2" / "TST1000_테스트과목"
        for category in ("게시판_첨부", "QNA_공지"):
            self.assertTrue((course_dir / category).is_dir())
        self.assertTrue((course_dir / "강좌정보.md").is_file())
        self.assertFalse((course_dir / "강의자료").exists())
        files = [p for p in Path(self.out.name).rglob("*") if p.is_file()]
        material = next(p for p in files if p.name.endswith("강의안__f1.pdf"))
        self.assertEqual(material.parent, course_dir)
        self.assertTrue(material.name.startswith("W01-L02__강의자료__"))
        post = next(p for p in files if p.suffix == ".md" and "QNA_공지" in p.parts)
        self.assertIn("질문 본문", post.read_text(encoding="utf-8"))

        second = export_onedrive_tree(
            self.store, self.out.name, year="2026", semester="2학기"
        )
        self.assertEqual(second.copied_files, 0)

    def test_cli_can_export_every_archived_term(self):
        self.store.save_course(
            {
                "course_id": 2,
                "year": "2025",
                "semester": "2학기",
                "name": "이전과목",
                "title": "이전과목",
                "slug": "OLD1000_이전과목",
            }
        )
        self.store.commit()
        args = SimpleNamespace(
            store=self.tmp.name, destination=self.out.name,
            year=None, semester=None, all_terms=True, dry_run=False,
        )

        with patch("builtins.print"):
            code = cli.cmd_export(args)

        self.assertEqual(code, 0)
        self.assertTrue((Path(self.out.name) / "2025-2" / "OLD1000_이전과목").is_dir())
        self.assertTrue((Path(self.out.name) / "2026-2" / "TST1000_테스트과목").is_dir())


if __name__ == "__main__":
    unittest.main()
