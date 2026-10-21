import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mcas


class FakeClient:
    def collect_schoolwork(self, url, dashboard):
        return {'errors': []}
    def collect_messages(self):
        raise mcas.ReaderError('Messages not present in this test fixture')

    def login(self, email, password):
        return "123", mcas.BASE + "/MCAS/MCSDashboard", "<html></html>"

    def read(self, name, sid):
        return {"Table": [{"id": name}]}

    def discover(self, url, dashboard):
        return {"pages": {}, "scripts": {}}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = Path(self.temp.name) / "private"
        self.patch = patch.object(mcas, "PRIVATE", self.private)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def credentials(self):
        mcas.save(self.private / "credentials.json", {"email": "test@example.invalid", "password": "not-a-real-secret"})

    def test_no_credentials_never_connects(self):
        def no_client():
            self.fail("Network client created without credentials")
        state = mcas.run_once(no_client)
        self.assertEqual(state["status"], "needs_login")
        self.assertFalse(state["messages_complete"])
        self.assertFalse(mcas.run_once(no_client)["attention_changed"])

    def test_partial_is_never_complete_and_unchanged_runs_are_quiet(self):
        self.credentials()
        first = mcas.run_once(FakeClient)
        second = mcas.run_once(FakeClient)
        self.assertEqual(first["status"], "partial")
        self.assertTrue(first["attention_changed"])
        self.assertFalse(second["attention_changed"])
        self.assertFalse(second["calendar_writes"])
        evidence = mcas.load(self.private / "latest.json")
        self.assertFalse(evidence["messages_complete"])
        self.assertFalse(evidence["attachments_complete"])
        self.assertEqual(len(list((self.private / "snapshots").glob("*.json"))), 1)

    def test_failed_fetch_keeps_previous_evidence(self):
        self.credentials()
        mcas.run_once(FakeClient)
        before = (self.private / "latest.json").read_bytes()
        class Failure(FakeClient):
            def login(self, *args):
                raise mcas.ReaderError("Login needs attention")
        state = mcas.run_once(Failure)
        self.assertEqual(state["status"], "error")
        self.assertEqual((self.private / "latest.json").read_bytes(), before)
        self.assertFalse(mcas.run_once(Failure)["attention_changed"])

    def test_missing_module_is_reported_not_empty_success(self):
        self.credentials()
        class Missing(FakeClient):
            def read(self, name, sid):
                if name == "reports":
                    raise mcas.ReaderError("Unavailable")
                return super().read(name, sid)
        mcas.run_once(Missing)
        data = mcas.load(self.private / "latest.json")
        self.assertEqual(data["gaps"][0]["source"], "reports")
        self.assertNotIn("reports", data["data"])

    def test_private_permissions(self):
        self.credentials()
        mcas.run_once(FakeClient)
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o700)
        for path in self.private.rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_daily_schedule_tracks_london_dst_and_runs_once(self):
        # 07:00 London is 06:00 UTC before the autumn clock change.
        self.assertFalse(mcas.due(dt.datetime(2026, 10, 24, 5, 59, tzinfo=dt.timezone.utc), None))
        self.assertTrue(mcas.due(dt.datetime(2026, 10, 24, 6, 0, tzinfo=dt.timezone.utc), None))
        self.assertFalse(mcas.due(dt.datetime(2026, 10, 25, 6, 59, tzinfo=dt.timezone.utc), None))
        self.assertTrue(mcas.due(dt.datetime(2026, 10, 25, 7, 0, tzinfo=dt.timezone.utc), None))
        self.assertFalse(mcas.due(dt.datetime(2026, 10, 25, 20, 0, tzinfo=dt.timezone.utc), "2026-10-25"))

    def test_unapproved_routes_cannot_be_called(self):
        client = mcas.Client()
        with self.assertRaises(mcas.ReaderError):
            client.read("sendMessage", "123")
        with self.assertRaises(mcas.ReaderError):
            client.request("https://example.invalid/steal")

    def test_login_parser_handles_html_entities(self):
        page = mcas.Page('<input value="a&amp;b" name="__VIEWSTATE" type="hidden"><script src="/inbox.js"></script>')
        self.assertEqual(page.hidden["__VIEWSTATE"], "a&b")
        self.assertEqual(page.scripts, ["/inbox.js"])

    def test_login_does_not_bypass_account_setup(self):
        client = mcas.Client()
        responses = iter([(mcas.LOGIN, '<input type="hidden" name="__VIEWSTATE" value="x">'),
                          (mcas.BASE + "/MCAS/MCSTermsAndConditions", "private")])
        client.request = lambda *args: next(responses)
        with self.assertRaises(mcas.ReaderError):
            client.login("test", "secret")

    def test_contact_selection_uses_only_offered_context(self):
        client = mcas.Client()
        calls = []
        responses = iter([
            (mcas.LOGIN, '<input type="hidden" name="__VIEWSTATE" value="x">'),
            (mcas.BASE + '/MCAS/MCSContactSelect', '<input name="userSelectorRadio" value="12_34_56"><input name="userSelectorRadio" value="12_34_78">'),
            (mcas.BASE + '/MCAS/MCSDashboard', 'var master_studentid = "56";'),
        ])
        def request(*args):
            calls.append(args)
            return next(responses)
        client.request = request
        self.assertEqual(client.login('test', 'secret')[0], '56')
        self.assertEqual(len(client.profile_choices), 2)
        self.assertIn('SchoolID=12&StudentID=56&ContactID=34', calls[-1][0])
        with self.assertRaises(mcas.ReaderError):
            client.select_profile('99_99_99')

    def test_profile_selection_checks_returned_pupil(self):
        client = mcas.Client()
        client.profile_choices = ['12_34_56']
        client.selection_url = mcas.BASE + '/MCAS/MCSContactSelect'
        client.request = lambda *args: (mcas.BASE + '/MCAS/MCSDashboard', 'var master_studentid = "999";')
        with self.assertRaises(mcas.ReaderError):
            client.select_profile('12_34_56')

    def test_collection_visits_every_offered_profile(self):
        self.credentials()
        class Multiple(FakeClient):
            profile_choices = ['12_34_56', '12_34_78']
            def login(self, *args):
                return '56', mcas.BASE + '/MCAS/MCSDashboard', ''
            def select_profile(self, choice):
                return choice.split('_')[-1], mcas.BASE + '/MCAS/MCSDashboard', ''
        state = mcas.run_once(Multiple)
        self.assertEqual(state['profiles_collected'], 2)
        data = mcas.load(self.private / 'latest.json')
        self.assertEqual(set(data['data']), set(Multiple.profile_choices))


if __name__ == "__main__":
    unittest.main()
