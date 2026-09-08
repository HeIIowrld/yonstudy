import unittest

from yonstudy.parse import (
    ProgressRow, _completion_state, find_activity_ids, parse_course_list,
    parse_course_page,
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
