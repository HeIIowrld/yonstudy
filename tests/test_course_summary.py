import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yonstudy.course_summary import plain_transcript, render_course_summary, summarize_courses
from yonstudy.export import export_tree
from yonstudy.lecture_pages import generated_lecture_documents, week_for_asset, week_for_source
from yonstudy.remote import prune_stale_lecture_pages, sync_generated_pages, sync_study_maps
from yonstudy.store import Store


class CourseSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "store")
        self.course = {
            "course_id": 1, "year": "2026", "semester": "2학기",
            "name": "자료구조", "title": "자료구조", "slug": "CAS2103_자료구조",
        }
        self.store.save_course(self.course)
        self.folder = self.root / "transcripts" / self.course["slug"]
        self.folder.mkdir(parents=True)
        self.srt = self.folder / "01주차 1강.ko.srt"
        self.srt.write_text(
            "1\n00:00:01,000 --> 00:00:05,000\n배열은 연속된 메모리에 데이터를 저장합니다.\n\n"
            "2\n00:00:05,000 --> 00:00:10,000\n인덱스로 빠르게 접근할 수 있지만 중간 삽입은 느립니다.\n\n"
            "3\n00:00:10,000 --> 00:00:15,000\n연결 리스트는 노드를 포인터로 잇고 삽입과 탐색의 비용이 다릅니다. 시간 복잡도를 함께 살펴봅니다.\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_strip_cues_without_dropping_speech(self):
        content = plain_transcript(self.srt.read_text(encoding="utf-8"))
        self.assertNotIn("00:00", content)
        self.assertNotIn("\n1\n", content)
        self.assertIn("연결 리스트", content)

    def test_cache_deduplicates_and_rebuilds_on_source_change(self):
        (self.folder / "W01__cmid10.ko.srt").write_bytes(self.srt.read_bytes())
        (self.root / "transcripts" / "전사_현황.md").write_text(
            "| 파일 | 상태 | 시도 | 사유 |\n"
            "| CAS2103_자료구조/01주차 1강.m4a | 정상 자막 유지 | 0 | |\n",
            encoding="utf-8",
        )
        generated = {
            "overview": "배열과 연결 리스트의 저장 구조와 연산 비용을 비교합니다.",
            "topics": ["배열의 인덱스 접근", "연결 리스트의 삽입"],
            "lectures": [{"source": self.srt.name, "summary": "두 자료구조를 비교합니다.",
                          "important_points": ["배열의 중간 삽입 비용"]}],
        }
        weekly = [{"week": 1, "overview": "1주차에는 배열과 연결 리스트의 저장 방식과 연산 비용을 비교합니다.",
                   "important_points": ["배열은 인덱스 접근이 빠릅니다."]}]
        with patch("yonstudy.course_summary._generate", return_value=generated) as call, patch(
            "yonstudy.course_summary._generate_weeks", return_value=weekly,
        ) as generate_weeks:
            result = summarize_courses(
                self.store, self.root / "transcripts", api_key="test", year="2026", semester="2학기",
            )
        self.assertEqual((result.generated, result.transcripts, result.unique_transcripts), (1, 2, 1))
        call.assert_called_once()
        generate_weeks.assert_called_once()
        summary = dict(self.store.query("SELECT * FROM course_summary WHERE course_id=1")[0])
        self.assertIn("중간 삽입 비용", render_course_summary(self.course, summary).decode())
        docs = generated_lecture_documents(self.course, summary)
        self.assertTrue(any(path.endswith("/W01-00__주차학습_자동생성.md") for path in docs))
        self.assertTrue(any("1강" in path and path.endswith("_요약.md") for path in docs))
        self.assertEqual(len([path for path in docs if path.endswith("_요약.md")]), 2)
        self.assertTrue(any("영상·녹음" in body.decode() for path, body in docs.items() if path.endswith("_요약.md")))
        self.assertIn("배열과 연결 리스트", next(body.decode() for path, body in docs.items() if path.endswith("W01-00__주차학습_자동생성.md")))
        with patch("yonstudy.course_summary._generate") as call:
            result = summarize_courses(
                self.store, self.root / "transcripts", api_key="test", year="2026", semester="2학기",
            )
        self.assertEqual(result.skipped, 1)
        call.assert_not_called()
        self.assertEqual(result.weeks_skipped, 1)

        output = self.root / "output"
        export_tree(self.store, output, year="2026", semester="2학기")
        index = (output / "2026-2" / self.course["slug"] / "강좌정보.md").read_text(encoding="utf-8")
        self.assertIn("강좌내용요약_자동생성.md", index)
        self.assertIn("배열과 연결 리스트", index)

        self.srt.write_text(self.srt.read_text(encoding="utf-8") + "\n4\n00:00:15,000 --> 00:00:20,000\n스택도 살펴봅니다.\n", encoding="utf-8")
        result = summarize_courses(
            self.store, self.root / "transcripts", api_key=None, year="2026", semester="2학기", dry_run=True,
        )
        self.assertEqual(result.pending, 1)

    def test_same_content_in_another_week_gets_own_page(self):
        (self.folder / "02주차 1강.ko.srt").write_bytes(self.srt.read_bytes())
        generated = {
            "overview": "배열과 연결 리스트를 비교하는 자료구조 강의입니다.",
            "topics": ["배열", "연결 리스트"],
            "lectures": [{"source": self.srt.name, "summary": "배열과 연결 리스트를 비교합니다.",
                          "important_points": ["배열의 접근 비용"]}],
        }
        weekly = [
            {"week": 1, "overview": "첫 주에는 배열과 연결 리스트의 성능을 비교합니다.",
             "important_points": ["배열의 접근 비용"]},
            {"week": 2, "overview": "둘째 주 녹음도 배열과 연결 리스트의 성능을 설명합니다.",
             "important_points": ["배열의 접근 비용"]},
        ]
        with patch("yonstudy.course_summary._generate", return_value=generated), patch(
            "yonstudy.course_summary._generate_weeks", return_value=weekly,
        ):
            result = summarize_courses(
                self.store, self.root / "transcripts", api_key="test", year="2026", semester="2학기",
            )
        self.assertEqual((result.transcripts, result.unique_transcripts), (2, 1))
        summary = dict(self.store.query("SELECT * FROM course_summary WHERE course_id=1")[0])
        docs = generated_lecture_documents(self.course, summary)
        self.assertTrue(any(path.endswith("/W02-00__주차학습_자동생성.md") for path in docs))
        self.assertTrue(any("02주차 1강" in path for path in docs))

    def test_moving_transcript_to_media_shelf_reuses_ai_summary(self):
        generated = {
            "overview": "배열과 연결 리스트의 기본 구조와 연산 비용을 설명합니다.",
            "topics": ["배열", "연결 리스트"],
            "lectures": [{"source": self.srt.name, "summary": "두 자료구조의 특징을 비교합니다.",
                          "important_points": ["배열의 인덱스 접근"]}],
        }
        weekly = [{"week": 1, "overview": "1주차에는 배열과 연결 리스트의 특징을 비교합니다.",
                   "important_points": ["배열의 인덱스 접근"]}]
        with patch("yonstudy.course_summary._generate", return_value=generated), patch(
            "yonstudy.course_summary._generate_weeks", return_value=weekly,
        ):
            summarize_courses(self.store, self.root / "transcripts", api_key="test",
                              year="2026", semester="2학기")
        moved = self.folder / "강의미디어" / self.srt.name
        moved.parent.mkdir()
        self.srt.rename(moved)
        with patch("yonstudy.course_summary._generate") as call, patch(
            "yonstudy.course_summary._generate_weeks",
        ) as week_call:
            result = summarize_courses(self.store, self.root / "transcripts", api_key="test",
                                       year="2026", semester="2학기")
        self.assertEqual((result.skipped, result.metadata_updates, result.weeks_skipped), (1, 1, 1))
        call.assert_not_called()
        week_call.assert_not_called()
        summary = dict(self.store.query("SELECT * FROM course_summary WHERE course_id=1")[0])
        self.assertIn("강의미디어/", summary["lectures_json"])

    def test_inventory_links_existing_materials_without_moving_them(self):
        files = [
            "W01-L00__강의자료__배열__f1.pdf", "강의자료/02주차/02-01 재귀.pdf",
            "자구1-1.m4a", "자구1-1.ko.srt", "md/03-01-그래프.md",
            "과제자료/과제 1/제출안내.md", "QNA_공지/공지/안내.md",
        ]
        docs = generated_lecture_documents(self.course, None, inventory=files)
        root = "2026-2/CAS2103_자료구조/"
        self.assertIn(root + "00_학습목차_자동생성.html", docs)
        self.assertFalse(any(path.startswith(root) and path[len(root):].endswith(".md")
                             and "/" not in path[len(root):] for path in docs))
        self.assertIn(root + "강의요약/W02-00__주차학습_자동생성.md", docs)
        week1 = docs[root + "강의요약/W01-00__주차학습_자동생성.md"].decode()
        self.assertIn("자구1-1.m4a", week1)
        self.assertIn("W01-L00__%EA%B0%95%EC%9D%98%EC%9E%90%EB%A3%8C__", week1)
        self.assertIn("과제자료", docs[root + "강의요약/00_학습목차_자동생성.md"].decode())
        self.assertEqual(week_for_asset("강의자료/02주차/02-01 재귀.pdf"), 2)
        self.assertEqual(week_for_asset("강의자료/13-03 - Course Wrap-Up.pdf"), 13)
        self.assertEqual(week_for_asset("md/13-03 - Course Wrap-Up.md"), 0)

    def test_user_notes_in_note_shelf_remain_in_week_index(self):
        from urllib.parse import unquote

        docs = generated_lecture_documents(
            self.course, None, inventory=["학습노트/week02.md"],
        )
        week2 = docs["2026-2/CAS2103_자료구조/강의요약/W02-00__주차학습_자동생성.md"].decode()
        self.assertIn("../학습노트/week02.md", unquote(week2))

    def test_only_obsolete_generated_lecture_pages_are_pruned(self):
        class Sink:
            def __init__(self, files):
                self.files = dict(files)

            def _load_term(self, _term):
                return self.files

            def delete_file(self, path):
                return self.files.pop(path, None) is not None

        base = "2026-2/CAS2103_자료구조/"
        old = base + "강의요약/01주차__10_옛강의__0123456789_요약.md"
        manual = base + "강의요약/개인_요약.md"
        source = base + "강의미디어/01주차.mp4"
        current = base + "W01-00__주차학습_자동생성.html"
        sink = Sink({old: 1, manual: 1, source: 1, current: 1})
        self.assertEqual(prune_stale_lecture_pages(sink, self.course, {current}), 1)
        self.assertEqual(set(sink.files), {manual, source, current})

    def test_generated_upload_touches_only_documents(self):
        class Sink:
            def __init__(self):
                self.files = {}

            def upload_bytes(self, path, body, force=False):
                self.files[path] = body
                return True

        sink = Sink()
        result = sync_generated_pages(self.store, sink, year="2026", semester="2학기")
        self.assertEqual(result["course_indexes"], 1)
        self.assertIn("2026-2/CAS2103_자료구조/강좌정보.md", sink.files)
        self.assertIn("2026-2/CAS2103_자료구조/00_학습목차_자동생성.html", sink.files)

    def test_study_map_can_index_existing_remote_without_changing_sources(self):
        class Sink:
            def __init__(self):
                self.source = "2026-2/CAS2103_자료구조/W01-L00__강의자료__배열__f1.pdf"
                self.files = {self.source: 123}
                self.uploaded = {}

            def _load_term(self, term):
                self.assert_term = term
                return self.files

            def upload_bytes(self, path, body, force=False):
                self.uploaded[path] = body
                return True

        sink = Sink()
        preview = sync_study_maps(self.store, sink, year="2026", semester="2학기", dry_run=True)
        self.assertEqual(preview["courses"], 1)
        self.assertEqual(preview["source_files"], 1)
        self.assertFalse(sink.uploaded)
        sync_study_maps(self.store, sink, year="2026", semester="2학기")
        self.assertIn(sink.source, sink.files)
        self.assertIn("2026-2/CAS2103_자료구조/W01-00__주차학습_자동생성.html", sink.uploaded)

    def test_failed_transcript_is_excluded_by_status(self):
        (self.root / "transcripts" / "전사_현황.md").write_text(
            "| 파일 | 상태 | 시도 | 사유 |\n"
            "| --- | --- | ---: | --- |\n"
            "| CAS2103_자료구조/01주차 1강.m4a | 자동 재시도 중단 | 2 | 검토 실패 |\n",
            encoding="utf-8",
        )
        result = summarize_courses(
            self.store, self.root / "transcripts", api_key=None,
            year="2026", semester="2학기", dry_run=True,
        )
        self.assertEqual(result.transcripts, 0)

    def test_week_inference_is_conservative(self):
        self.assertEqual(week_for_source("01주차 - Lec 1.en.srt"), 1)
        self.assertEqual(week_for_source("W04-L02__강의영상.en.srt"), 4)
        self.assertEqual(week_for_source("자구3-2.en.srt"), 3)
        self.assertEqual(week_for_source("Week 2 Lecture Video.en.srt"), 2)
        self.assertEqual(week_for_source("Lec 2 - 9_8.en.srt"), 0)


if __name__ == "__main__":
    unittest.main()
