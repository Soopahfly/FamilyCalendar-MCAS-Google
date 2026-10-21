"""Loopback control page and daily London-time scheduler."""
import datetime as dt
import os
import hmac
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import ssl
import ipaddress
import threading
import time
import urllib.parse

import mcas
import google_calendar
import review_control


def settings():
    return mcas.load(mcas.PRIVATE / "settings.json", {"enabled": False, "time": "07:00"})


def scheduler(stop):
    while not stop.wait(30):
        try:
            config = settings()
            state = mcas.load(mcas.PRIVATE / "state.json", {})
            hour, minute = map(int, config["time"].split(":"))
            if config["enabled"] and mcas.due(dt.datetime.now(dt.timezone.utc), state.get("last_attempt_date"), hour, minute):
                mcas.run_once()
            review_control.tick()
        except Exception:
            mcas.save(mcas.PRIVATE / 'scheduler-error.json', {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'message': 'The server scheduler encountered an error. Inspect the reader and assistant run status before relying on the next update.'})


def make_handler(port, token, host="127.0.0.1", scheme="http"):
    hosts = {f"{host}:{port}"}
    if host == "127.0.0.1":
        hosts.add(f"localhost:{port}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log requests, credentials or session tokens.

        def headers_ok(self):
            return self.headers.get("Host") in hosts

        def respond(self, status, body):
            encoded = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if not self.headers_ok():
                return self.respond(403, "Use the loopback address.")
            if self.path != "/":
                return self.respond(404, "Not found")
            state = mcas.load(mcas.PRIVATE / "state.json", {})
            config = settings()
            connected = (mcas.PRIVATE / "credentials.json").exists()
            last_attempt = state.get('last_attempt', 'Never')
            if state.get('last_attempt'):
                last_attempt = dt.datetime.fromisoformat(state['last_attempt']).astimezone(mcas.LONDON).strftime('%d %B %Y, %H:%M %Z')
            status_label = state.get('status', 'Not tested').replace('_', ' ').capitalize()
            escape = lambda v: html.escape(str(v), quote=True)
            review = mcas.load(mcas.PRIVATE / 'review.json', {})
            latest = mcas.load(mcas.PRIVATE / 'latest.json', {})
            review_html = ''
            if review:
                fresh = review.get('data_digest') == mcas.digest(latest.get('data', {}))
                review_html = '<section><h2>School review findings</h2><p>' + escape(review.get('status', '')) + '</p>'
                if not fresh:
                    review_html += '<p><strong>New evidence has arrived since this review; findings below may need updating.</strong></p>'
                review_html += '<p>These are saved school findings. See Family assistant status for the latest verified calendar changes.</p>'
                for finding in review.get('findings', []):
                    review_html += '<h3>' + escape(finding['title']) + '</h3><p>' + escape(finding['detail']) + '</p>'
                review_html += '</section>'
            hidden = f'<input type="hidden" name="csrf" value="{token}">'
            assistant = review_control.status()
            review_settings = review_control.settings()
            assistant_html = '<section><h2>Family assistant — email, calendar and school</h2><p>' + escape(assistant['message']) + '</p>'
            assistant_html += '<p>Status: ' + escape(assistant['status'].replace('_', ' ')) + '</p>'
            if review_settings['enabled'] and not review_settings.get('validated', False):
                assistant_html += '<p><strong>Automatic reviews are waiting for the first full review to pass its live checks.</strong></p>'
            if assistant.get('requested_at'):
                assistant_html += '<p>Last requested: ' + escape(dt.datetime.fromisoformat(assistant['requested_at']).astimezone(mcas.LONDON).strftime('%d %b %Y, %H:%M %Z')) + '</p>'
            assistant_html += f'<form action="/assistant/run" method="post">{hidden}<button>Run full assistant review</button></form>'
            assistant_html += '<p>This queues the review into your existing ChatGPT-backed chat on your server. It uses that chat’s connected Gmail/Calendar tools and saved instructions; it does not send messages to other people. Queue acceptance is not proof that the review has finished.</p>'
            assistant_html += f'''<form action="/assistant/schedule" method="post">{hidden}
<label><input type="checkbox" name="enabled" {'checked' if review_settings['enabled'] else ''}> Enable automatic full reviews</label>
<label>Daily review time <input type="time" name="daily_time" value="{escape(review_settings['daily_time'])}" required></label>
<label>Wednesday/Sunday refresh time <input type="time" name="refresh_time" value="{escape(review_settings['refresh_time'])}" required></label>
<button>Save review schedule</button></form>
<p>Times follow Europe/London. Review appointments remain 18:00 with a 17:45 alert. Start refreshes early enough to finish before alerts. Inspect results and questions in this chat; keep the server’s Codex service signed in and available. ChatGPT usage limits and unattended permissions still apply.</p></section>'''
            google = google_calendar.status()
            google_ready = google_calendar.path('client').exists()
            google_html = '<section><h2>Google Calendar connection</h2><p>' + escape(google['message']) + '</p>'
            google_html += '<p>This server uses a separate Google connection from this chat. Automatic calendar changes remain off.</p>'
            google_html += f'''<details><summary>One-time Google setup (no domain needed)</summary>
<ol><li>Open <a href="https://console.cloud.google.com/" target="_blank" rel="noreferrer">Google Cloud Console</a> and create a project named Personal calendar assistant.</li>
<li>Enable <strong>Google Calendar API</strong> in APIs &amp; Services.</li>
<li>In Google Auth Platform, configure the app for your own use and add your Google account as a test user if the app is in Testing.</li>
<li>Create an OAuth client of type <strong>Web application</strong>. Add this exact authorised redirect URI: <code>{google_calendar.REDIRECT}</code>. Download its JSON file.</li>
<li>Paste that JSON below on this private page. Do not paste it into this chat.</li>
<li>On your computer, open a terminal and keep this one-time connection running:<br><code>ssh -L 8767:127.0.0.1:8767 USER@SERVER</code></li>
<li>Choose <strong>Connect Google and test</strong>. Sign in with your configured Google account and grant the requested access. Google returns through the local connection; you can close the terminal after the success page.</li></ol>
<p>While a Google app is in Testing, its ongoing-access grant may expire after seven days. This setup is suitable for initial tests; we must address the publishing state before relying on unattended daily runs.</p></details>
<form action="/google/configure" method="post">{hidden}<label>Google OAuth client JSON (stored privately)<br><textarea name="client_json" rows="5" cols="45" required autocomplete="off" spellcheck="false"></textarea></label><button>Save Google app setup</button></form>'''
            if google_ready:
                google_html += f'<form action="/google/start" method="post">{hidden}<button>Connect Google and test</button></form>'
            if google_calendar.path('tokens').exists():
                google_html += f'<form action="/google/test" method="post">{hidden}<button>Recheck calendar connection</button></form>'
            google_html += '<small>Access requested: events on calendars you own, calendar list and Google account email. Software writes are restricted to an immediately removed connection-test event on Family. No attendee invitations or RSVPs are sent. Gmail is not connected to this server yet.</small></section>'
            schoolwork_html = ''
            profiles = latest.get('data', {})
            profile_values = profiles.values() if latest.get('profile_contexts') and len(latest['profile_contexts']) > 1 else [profiles]
            def display_date(value, show_time=False):
                if not value:
                    return 'Not supplied'
                parsed = dt.datetime.fromisoformat(value)
                if parsed.tzinfo:
                    parsed = parsed.astimezone(mcas.LONDON)
                return parsed.strftime('%d %b %Y, %H:%M' if show_time else '%d %b %Y')
            for profile in profile_values:
                sw = profile.get('schoolwork')
                if not sw:
                    continue
                schoolwork_html += '<section><h2>School calendar and homework — ' + escape(sw['pupil']) + '</h2>'
                if sw.get('school'):
                    schoolwork_html += '<p>' + escape(sw['school']) + '</p>'
                schoolwork_html += '<h3>Holiday and staff-only dates</h3><p>Normal days are omitted. Dates below are inclusive; only labels supplied by the school are used. Some schools label breaks “Not in Term”; those are outside this exact Holiday/Staff Only filter.</p>'
                if not sw['calendar_available']:
                    schoolwork_html += '<p>School calendar is not available in this profile menu.</p>'
                elif any(e['source'] == 'calendar' for e in sw['errors']):
                    schoolwork_html += '<p>School calendar could not be read. This does not mean there are no holidays.</p>'
                elif sw['calendar_ranges']:
                    schoolwork_html += '<ul>'
                    for row in sw['calendar_ranges']:
                        label = display_date(row['start'])
                        if row['end'] != row['start']:
                            label += ' – ' + display_date(row['end'])
                        schoolwork_html += '<li>' + escape(label + ': ' + row['label']) + '</li>'
                    schoolwork_html += '</ul>'
                else:
                    schoolwork_html += '<p>No upcoming holiday/staff-only labels were returned in the collected calendar.</p>'
                highlight_homework = sw['pupil'].casefold() == os.environ.get('PURPLE_HOMEWORK_PUPIL', '').casefold()
                schoolwork_html += '<div class="purple-homework">' if highlight_homework else '<div>'
                schoolwork_html += '<h3>Homework: when set and when due</h3>'
                if not sw['homework_available']:
                    schoolwork_html += '<p>Homework is not available in this profile menu; that is not confirmation there is no homework.</p>'
                elif any(e['source'] == 'homework' for e in sw['errors']):
                    schoolwork_html += '<p>Homework could not be read. Check MCAS directly.</p>'
                else:
                    schoolwork_html += '<p>Dates/times are as MCAS supplies them, interpreted in London time. Submission status is unknown; a blank submission field does not prove unfinished work.</p><div style="overflow-x:auto"><table><thead><tr><th>Subject / task</th><th>Set</th><th>Due</th></tr></thead><tbody>'
                    for item in sw['homework']:
                        schoolwork_html += '<tr><td>' + escape((item.get('subject') or '') + ' — ' + item['title']) + '</td><td>' + escape(display_date(item['set_at'])) + '</td><td>' + escape(display_date(item['due_at'], True)) + '</td></tr>'
                    schoolwork_html += '</tbody></table></div>'
                    for item in sw['homework']:
                        schoolwork_html += '<details><summary>' + escape(item['title']) + '</summary><p>' + escape(item['instructions']) + '</p></details>'
                    schoolwork_html += '<small>' + escape(sw.get('homework_coverage', '')) + '</small>'
                schoolwork_html += '</div></section>'
            self.respond(200, f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>School reader</title>
<style>body{{font:18px system-ui;max-width:740px;margin:3em auto;padding:0 1em;background:#fafafa;color:#222}}section{{background:white;padding:1.2em;margin:1em 0;border:1px solid #ddd;border-radius:12px}}button,input{{font:inherit;padding:.55em;margin:.3em 0}}label{{display:block}}small{{display:block;color:#555}}.purple-homework{{background:#f4e9ff;border-left:5px solid #8b46b8;border-radius:8px;padding:1em;margin-top:1em}}.purple-homework h3,.purple-homework th,.purple-homework summary{{color:#652b8c}}th,td{{padding:.45em;text-align:left;vertical-align:top}}.purple-homework td{{border-top:1px solid #dbadff}}</style>
<h1>Family assistant and school reader</h1><p>MCAS collection is read-only · Calendar maintenance runs through your assistant chat · Europe/London</p>
{assistant_html}
<section><h2>Review status</h2><p>{escape(status_label)}</p>
<p>{escape(state.get('issue', 'Connect MCAS, then run a live test.'))}</p>
<p>Last attempt: {escape(last_attempt)}</p>
<p>Message bodies: {'retrieved' if state.get('message_bodies_fetched') else 'not yet verified'} · Attachments: {'downloaded' if state.get('attachments_downloaded') else 'not yet verified'}</p>
<p>{escape(state.get('message_count', 0))} messages · {escape(state.get('announcement_count', 0))} announcements · {escape(state.get('attachment_count', 0))} attachments</p>
<p><strong>Content review and historical coverage are not complete.</strong> This collection step does not write calendars; use the full assistant review above.</p>
<form action="/run" method="post">{hidden}<button>Refresh MCAS only</button></form><p>Refresh this page to see the result. Use “Run full assistant review” above for email/calendar review.</p></section>
{review_html}
{schoolwork_html}
{google_html}
<section><h2>MCAS sign-in</h2><p>{'Sign-in saved on this server.' if connected else 'No sign-in saved.'}</p>
<form action="/connect" method="post">{hidden}
<label>Parent email <input name="email" type="email" required autocomplete="username"></label>
<label>Password <input name="password" type="password" required autocomplete="current-password"></label>
<button>Save sign-in and test</button></form>
<small>Your password stays in a private file on this server. It is never displayed here or sent to this chat.</small></section>
<section><h2>Daily check</h2><p>{'Enabled' if config['enabled'] else 'Paused'} · {escape(config['time'])} London time</p>
<p>Once per day, with daylight-saving changes handled automatically. This reader must remain running. Results are saved here; chat and phone notifications are not connected.</p>
<form action="/schedule" method="post">{hidden}<label>Time <input type="time" name="time" value="{escape(config['time'])}" required></label>
<label><input type="checkbox" name="enabled" {'checked' if config['enabled'] else ''}> Enable daily checks</label>
<button>Save schedule</button></form></section></html>''')

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.headers_ok() or (origin and origin not in {f"{scheme}://{host}" for host in hosts}):
                return self.respond(403, "Request origin rejected.")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                return self.respond(415, "Unsupported form type.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError()
                data = urllib.parse.parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
            except (ValueError, UnicodeError):
                return self.respond(400, "Invalid form.")
            value = lambda key: data.get(key, [""])[0]
            if not hmac.compare_digest(value("csrf"), token):
                return self.respond(403, "Refresh the page and try again.")
            if self.path == '/assistant/run':
                threading.Thread(target=review_control.queue_review, daemon=True).start()
                self.send_response(303)
                self.send_header('Location', '/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/assistant/schedule':
                times = {}
                try:
                    for name in ['daily_time', 'refresh_time']:
                        parsed_time = dt.time.fromisoformat(value(name))
                        if len(value(name)) != 5:
                            raise ValueError()
                        times[name] = parsed_time.strftime('%H:%M')
                except ValueError:
                    return self.respond(400, 'Choose valid review times.')
                current = review_control.settings()
                mcas.save(mcas.PRIVATE / 'review-settings.json', {**current, 'enabled': value('enabled') == 'on', **times})
                self.send_response(303)
                self.send_header('Location', '/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path.startswith('/google/'):
                try:
                    if self.path == '/google/configure':
                        google_calendar.configure(value('client_json'))
                    elif self.path == '/google/start':
                        location = google_calendar.begin()
                        self.send_response(303)
                        self.send_header('Location', location)
                        self.send_header('Content-Length', '0')
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        return
                    elif self.path == '/google/test':
                        google_calendar.test_connection()
                    else:
                        return self.respond(404, 'Not found')
                except mcas.ReaderError as err:
                    return self.respond(400, '<p>' + html.escape(str(err)) + '</p><p><a href="/">Return to setup</a></p>')
                self.send_response(303)
                self.send_header('Location', '/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == "/connect":
                if not value("email").strip() or not value("password"):
                    return self.respond(400, "Email and password are required.")
                mcas.save(mcas.PRIVATE / "credentials.json", {"email": value("email").strip(), "password": value("password")})
                threading.Thread(target=mcas.run_once, daemon=True).start()
            elif self.path == "/run":
                threading.Thread(target=mcas.run_once, daemon=True).start()
            elif self.path == "/schedule":
                try:
                    parsed = dt.time.fromisoformat(value("time"))
                    if len(value("time")) != 5:
                        raise ValueError()
                except ValueError:
                    return self.respond(400, "Choose a valid daily time.")
                mcas.save(mcas.PRIVATE / "settings.json", {"enabled": value("enabled") == "on", "time": parsed.strftime("%H:%M")})
            else:
                return self.respond(404, "Not found")
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


class GoogleCallback(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if self.headers.get('Host') not in {'localhost:8767', '127.0.0.1:8767'} or parsed.path != '/google/callback' or len(self.path) > 16384:
            self.send_error(404)
            return
        try:
            query = urllib.parse.parse_qs(parsed.query)
            google_calendar.complete(query)
            google_calendar.test_connection()
            message = 'Google Calendar connected and tested. The temporary event was removed. You can close the SSH connection and return to the school-reader page.'
            status = 200
        except mcas.ReaderError as err:
            message = str(err)
            status = 400
        body = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Google connection</title><p>' + html.escape(message) + '</p><p>Automatic calendar changes remain off.</p></html>').encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


def serve(port=8765, host="127.0.0.1", tls_cert=None, tls_key=None):
    mcas.private_dir()
    if host != "127.0.0.1":
        if not ipaddress.ip_address(host).is_private or host == "0.0.0.0":
            raise mcas.ReaderError("Choose a specific private network address.")
        if not tls_cert or not tls_key:
            raise mcas.ReaderError("Network access requires HTTPS certificate and key.")
    scheme = "https" if tls_cert and tls_key else "http"
    server = ThreadingHTTPServer((host, port), make_handler(port, secrets.token_urlsafe(32), host, scheme))
    if scheme == "https":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(tls_cert, tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    stop = threading.Event()
    worker = threading.Thread(target=scheduler, args=(stop,), daemon=True)
    worker.start()
    callback = ThreadingHTTPServer(('127.0.0.1', 8767), GoogleCallback)
    callback_worker = threading.Thread(target=callback.serve_forever, daemon=True)
    callback_worker.start()
    print(f"School reader: {scheme}://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        callback.shutdown()
        callback.server_close()
        server.server_close()
