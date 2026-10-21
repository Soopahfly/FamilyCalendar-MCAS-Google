import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.parse

import google_calendar as google
import mcas


class GoogleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(mcas, 'PRIVATE', Path(self.temp.name))
        self.patch.start()
        self.family_patch = patch.object(google, 'FAMILY', 'family@example.invalid')
        self.accounts_patch = patch.object(google, 'ALLOWED_ACCOUNTS', {'parent@example.invalid'})
        self.family_patch.start()
        self.accounts_patch.start()
        self.config = json.dumps({'web': {'client_id': 'test.apps.googleusercontent.com', 'client_secret': 'fixture-not-a-secret', 'redirect_uris': [google.REDIRECT]}})

    def tearDown(self):
        self.accounts_patch.stop()
        self.family_patch.stop()
        self.patch.stop()
        self.temp.cleanup()

    def test_config_requires_exact_local_callback_and_web_type(self):
        with self.assertRaises(mcas.ReaderError):
            google.configure(json.dumps({'installed': {}}))
        bad = json.loads(self.config)
        bad['web']['redirect_uris'] = ['https://attacker.invalid/']
        with self.assertRaises(mcas.ReaderError):
            google.configure(json.dumps(bad))
        self.assertFalse(google.path('client').exists())

    def test_missing_destination_configuration_cannot_begin_consent(self):
        with patch.object(google, 'FAMILY', ''), patch.object(google, 'ALLOWED_ACCOUNTS', set()):
            with self.assertRaises(mcas.ReaderError):
                google.begin()
        self.assertFalse(google.path('pending').exists())

    def test_oauth_uses_state_pkce_offline_and_narrow_scopes(self):
        google.configure(self.config)
        url = google.begin()
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        pending = mcas.load(google.path('pending'))
        self.assertEqual(params['state'][0], pending['state'])
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertEqual(params['access_type'], ['offline'])
        self.assertEqual(params['redirect_uri'], [google.REDIRECT])
        self.assertNotIn('gmail', params['scope'][0])
        self.assertEqual(google.path('pending').stat().st_mode & 0o777, 0o600)

    def test_wrong_and_expired_state_never_exchange_code(self):
        google.configure(self.config)
        google.begin()
        with patch.object(google, 'http') as transport:
            with self.assertRaises(mcas.ReaderError):
                google.complete({'state': ['wrong'], 'code': ['code']})
            pending = mcas.load(google.path('pending'))
            pending['created'] = time.time() - 601
            mcas.save(google.path('pending'), pending)
            with self.assertRaises(mcas.ReaderError):
                google.complete({'state': [pending['state']], 'code': ['code']})
            transport.assert_not_called()

    def auth_transport(self, email='parent@example.invalid', scopes=None):
        def http(url, *args, **kwargs):
            if 'token' in url:
                return 200, {'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh', 'scope': ' '.join(google.SCOPES if scopes is None else scopes), 'expires_in': 3600}
            return 200, {'email': email, 'verified_email': True}
        return http

    def test_wrong_account_never_persists_tokens(self):
        google.configure(self.config)
        google.begin()
        pending = mcas.load(google.path('pending'))
        with patch.object(google, 'http', self.auth_transport('someone@example.invalid')):
            with self.assertRaises(mcas.ReaderError):
                google.complete({'state': [pending['state']], 'code': ['code']})
        self.assertFalse(google.path('tokens').exists())
        self.assertFalse(google.path('pending').exists())

    def test_partial_consent_never_connects(self):
        google.configure(self.config)
        google.begin()
        pending = mcas.load(google.path('pending'))
        with patch.object(google, 'http', self.auth_transport(scopes=[])):
            with self.assertRaises(mcas.ReaderError):
                google.complete({'state': [pending['state']], 'code': ['code']})
        self.assertFalse(google.path('tokens').exists())

    def test_wrong_calendar_rolls_back_tokens(self):
        google.configure(self.config)
        google.begin()
        pending = mcas.load(google.path('pending'))
        with patch.object(google, 'http', self.auth_transport()), patch.object(google, 'verify_destination', side_effect=mcas.ReaderError('No Family')):
            with self.assertRaises(mcas.ReaderError):
                google.complete({'state': [pending['state']], 'code': ['code']})
        self.assertFalse(google.path('tokens').exists())

    def test_successful_consent_keeps_automation_disabled(self):
        google.configure(self.config)
        google.begin()
        pending = mcas.load(google.path('pending'))
        with patch.object(google, 'http', self.auth_transport()), patch.object(google, 'verify_destination'):
            google.complete({'state': [pending['state']], 'code': ['code']})
        self.assertFalse(google.status()['automatic_writes'])
        self.assertEqual(google.path('tokens').stat().st_mode & 0o777, 0o600)

    def run_calendar_test(self, bad_alarm=False):
        events = {}
        writes = []
        def api(suffix, method='GET', data=None, etag=None):
            if method == 'POST':
                self.assertIn('sendUpdates=none', suffix)
                self.assertEqual(data['attendees'], [])
                writes.append(data)
                events[data['id']] = json.loads(json.dumps(data))
                if bad_alarm:
                    events[data['id']]['reminders'] = {'useDefault': True}
                return 200, events[data['id']]
            event_id = suffix.split('?')[0].rsplit('/', 1)[-1]
            if method == 'DELETE':
                self.assertIn('sendUpdates=none', suffix)
                events.pop(event_id, None)
                return 204, {}
            return (200, events[event_id]) if event_id in events else (404, {})
        mcas.save(google.path('tokens'), {'account': 'parent@example.invalid'})
        with patch.object(google, 'verify_destination'), patch.object(google, 'api', api):
            if bad_alarm:
                with self.assertRaises(mcas.ReaderError):
                    google.test_connection()
            else:
                google.test_connection()
        self.assertFalse(events)
        self.assertTrue(mcas.load(google.path('test'))['removed_verified'])
        return writes

    def test_connection_reopens_checks_alarm_and_verifies_removal(self):
        writes = self.run_calendar_test()
        self.assertEqual(len(writes), 1)
        self.assertFalse(google.status()['automatic_writes'])
        self.assertTrue(mcas.load(google.path('test'))['event_verified'])

    def test_failed_alarm_verification_still_cleans_up(self):
        self.run_calendar_test(bad_alarm=True)
        self.assertFalse(mcas.load(google.path('test'))['event_verified'])

    def test_cannot_delete_unrelated_event(self):
        with patch.object(google, 'api', return_value=(200, {'id': 'other'})) as api:
            with self.assertRaises(mcas.ReaderError):
                google.cleanup_test({'event_id': 'known-test-id'})
            self.assertEqual(api.call_count, 1)

    def test_refresh_updates_tokens_privately(self):
        google.configure(self.config)
        mcas.save(google.path('tokens'), {'access_token': 'old', 'refresh_token': 'fixture-refresh', 'expires_at': 0})
        with patch.object(google, 'http', return_value=(200, {'access_token': 'new', 'expires_in': 3600})):
            self.assertEqual(google.access_token(), 'new')
        self.assertEqual(mcas.load(google.path('tokens'))['refresh_token'], 'fixture-refresh')


if __name__ == '__main__':
    unittest.main()
