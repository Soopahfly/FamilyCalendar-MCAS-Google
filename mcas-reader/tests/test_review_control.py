import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import mcas
import review_control as review


class ReviewControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(mcas, 'PRIVATE', Path(self.temp.name))
        self.patch.start()
        self.thread_patch = patch.object(review, 'THREAD', '00000000-0000-4000-8000-000000000000')
        self.thread_patch.start()

    def tearDown(self):
        self.thread_patch.stop()
        self.patch.stop()
        self.temp.cleanup()

    def test_queue_uses_fixed_chat_no_shell_and_reports_only_queued(self):
        result = subprocess.CompletedProcess([], 0, 'Queued message 1234-abcd for thread ' + review.THREAD + '.', '')
        with patch.object(review.subprocess, 'run', return_value=result) as run:
            status = review.queue_review()
        self.assertEqual(status['status'], 'queued')
        args, kwargs = run.call_args
        self.assertEqual(args[0][1:4], ['queue', '--thread', review.THREAD])
        self.assertNotIn('shell', kwargs)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args[0])
        self.assertEqual((mcas.PRIVATE / 'assistant-run-status.json').stat().st_mode & 0o777, 0o600)

    def test_duplicate_running_or_queued_requests_suppressed(self):
        mcas.save(mcas.PRIVATE / 'assistant-run-status.json', {'status': 'queued', 'requested_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'message': 'Queued'})
        with patch.object(review.subprocess, 'run') as run:
            self.assertTrue(review.queue_review()['duplicate_suppressed'])
            run.assert_not_called()

    def test_queue_failure_is_not_completion(self):
        with patch.object(review.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'private error')):
            status = review.queue_review()
        self.assertEqual(status['status'], 'needs_attention')
        self.assertNotIn('private error', json.dumps(status))

    def test_unverified_schedule_cannot_dispatch(self):
        mcas.save(mcas.PRIVATE / 'review-settings.json', {'enabled': True, 'validated': False, 'daily_time': '07:00', 'refresh_time': '17:15'})
        with patch.object(review, 'queue_review') as queue:
            review.tick(dt.datetime(2026, 10, 21, 18, tzinfo=dt.timezone.utc))
            queue.assert_not_called()

    def test_missing_chat_configuration_cannot_queue(self):
        with patch.object(review, 'THREAD', ''), patch.object(review.subprocess, 'run') as run:
            with self.assertRaises(mcas.ReaderError):
                review.queue_review()
            run.assert_not_called()
        self.assertFalse((mcas.PRIVATE / 'assistant-run-status.json').exists())

    def test_local_midweek_schedule_tracks_dst(self):
        mcas.save(mcas.PRIVATE / 'review-settings.json', {'enabled': True, 'validated': True, 'daily_time': '07:00', 'refresh_time': '17:15'})
        # Morning already covered; 17:15 London is 16:15 UTC before DST ends.
        mcas.save(mcas.PRIVATE / 'review-schedule-attempts.json', {'daily:2026-10-21': {}})
        with patch.object(review, 'queue_review', return_value={'status': 'queued'}) as queue:
            review.tick(dt.datetime(2026, 10, 21, 16, 14, tzinfo=dt.timezone.utc))
            queue.assert_not_called()
            review.tick(dt.datetime(2026, 10, 21, 16, 15, tzinfo=dt.timezone.utc))
            queue.assert_called_once_with('midweek', 'midweek:2026-10-21')

    def test_prompt_preserves_no_contact_and_verification_rules(self):
        text = review.prompt('daily', 'test-id')
        for required in ['Never RSVP', 'Reopen EVERY', '17:45 previous evening', 'initial review is incomplete', 'Stay quiet', 'validated=false']:
            self.assertIn(required, text)
        with self.assertRaises(mcas.ReaderError):
            review.prompt('arbitrary-command', 'test-id')


if __name__ == '__main__':
    unittest.main()
