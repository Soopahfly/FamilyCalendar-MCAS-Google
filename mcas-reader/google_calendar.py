"""Private Google OAuth connection and constrained calendar connection test.

Google's documented web-server flow, with a localhost callback for one-time SSH
forwarding. No general event writer or automatic commitment execution is enabled.
"""
import datetime as dt
import os
import hashlib
import base64
import hmac
import ipaddress
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import mcas

FAMILY = os.environ.get('FAMILY_CALENDAR_ID', '')
REDIRECT = 'http://localhost:8767/google/callback'
SCOPES = ['https://www.googleapis.com/auth/calendar.events.owned',
          'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
          'https://www.googleapis.com/auth/userinfo.email']
ALLOWED_ACCOUNTS = {x.strip().lower() for x in os.environ.get('GOOGLE_ALLOWED_ACCOUNTS', '').split(',') if x.strip()}
LOCK = threading.RLock()


def path(name):
    return mcas.PRIVATE / ('google-' + name + '.json')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise mcas.ReaderError('Google request unexpectedly redirected; stopped.')


def http(url, method='GET', data=None, access_token=None, form=False, etag=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'oauth2.googleapis.com', 'www.googleapis.com'}:
        raise mcas.ReaderError('Google connection URL is not allowed.')
    headers = {'Accept': 'application/json'}
    if access_token:
        headers['Authorization'] = 'Bearer ' + access_token
    if etag:
        headers['If-Match'] = etag
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        data = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(urllib.request.Request(url, method=method, data=data, headers=headers), timeout=30) as response:
            raw = response.read(2 * 1024 * 1024)
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        # Avoid server response bodies: they may include sensitive request values.
        if err.code in {404, 410}:
            return err.code, {}
        if err.code in {400, 401, 403}:
            raise mcas.ReaderError('Google rejected this request. Check app setup, granted permissions or reconnect Google.') from None
        raise mcas.ReaderError('Google request failed; the saved operation record must be checked before retrying writes.') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise mcas.ReaderError('Could not complete the Google request. Check network access and the saved operation status.') from None


def configure(raw):
    try:
        parsed = json.loads(raw)
        config = parsed['web']
        if not str(config['client_id']).endswith('.apps.googleusercontent.com') or not config['client_secret']:
            raise ValueError()
        if REDIRECT not in config.get('redirect_uris', []):
            raise ValueError()
        selected = {k: config[k] for k in ['client_id', 'client_secret']}
        selected['redirect_uri'] = REDIRECT
    except (ValueError, KeyError, TypeError):
        raise mcas.ReaderError('Choose Google Web application credentials with this exact authorised redirect: ' + REDIRECT) from None
    with LOCK:
        mcas.save(path('client'), selected)
        # A changed client invalidates old grants and pending browser state.
        path('tokens').unlink(missing_ok=True)
        path('pending').unlink(missing_ok=True)
        mcas.save(path('status'), {'status': 'needs_consent', 'message': 'Google app configured. Connect Google next.', 'automatic_writes': False})


def begin():
    if not FAMILY or not ALLOWED_ACCOUNTS:
        raise mcas.ReaderError('Configure FAMILY_CALENDAR_ID and GOOGLE_ALLOWED_ACCOUNTS before connecting Google.')
    with LOCK:
        config = mcas.load(path('client'))
        if not config:
            raise mcas.ReaderError('Add Google app credentials first.')
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        mcas.save(path('pending'), {'state': state, 'verifier': verifier, 'created': time.time()})
        return 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
            'client_id': config['client_id'], 'redirect_uri': REDIRECT,
            'response_type': 'code', 'scope': ' '.join(SCOPES), 'access_type': 'offline',
            'prompt': 'consent', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
            'login_hint': next(iter(sorted(ALLOWED_ACCOUNTS)), ''),
        })


def complete(query):
    with LOCK:
        pending = mcas.load(path('pending'))
        if not pending or time.time() - pending['created'] > 600 or not hmac.compare_digest(query.get('state', [''])[0], pending['state']):
            raise mcas.ReaderError('Google sign-in expired or did not match. Start Connect Google again.')
        path('pending').unlink(missing_ok=True)
        if query.get('error') or not query.get('code'):
            raise mcas.ReaderError('Google access was not granted. You can reconnect when ready.')
        config = mcas.load(path('client'))
        _, tokens = http('https://oauth2.googleapis.com/token', 'POST', {
            'client_id': config['client_id'], 'client_secret': config['client_secret'],
            'code': query['code'][0], 'redirect_uri': REDIRECT, 'grant_type': 'authorization_code',
            'code_verifier': pending['verifier'],
        }, form=True)
        if not tokens.get('access_token') or not tokens.get('refresh_token'):
            raise mcas.ReaderError('Google did not grant ongoing access. Reconnect and grant the requested permissions.')
        granted = set(tokens.get('scope', '').split())
        if not set(SCOPES).issubset(granted):
            raise mcas.ReaderError('Required Google permissions were not all granted. Nothing was connected.')
        _, user = http('https://www.googleapis.com/oauth2/v2/userinfo', access_token=tokens['access_token'])
        email = user.get('email', '').lower()
        if email not in ALLOWED_ACCOUNTS or user.get('verified_email') is not True:
            raise mcas.ReaderError('Sign in with the configured Family calendar Google account. This account was not saved.')
        tokens['expires_at'] = time.time() + int(tokens.get('expires_in', 3600))
        tokens['account'] = email
        mcas.save(path('tokens'), tokens)
        try:
            verify_destination()
        except mcas.ReaderError:
            path('tokens').unlink(missing_ok=True)
            raise
        mcas.save(path('status'), {'status': 'connected_needs_test', 'account': email,
                                  'message': 'Google connected to Family. Run the temporary connection test.', 'automatic_writes': False})


def access_token():
    with LOCK:
        tokens = mcas.load(path('tokens'))
        if not tokens:
            raise mcas.ReaderError('Connect Google first.')
        if tokens.get('expires_at', 0) <= time.time() + 60:
            config = mcas.load(path('client'))
            _, refreshed = http('https://oauth2.googleapis.com/token', 'POST', {
                'client_id': config['client_id'], 'client_secret': config['client_secret'],
                'refresh_token': tokens['refresh_token'], 'grant_type': 'refresh_token',
            }, form=True)
            if not refreshed.get('access_token'):
                raise mcas.ReaderError('Google access expired. Reconnect Google.')
            tokens.update(refreshed)
            tokens['expires_at'] = time.time() + int(refreshed.get('expires_in', 3600))
            mcas.save(path('tokens'), tokens)
        return tokens['access_token']


def api(suffix, method='GET', data=None, etag=None):
    return http('https://www.googleapis.com/calendar/v3/' + suffix, method, data, access_token(), etag=etag)


def verify_destination():
    if not FAMILY:
        raise mcas.ReaderError('Configure FAMILY_CALENDAR_ID first.')
    code, calendar = api('users/me/calendarList/' + urllib.parse.quote(FAMILY, safe=''))
    if code != 200 or calendar.get('id') != FAMILY or calendar.get('accessRole') != 'owner':
        raise mcas.ReaderError('The intended Family calendar was not found with owner access. No calendar changes made.')
    return calendar


def event_path(event_id=''):
    root = 'calendars/' + urllib.parse.quote(FAMILY, safe='') + '/events'
    return root + ('/' + urllib.parse.quote(event_id, safe='') if event_id else '')


def cleanup_test(operation):
    code, event = api(event_path(operation['event_id']))
    if code in {404, 410} or event.get('status') == 'cancelled':
        operation['removed_verified'] = True
        return
    if event.get('extendedProperties', {}).get('private', {}).get('schoolReaderConnectionTest') != operation['event_id']:
        raise mcas.ReaderError('Test event ownership marker differs. Stopped without deleting it.')
    api(event_path(operation['event_id']) + '?sendUpdates=none', 'DELETE', etag=event.get('etag'))
    code, event = api(event_path(operation['event_id']))
    if code not in {404, 410} and event.get('status') != 'cancelled':
        raise mcas.ReaderError('Test removal could not be verified. Retry the test cleanup.')
    operation['removed_verified'] = True


def test_connection():
    with LOCK:
        verify_destination()
        previous = mcas.load(path('test'))
        if previous and not previous.get('removed_verified'):
            cleanup_test(previous)
            mcas.save(path('test'), previous)
            if not previous.get('event_verified'):
                raise mcas.ReaderError('Previous test cleaned up. Run a fresh connection test.')
        event_id = secrets.token_hex(16)
        start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(second=0, microsecond=0)
        end = start + dt.timedelta(minutes=5)
        payload = {'id': event_id, 'summary': 'Temporary school-reader connection test — will be removed',
                   'description': 'Connection verification only. This entry is deleted immediately after checking its saved details.',
                   'start': {'dateTime': start.isoformat(), 'timeZone': 'Europe/London'},
                   'end': {'dateTime': end.isoformat(), 'timeZone': 'Europe/London'},
                   'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 15}]},
                   'attendees': [], 'transparency': 'transparent', 'visibility': 'private',
                   'extendedProperties': {'private': {'schoolReaderConnectionTest': event_id}}}
        operation = {'event_id': event_id, 'calendar_id': FAMILY, 'event_verified': False,
                     'removed_verified': False, 'started': dt.datetime.now(dt.timezone.utc).isoformat()}
        mcas.save(path('test'), operation)
        failure = None
        try:
            api(event_path() + '?sendUpdates=none&conferenceDataVersion=0', 'POST', payload)
            code, saved = api(event_path(event_id))
            if code != 200 or saved.get('id') != event_id or saved.get('summary') != payload['summary']:
                raise mcas.ReaderError('Saved test event details did not match.')
            for field in ['start', 'end']:
                actual = dt.datetime.fromisoformat(saved[field]['dateTime'])
                expected = dt.datetime.fromisoformat(payload[field]['dateTime'])
                if actual != expected or saved[field].get('timeZone') != 'Europe/London':
                    raise mcas.ReaderError('Saved test time or timezone did not match.')
            if saved.get('reminders') != payload['reminders'] or saved.get('attendees') or saved.get('conferenceData'):
                raise mcas.ReaderError('Saved test reminders or participant details did not match.')
            operation['event_verified'] = True
        except mcas.ReaderError as err:
            failure = err
        finally:
            try:
                cleanup_test(operation)
            finally:
                mcas.save(path('test'), operation)
        if failure:
            raise failure
        mcas.save(path('status'), {'status': 'connection_test_passed', 'account': mcas.load(path('tokens'))['account'],
                                  'message': 'Family connection tested: event, time zone and explicit alarm verified; temporary event removed and removal verified. Automatic calendar changes are not enabled.',
                                  'automatic_writes': False, 'tested_at': dt.datetime.now(dt.timezone.utc).isoformat()})


def status():
    return mcas.load(path('status'), {'status': 'setup_required', 'message': 'Google app setup is required before connecting this server to Calendar.', 'automatic_writes': False})
