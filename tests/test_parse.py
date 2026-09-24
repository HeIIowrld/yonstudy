import unittest

from yonstudy.parse import (
    ProgressRow, _completion_state, find_activity_ids, parse_course_list,
    parse_course_page, parse_submission,
)


class RosterAndActivityMarkupTests(unittest.TestCase):
    def test_course_list_accepts_extra_classes_single_quotes_and_query_order(self):
        page = """
        <table><tbody data-kind='courses' class='table striped my-course-lists'>
          <tr><TD>2026</TD><TD>2학기</TD><td>
            <span class='small badge badge-course'>교과</span>
            <a class='coursefullname' href='https://ys.learnus.org/course/view.php?x=1&amp;id=7'>
              자료구조 (CSE1000.01-00)
            </a>
          </td></tr>
        </tbody></table>
        """

        courses = parse_course_list(page)

        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].course_id, 7)
        self.assertEqual(courses[0].name, "자료구조")
        self.assertEqual(courses[0].kind, "교과")

    def test_activity_parser_accepts_reordered_attributes_and_excludes_labels(self):
        page = """
        <li class='section main' id='section-2'>
          <h3 class='sectionname extra'>2주차</h3>
          <li data-x='1' id='module-10' class='url activity modtype_url'>
            <a href='/mod/url/view.php?x=1&amp;id=10'>
              <span class='extra instancename'>알고리즘 링크</span>
            </a>
          </li>
          <li id='module-11' class='activity label modtype_label'></li>
        </li>
        """

        activities, _ = parse_course_page(page)

        self.assertEqual(find_activity_ids(page), {10})
        self.assertEqual([row.cmid for row in activities], [10])
        self.assertEqual(activities[0].title, "알고리즘 링크")
        self.assertEqual(activities[0].section_name, "2주차")
        self.assertEqual(
            activities[0].url,
            "https://ys.learnus.org/mod/url/view.php?x=1&id=10",
        )

    def test_duplicate_module_markup_is_archived_once(self):
        module = """
        <li id="module-10" class="activity modtype_turnitintooltwo">
          <a href="/mod/turnitintooltwo/view.php?id=10">
            <span class="instancename">Assignment #1</span>
          </a>
        </li>
        """

        activities, _ = parse_course_page(module + module)

        self.assertEqual([row.cmid for row in activities], [10])

    def test_module_like_id_inside_activity_description_is_ignored(self):
        page = """
        <li id="module-10" class="activity url modtype_url">
          <a href="/mod/url/view.php?id=10">
            <span class="instancename">실습 코드</span>
          </a>
          <div class="contentafterlink">
            <ul><li id="module-999">설명 안에서 우연히 사용한 ID</li></ul>
          </div>
        </li>
        """

        self.assertEqual(find_activity_ids(page), {10})


class SubmissionParsingTests(unittest.TestCase):
    def test_turnitin_dates_and_inline_instructions_are_preserved(self):
        page = """
        <table class="partDetails"><thead><tr>
          <th>제목</th><th>시작일</th><th>마감일</th><th>게시일</th>
          <th>가능한 최고점수</th>
        </tr></thead><tbody>
          <tr><td>Assignment #1</td><td>2026- 9월-08 14:08</td>
          <td>2026- 9월-15 23:59</td><td>2026- 9월-15 14:08</td><td>100</td></tr>
          <tr class="lastrow"><td colspan="5"><div class="no-overflow">
            <h1>AI 실험</h1><h2>Goal</h2><p>직접 AI를 시험한다.</p>
            <ol><li>잘한 사례</li><li>틀린 사례</li></ol>
          </div></td></tr>
        </tbody></table>
        """

        detail = parse_submission(page, 10, "turnitintooltwo")

        self.assertEqual(detail.fields["Start date"], "2026- 9월-08 14:08")
        self.assertEqual(detail.fields["Due date"], "2026- 9월-15 23:59")
        self.assertEqual(detail.fields["Post date"], "2026- 9월-15 14:08")
        self.assertIn("# AI 실험", detail.instructions)
        self.assertIn("1. 잘한 사례", detail.instructions)
        self.assertIn("<h2>Goal</h2>", detail.instructions_html)

    def test_standard_assignment_intro_and_inline_image_are_preserved(self):
        page = """
        <div id="intro" class="box generalbox"><div class="no-overflow">
          <p>보고서 명세</p>
          <img alt="양식" src="/pluginfile.php/1/mod_assign/intro/report.png">
        </div></div>
        <table><tr><td>Submission status</td><td>No submission</td></tr></table>
        """

        detail = parse_submission(page, 11, "assign")

        self.assertIn("보고서 명세", detail.instructions)
        self.assertEqual(len(detail.intro_files), 1)
        self.assertEqual(detail.intro_files[0][0], "양식")

    def test_image_only_intros_and_distinct_images_are_not_discarded(self):
        page = '''<div id="intro"><img src="/pluginfile.php/1/mod_assign/intro/a.png"></div>
        <div class="activity-description"><img src="/pluginfile.php/1/mod_assign/intro/b.png"></div>'''
        detail = parse_submission(page, 12)
        self.assertIn('a.png)', detail.instructions)
        self.assertIn('b.png)', detail.instructions)
        self.assertEqual(len(detail.intro_files), 2)

    def test_nested_description_relative_attachment_and_code(self):
        page = '''<div id="intro"><section class="activity-description">
        <ol><li><p>Run <code>solve()</code></p><pre>if n &lt; 2:
    return n</pre></li></ol>
        <a href='/pluginfile.php/1/mod_assign/introattachment/spec.pdf'>
        <img class="icon icon" src="/theme/image.php/a/core/1/f/pdf">Specification</a>
        </section></div>'''
        detail = parse_submission(page, 13)
        self.assertEqual(detail.instructions.count('Run'), 1)
        self.assertIn('1. Run `solve()`', detail.instructions)
        self.assertIn('if n < 2:', detail.instructions)
        self.assertIn('       return n', detail.instructions)
        self.assertNotIn('theme/image.php', detail.instructions)
        self.assertEqual(detail.intro_files, [('Specification', 'https://ys.learnus.org/pluginfile.php/1/mod_assign/introattachment/spec.pdf')])

    def test_turnitin_nested_instruction_table_is_not_cut_off(self):
        page = '''<table class="extra partDetails"><tr><td><div class="no-overflow">
        <table><tr><th>Input</th><th>Output</th></tr><tr><td>1</td><td>2</td></tr></table>
        <p>Submission requirement after table.</p></div></td></tr></table>'''
        detail = parse_submission(page, 14, 'turnitintooltwo')
        self.assertIn('| Input | Output |', detail.instructions)
        self.assertIn('Submission requirement after table.', detail.instructions)


class CompletionStateTests(unittest.TestCase):
    def test_legacy_icons(self):
        self.assertEqual(_completion_state('<img src="completion-auto-y">'), "y")
        self.assertEqual(_completion_state('<img src="completion-manual-n">'), "n")

    def test_data_state(self):
        self.assertEqual(_completion_state('<button data-completionstate="1">'), "y")
        self.assertEqual(_completion_state('<button data-activity-completionstate="0">'), "n")

    def test_manual_checkbox(self):
        self.assertEqual(
            _completion_state('<input class="activity-completion" type="checkbox" checked>'),
            "y",
        )
        self.assertEqual(
            _completion_state('<input class="activity-completion" type="checkbox">'),
            "n",
        )
        self.assertEqual(
            _completion_state(
                '<form class="togglecompletion"><input type="checkbox" checked></form>'
            ),
            "y",
        )

    def test_aria_and_localized_labels(self):
        self.assertEqual(
            _completion_state('<button class="completion-toggle" aria-pressed="true">'),
            "y",
        )
        self.assertEqual(_completion_state('<img title="완료로 표시: 1주차">'), "n")
        self.assertEqual(
            _completion_state('<img title="완료하지 않은 것으로 표시: 1주차">'),
            "y",
        )

    def test_untracked_is_not_incomplete(self):
        self.assertIsNone(_completion_state('<li><input type="checkbox"></li>'))


class ProgressRowTests(unittest.TestCase):
    def test_localized_details_suffix_does_not_truncate_seconds(self):
        row = ProgressRow(
            week="1",
            title="강의",
            content_time="56:40",
            max_position="56:40 상세보기 (1)",
            progress="100%",
        )
        self.assertEqual(row.duration_sec, 3400)
        self.assertEqual(row.watched_sec, 3400)


if __name__ == "__main__":
    unittest.main()
