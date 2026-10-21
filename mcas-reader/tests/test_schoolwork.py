import json
import unittest

import mcas
import schoolwork


def calendar_page(records):
    rows = [['cell', 'cell', json.dumps(row)] for row in records]
    return 'var page = {"targetControls":' + json.dumps(repr(rows)) + '};'


class SchoolworkTests(unittest.TestCase):
    def test_normal_days_excluded_and_ranges_keep_actual_labels(self):
        page = calendar_page([
            {'date': '2026-10-23T00:00:00', 'text': 'Normal Day'},
            {'date': '2026-10-26T00:00:00', 'text': 'Holiday'},
            {'date': '2026-10-27T00:00:00', 'text': 'Holiday'},
            {'date': '2026-10-28T00:00:00', 'text': 'Staff Only'},
        ])
        ranges = schoolwork.important_ranges(schoolwork.calendar_days(page), '2026-10-20')
        self.assertEqual(ranges, [{'start': '2026-10-26', 'end': '2026-10-27', 'label': 'Holiday'}, {'start': '2026-10-28', 'end': '2026-10-28', 'label': 'Staff Only'}])

    def test_ranges_do_not_invent_holiday_weekends(self):
        rows = [{'date': '2026-10-30', 'label': 'Holiday'}, {'date': '2026-11-02', 'label': 'Holiday'}]
        self.assertEqual(len(schoolwork.important_ranges(rows, '2026-10-20')), 2)

    def test_missing_calendar_is_failure_not_no_holidays(self):
        with self.assertRaises(mcas.ReaderError):
            schoolwork.calendar_days('<html>Session expired</html>')

    def test_conflicting_calendar_labels_raise(self):
        with self.assertRaises(mcas.ReaderError):
            schoolwork.calendar_days(calendar_page([{'date': '2026-10-26', 'text': 'Normal Day'}, {'date': '2026-10-26', 'text': 'Holiday'}]))

    def test_homework_preserves_set_due_and_does_not_infer_completion(self):
        row = {'HomeworkID': 12, 'SchoolID': 34, 'HomeworkTitle': 'Fractions', 'AssignedDate': '2026-10-15T00:00:00', 'DueDate': '2026-10-22T23:59:59', 'SubmittedWork': '', 'HomeworkDescription': '<p>Complete on MathsWatch.</p>'}
        result = schoolwork.homework('var homeWorkListItem = ' + json.dumps([row]) + ';')[0]
        self.assertEqual(result['set_at'], row['AssignedDate'])
        self.assertEqual(result['due_at'], row['DueDate'])
        self.assertEqual(result['completion_status'], 'unknown')
        self.assertEqual(result['instructions'], 'Complete on MathsWatch.')

    def test_missing_homework_not_silently_empty(self):
        with self.assertRaises(mcas.ReaderError):
            schoolwork.homework('<html>No module</html>')
        self.assertEqual(schoolwork.homework('var homeWorkListItem = [];'), [])

    def test_duplicate_ids_and_invalid_dates_stop_parsing(self):
        row = {'HomeworkID': 1, 'HomeworkTitle': 'Task', 'DueDate': 'tomorrow'}
        with self.assertRaises(mcas.ReaderError):
            schoolwork.homework('var homeWorkListItem = ' + json.dumps([row]))
        row['DueDate'] = None
        with self.assertRaises(mcas.ReaderError):
            schoolwork.homework('var homeWorkListItem = ' + json.dumps([row, row]))

    def test_module_absence_is_explicit_and_never_guessed(self):
        class Client:
            def request(self, *args):
                raise AssertionError('Should not guess absent module paths')
        data = schoolwork.collect(Client(), mcas.BASE + '/MCAS/MCSDashboardPage', '<html></html>')
        self.assertFalse(data['homework_available'])
        self.assertFalse(data['calendar_available'])


if __name__ == '__main__':
    unittest.main()
