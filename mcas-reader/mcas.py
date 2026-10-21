"""Standalone, read-only MCAS evidence collector. Python standard library only.

Protocol reference: robbrad/ha-mychildatschool-mcas (MIT).
No inbox body endpoint is called until its read-status behaviour is established.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import getpass
import hashlib
import html
from html.parser import HTMLParser
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

if __name__ == "__main__":
    sys.modules["mcas"] = sys.modules[__name__]

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"
BASE = "https://www.mychildatschool.com"
LOGIN = BASE + "/MCAS/MCSParentLogin"
PROXY = BASE + "/MCAS/WebServices/MCSAPIRequestProxy.asmx/CreateGetRequest"
LONDON = ZoneInfo("Europe/London")
MAX_BYTES = 8 * 1024 * 1024
READ_ROUTES = {
    "user_details": "api/v1/mcas/user/details",
    "modules": "api/v1/mcas/configurations",
    "detentions": "api/v1/detentions/mcas/{sid}",
    "reports": "api/v1/studentDetails/reports/{sid}",
    "clubs_and_trips": "api/v1/mcas/clubsandtrips/StudentClubsAndTrips/{sid}",
}
MUTATIONS = re.compile(r"update|delete|remove|send|save|insert|mark|consent|payment", re.I)


class ReaderError(Exception):
    pass


def private_dir():
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    PRIVATE.chmod(0o700)


def save(path, value):
    """Atomic private write; temporary and final files are owner-only."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


class Page(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.hidden = {}
        self.links = []
        self.scripts = []
        self.profiles = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("type", "").lower() == "hidden" and a.get("name"):
            self.hidden[a["name"]] = a.get("value", "")
        if tag == "input" and a.get("name") == "userSelectorRadio":
            value = a.get("value", "")
            if re.fullmatch(r"\d+_\d+_\d+", value) and value not in self.profiles:
                self.profiles.append(value)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl)[:2] != urllib.parse.urlsplit(BASE)[:2]:
            raise ReaderError("MCAS redirected outside its origin; stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            SameOriginRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(self, url, data=None, json_body=False):
        if urllib.parse.urlsplit(url)[:2] != urllib.parse.urlsplit(BASE)[:2]:
            raise ReaderError("Only the MCAS HTTPS origin is allowed.")
        headers = {"User-Agent": "Personal-MCAS-ReadOnly/0.1"}
        if data is not None:
            headers["Content-Type"] = (
                "application/json;charset=utf-8" if json_body
                else "application/x-www-form-urlencoded"
            )
            data = (json.dumps(data) if json_body else urllib.parse.urlencode(data)).encode()
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=headers), timeout=30) as r:
                content = r.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ReaderError("MCAS response exceeded the size limit.")
                return r.url, content.decode("utf-8-sig", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError):
            # Do not print server responses, login forms, passwords or session cookies.
            raise ReaderError("Could not reach MCAS. Check network access and try again.") from None

    def login(self, email, password):
        _, page = self.request(LOGIN)
        form = Page(page).hidden
        if not form:
            raise ReaderError("MCAS login layout changed; authentication needs inspection.")
        form.update(EmailTextBox=email, PasswordTextBox=password)
        url, dashboard = self.request(LOGIN, form)
        self.profile_choices = []
        if "MCSContactSelect" in urllib.parse.urlsplit(url).path:
            self.selection_url = urllib.parse.urlunsplit((*urllib.parse.urlsplit(url)[:3], "", ""))
            self.profile_choices = Page(dashboard).profiles
            if not self.profile_choices:
                raise ReaderError("MCAS accepted sign-in but offered no readable pupil profiles. Profile selection needs inspection.")
            return self.select_profile(self.profile_choices[0])
        return self.dashboard_context(url, dashboard)

    def select_profile(self, choice):
        if choice not in self.profile_choices:
            raise ReaderError("Pupil selection was not offered by this MCAS session.")
        school, contact, pupil = choice.split("_")
        query = urllib.parse.urlencode({"SchoolID": school, "StudentID": pupil, "ContactID": contact})
        url, dashboard = self.request(self.selection_url + "?" + query)
        context = self.dashboard_context(url, dashboard)
        if context[0] != pupil:
            raise ReaderError("MCAS returned a different pupil from the selected profile; stopped.")
        return context

    @staticmethod
    def dashboard_context(url, dashboard):
        if "MCSDashboard" not in url:
            path = urllib.parse.urlsplit(url).path.lower()
            if any(x in path for x in ["termsandconditions", "securityquestions", "accountsettings"]):
                raise ReaderError("MCAS requires account setup. Sign in on the official MCAS website and complete its prompts, then retry.")
            raise ReaderError("MCAS sign-in did not reach the dashboard. Check your login in MCAS, including any required account setup.")
        match = re.search(r'\bmaster_studentid\s*=\s*[\"\x27]?(\d+)', dashboard)
        if not match:
            raise ReaderError("Signed in, but pupil context could not be verified.")
        return match.group(1), url, dashboard

    def read(self, name, sid):
        # Callers choose a fixed, reviewed route, never an arbitrary supplied URL.
        if name not in READ_ROUTES:
            raise ReaderError("Unapproved endpoint.")
        route = READ_ROUTES[name].format(sid=sid)
        _, body = self.request(PROXY, {"url": route, "schoolID": "", "contactID": ""}, True)
        try:
            value = json.loads(body)["d"]
            if isinstance(value, str):
                if value.startswith("Error 404"):
                    raise ReaderError("A documented read route is unavailable.")
                try:
                    value = json.loads(value)
                except ValueError:
                    raise ReaderError("Unexpected MCAS data format; review required.") from None
            if value is None:
                raise ReaderError("MCAS returned no data; coverage cannot be established.")
            return value
        except (ValueError, KeyError, TypeError):
            raise ReaderError("Unexpected MCAS response; the session or format may have changed.") from None

    def discover(self, url, dashboard):
        """Collect linked page scripts to establish inbox protocol without opening messages.

        Only list-page URLs linked by the signed-in dashboard are eligible.
        Query-string links and message-specific URLs are deliberately excluded.
        """
        pages = {url: dashboard}
        pattern = re.compile(r"/MCAS/MCS(?:Inbox|Announcements|ImportantDocuments)(?:\.aspx)?$", re.I)
        for link in Page(dashboard).links:
            target = urllib.parse.urljoin(url, link)
            parsed = urllib.parse.urlsplit(target)
            if parsed[:2] == urllib.parse.urlsplit(BASE)[:2] and not parsed.query and pattern.fullmatch(parsed.path):
                final, body = self.request(target)
                if "MCSParentLogin" in final:
                    raise ReaderError("MCAS session expired during discovery.")
                pages[final] = body
        scripts = {}
        for page_url, body in pages.items():
            for src in Page(body).scripts:
                target = urllib.parse.urljoin(page_url, src)
                parsed = urllib.parse.urlsplit(target)
                if parsed[:2] != urllib.parse.urlsplit(BASE)[:2]:
                    continue
                if not re.search(r"inbox|announcement|document|apiEndPoints", parsed.path, re.I):
                    continue
                if target not in scripts:
                    _, scripts[target] = self.request(target)
        return {"pages": pages, "scripts": scripts}

    def collect_messages(self):
        from inbox import collect
        return collect(self)

    def collect_schoolwork(self, url, dashboard):
        from schoolwork import collect
        return collect(self, url, dashboard)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def due(now, last_date, hour=7, minute=0):
    local = now.astimezone(LONDON)
    return local.date().isoformat() != last_date and (local.hour, local.minute) >= (hour, minute)


def run_once(client_factory=Client):
    private_dir()
    # Cross-process lock makes manual and scheduled runs mutually exclusive.
    with open(PRIVATE / "run.lock", "a") as lock:
        os.chmod(PRIVATE / "run.lock", 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "message": "A check is already running."}
        state = load(PRIVATE / "state.json", {})
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        credentials = load(PRIVATE / "credentials.json")
        issue = None
        if not credentials:
            issue = "Local MCAS sign-in is required."
            status = "needs_login"
        else:
            try:
                client = client_factory()
                sid, url, dashboard = client.login(credentials["email"], credentials["password"])
                snapshot = {}
                gaps = []
                choices = getattr(client, "profile_choices", [])
                contexts = []
                message_records = []
                discoveries = {}
                for index, choice in enumerate(choices or [None]):
                    if index:
                        sid, url, dashboard = client.select_profile(choice)
                    profile_data = {}
                    context_key = choice or sid
                    contexts.append(context_key)
                    for name in READ_ROUTES:
                        try:
                            profile_data[name] = client.read(name, sid)
                        except ReaderError as err:
                            gaps.append({"profile": context_key, "source": name, "error": str(err)})
                    try:
                        profile_data['schoolwork'] = client.collect_schoolwork(url, dashboard)
                        gaps.extend({'profile': context_key, **gap} for gap in profile_data['schoolwork']['errors'])
                    except ReaderError as err:
                        gaps.append({'profile': context_key, 'source': 'schoolwork', 'error': str(err)})
                    try:
                        messages = client.collect_messages()
                        profile_data['school_messages'] = messages
                        message_records.append(messages)
                        gaps.extend({'profile': context_key, **gap} for gap in messages['attachment_errors'])
                    except ReaderError as err:
                        gaps.append({'profile': context_key, 'source': 'school_messages', 'error': str(err)})
                    if len(choices) > 1:
                        snapshot[context_key] = profile_data
                    else:
                        snapshot = profile_data
                    discoveries[context_key] = client.discover(url, dashboard)
                discovery = {"profiles": discoveries}
                save(PRIVATE / "discovery.json", discovery)
                previous = load(PRIVATE / "latest.json", {})
                changed = digest(snapshot) != digest(previous.get("data", {}))
                bodies_fetched = len(message_records) == len(contexts) and all(r['bodies_fetched'] for r in message_records)
                files_fetched = bodies_fetched and all(r['attachments_downloaded'] for r in message_records)
                evidence = {"fetched_at": now, "profile_contexts": contexts, "data": snapshot,
                            "gaps": gaps, "messages_complete": False, "message_bodies_fetched": bodies_fetched,
                            "attachments_downloaded": files_fetched, "attachments_complete": False, "calendar_writes": False}
                save(PRIVATE / "latest.json", evidence)
                if changed:
                    stamp = now.replace(":", "-")
                    save(PRIVATE / "snapshots" / f"{stamp}.json", evidence)
                state["data_changed"] = changed
                state["last_authenticated_fetch"] = now
                state["profiles_collected"] = len(contexts)
                state['message_bodies_fetched'] = bodies_fetched
                state['attachments_downloaded'] = files_fetched
                state['message_count'] = sum(len(c['Messages']) for r in message_records for c in r['conversations'])
                state['announcement_count'] = sum(len(r['announcements']) for r in message_records)
                state['attachment_count'] = sum(len(r['attachments']) for r in message_records)
                status = "ready_for_review" if bodies_fetched and files_fetched and not gaps else "partial"
                issue = ("School messages, announcements and attachments retrieved. Read status verified unchanged. Content review and historical coverage checks remain outstanding; calendar writes are disabled."
                         if status == 'ready_for_review' else "Collection is incomplete. Some school data or attachments could not be retrieved. Calendar writes remain disabled.")
            except ReaderError as err:
                status, issue = "error", str(err)
        issue_changed = issue != state.get("issue")
        state.update(status=status, issue=issue, last_attempt=now,
                     last_attempt_date=dt.datetime.fromisoformat(now).astimezone(LONDON).date().isoformat(),
                     messages_complete=False, calendar_writes=False,
                     attention_changed=issue_changed or (status in {"partial", "ready_for_review"} and state.get("data_changed", False)))
        save(PRIVATE / "state.json", state)
        return state


def configure():
    private_dir()
    email = input("MCAS parent email: ").strip()
    password = getpass.getpass("MCAS password (hidden): ")
    if not email or not password:
        raise ReaderError("Email and password are required.")
    save(PRIVATE / "credentials.json", {"email": email, "password": password})
    print("Sign-in saved locally with owner-only permissions. Run 'python3 mcas.py run' to test.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "run", "status", "serve"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    args = parser.parse_args()
    if args.command == "setup":
        configure()
    elif args.command == "run":
        result = run_once()
        print(json.dumps({k: result.get(k) for k in ["status", "issue", "last_attempt"]}, indent=2))
    elif args.command == "status":
        print(json.dumps(load(PRIVATE / "state.json", {"status": "not_tested"}), indent=2))
    else:
        from server import serve
        serve(args.port, args.host, args.tls_cert, args.tls_key)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except ReaderError as err:
        raise SystemExit(str(err)) from None
