import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import inbox
import mcas


class Portal:
    def __init__(self, change_flag=False):
        self.calls = []
        self.reads = 0
        self.change_flag = change_flag

    def request(self, url, data=None, json_body=False):
        self.calls.append((url, data))
        if url.endswith('/MCSInbox'):
            return url, 'var ID = "45";'
        if url.endswith('/MCSAnnouncements'):
            return url, 'var master_userid = "45"; var master_studentid = "67";'
        route = data['url']
        if route.endswith('/conversations/true'):
            self.reads += 1
            result = {'Conversations': [{'Messages': [{'MessageID': 1, 'MessageDate': '2026-10-20', 'Subject': 'Test', 'Message': 'Whole body', 'IsRead': self.change_flag and self.reads > 1}]}],
                      'MessageAttachments': [{'MessageID': 1, 'AttachmentID': 2, 'FileName': '../../payload.txt'}]}
        elif '/attachments/' in route:
            result = base64.b64encode(b'Example evidence').decode()
        elif '/announcements/get/' in route:
            result = {'Table': [{'AnnouncementID': 9, 'IsRead': False}], 'Table1': []}
        else:
            raise AssertionError('Unexpected route: ' + route)
        return url, json.dumps({'d': json.dumps(result) if isinstance(result, dict) else result})


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = Path(self.temp.name)
        self.patch = patch.object(mcas, 'PRIVATE', self.private)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_full_body_attachment_and_read_flags(self):
        client = Portal()
        result = inbox.collect(client)
        self.assertEqual(result['conversations'][0]['Messages'][0]['Message'], 'Whole body')
        self.assertTrue(result['attachments_downloaded'])
        self.assertTrue(result['read_flags_preserved'])
        self.assertFalse(result['historical_retention_verified'])
        path = self.private / result['attachments'][0]['path']
        self.assertTrue(path.is_relative_to(self.private))
        self.assertEqual(path.read_bytes(), b'Example evidence')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for _, data in client.calls:
            if data:
                self.assertNotIn('update', data['url'].lower())
                self.assertNotIn('remove', data['url'].lower())
                self.assertNotIn('send', data['url'].lower())

    def test_read_flag_change_stops_collection(self):
        with self.assertRaisesRegex(mcas.ReaderError, 'read flags changed'):
            inbox.collect(Portal(change_flag=True))

    def test_read_whitelist_rejects_status_mutation(self):
        for route in ['api/v1/person/45/conversations/1/true', 'api/v1/mcas/user/sendMessage/45', 'api/v1/mcas/announcements/remove/45']:
            with self.assertRaises(mcas.ReaderError):
                inbox.proxy(Portal(), route)
            with self.assertRaises(mcas.ReaderError):
                inbox.proxy(Portal(), route, {})

    def test_future_pagination_never_silently_ignored(self):
        with self.assertRaises(mcas.ReaderError):
            inbox.unwrap_list({'Conversations': [], 'NextPageToken': 'more'}, 'Conversations')

    def test_missing_messages_never_means_empty_inbox(self):
        with self.assertRaises(mcas.ReaderError):
            inbox.unwrap_list({'Error': 'not authorized'}, 'Conversations')

    def test_explicit_null_attachment_list_is_empty(self):
        self.assertEqual(inbox.unwrap_list({'MessageAttachments': None}, 'MessageAttachments'), [])
        with self.assertRaises(mcas.ReaderError):
            inbox.unwrap_list({'Conversations': None}, 'Conversations')


if __name__ == '__main__':
    unittest.main()
