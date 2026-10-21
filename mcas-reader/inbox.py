"""MCAS inbox/announcement reads, using the portal's observed request shapes.

No Javascript execution, mark-read, delete, message-send or acknowledgement calls.
The portal returns entire lists (no page arguments in its current client).
"""
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import mcas

POST_PROXY = mcas.BASE + '/MCAS/WebServices/MCSAPIRequestProxy.asmx/CreatePostRequest'
GET_ALLOWED = re.compile(r'api/v1/person/\d+/(?:conversations/true|messages/\d+/attachments/\d+)$')
POST_ALLOWED = re.compile(r'api/v1/mcas/announcements/(?:get/\d+|getFile/)$')


def proxy(client, route, data=None):
    if data is None:
        if not GET_ALLOWED.fullmatch(route):
            raise mcas.ReaderError('Unapproved inbox read route.')
        endpoint = mcas.PROXY
        payload = {'url': route, 'schoolID': '', 'contactID': ''}
    else:
        if not POST_ALLOWED.fullmatch(route):
            raise mcas.ReaderError('Unapproved announcement read route.')
        endpoint = POST_PROXY
        payload = {'url': route, 'jsonDataString': json.dumps(data)}
    _, body = client.request(endpoint, payload, True)
    try:
        value = json.loads(body)['d']
        if isinstance(value, str):
            if value.startswith('Error 404'):
                raise mcas.ReaderError('MCAS read route is unavailable.')
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value
    except (ValueError, TypeError, KeyError):
        raise mcas.ReaderError('MCAS inbox returned an unexpected response.') from None


def numeric(value):
    value = str(value)
    if not re.fullmatch(r'\d+', value):
        raise mcas.ReaderError('Unexpected MCAS source identifier.')
    return value


def unwrap_list(data, key):
    if isinstance(data, dict) and key in {'MessageAttachments', 'Table1'} and key in data and data[key] is None:
        data = {**data, key: []}
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise mcas.ReaderError('MCAS message format changed; coverage is incomplete.')
    # Never discard a newly introduced pagination marker.
    for k, v in data.items():
        if re.search(r'next|hasmore|continuation|pagecount', k, re.I) and v:
            raise mcas.ReaderError('MCAS now returns pagination; adapter update required.')
    return data[key]


def read_flags(conversations, announcements):
    flags = {}
    for conversation in conversations:
        for message in conversation.get('Messages', []):
            flags['message:' + numeric(message['MessageID'])] = message.get('IsRead')
    for announcement in announcements:
        flags['announcement:' + numeric(announcement['AnnouncementID'])] = announcement.get('IsRead')
    return flags


def attachment(client, route, metadata, data=None):
    value = proxy(client, route, data)
    if not isinstance(value, str):
        raise mcas.ReaderError('MCAS attachment is not a base64 file.')
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise mcas.ReaderError('MCAS attachment encoding is invalid.') from None
    if not content or len(content) > mcas.MAX_BYTES:
        raise mcas.ReaderError('MCAS attachment is empty or too large.')
    sha = hashlib.sha256(content).hexdigest()
    suffix = Path(str(metadata.get('FileName', ''))).suffix.lower()
    suffix = suffix if re.fullmatch(r'\.[a-z0-9]{1,8}', suffix) else '.bin'
    folder = mcas.PRIVATE / 'attachments'
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (sha + suffix)
    if not path.exists():
        with open(path, 'xb') as stream:
            os.chmod(path, 0o600)
            stream.write(content)
    result = {'source': metadata, 'sha256': sha, 'path': str(path.relative_to(mcas.PRIVATE)),
              'bytes': len(content), 'text_complete': False}
    if suffix == '.pdf' and content.startswith(b'%PDF-'):
        executable = shutil.which('pdftotext')
        env = os.environ.copy()
        bundled = Path('/tmp/email-calendar-pdf-tools/usr/bin/pdftotext')
        if not executable and bundled.exists():
            executable = str(bundled)
            env['LD_LIBRARY_PATH'] = '/tmp/email-calendar-pdf-tools/usr/lib/x86_64-linux-gnu'
        if executable:
            try:
                completed = subprocess.run([executable, '-layout', str(path), '-'],
                                           capture_output=True, timeout=30, env=env)
                text = completed.stdout.decode('utf-8', errors='replace')
                if completed.returncode == 0 and text.strip():
                    result['extracted_text'] = text
                    # Images/diagrams still require a visual review; extraction isn't proof of completeness.
                    result['text_status'] = 'text_extracted_visual_review_not_verified'
                else:
                    result['text_status'] = 'visual_review_required'
            except (OSError, subprocess.TimeoutExpired):
                result['text_status'] = 'extraction_failed'
        else:
            result['text_status'] = 'pdf_reader_unavailable'
    else:
        result['text_status'] = 'manual_review_required'
    return result


def collect(client):
    _, page = client.request(mcas.BASE + '/MCAS/MCSInbox')
    match = re.search(r'\bvar\s+ID\s*=\s*[\"\x27]?(\d+)', page)
    if not match:
        raise mcas.ReaderError('Inbox pupil/contact context unavailable.')
    person = match.group(1)
    route = 'api/v1/person/' + person + '/conversations/true'
    inbox = proxy(client, route)
    conversations = unwrap_list(inbox, 'Conversations')
    attachments = unwrap_list(inbox, 'MessageAttachments')
    for conversation in conversations:
        if not isinstance(conversation.get('Messages'), list):
            raise mcas.ReaderError('Conversation bodies are missing.')
        for message in conversation['Messages']:
            if not all(k in message for k in ['MessageID', 'Message', 'MessageDate', 'IsRead']):
                raise mcas.ReaderError('Full message evidence is missing.')
    _, announcements_page = client.request(mcas.BASE + '/MCAS/MCSAnnouncements')
    variables = {}
    for key in ['master_userid', 'master_studentid']:
        found = re.search(r'\b' + key + r'\s*=\s*[\"\x27]?(\d+)', announcements_page)
        if not found:
            raise mcas.ReaderError('Announcement context unavailable.')
        variables[key] = found.group(1)
    announcement_route = 'api/v1/mcas/announcements/get/' + variables['master_userid']
    notice_data = proxy(client, announcement_route, variables['master_studentid'])
    announcements = unwrap_list(notice_data, 'Table')
    notice_attachments = unwrap_list(notice_data, 'Table1')
    before = read_flags(conversations, announcements)
    files = []
    gaps = []
    for meta in attachments:
        try:
            file_route = 'api/v1/person/' + person + '/messages/' + numeric(meta['MessageID']) + '/attachments/' + numeric(meta['AttachmentID'])
            files.append(attachment(client, file_route, meta))
        except (mcas.ReaderError, KeyError) as err:
            gaps.append({'source': 'inbox_attachment', 'error': str(err) if isinstance(err, mcas.ReaderError) else 'Missing attachment identifier.'})
    for meta in notice_attachments:
        try:
            file_data = {'ID': int(numeric(meta['AnnouncementAttachmentID'])), 'Type': meta['AttachmentsType']}
            files.append(attachment(client, 'api/v1/mcas/announcements/getFile/', meta, file_data))
        except (mcas.ReaderError, KeyError) as err:
            gaps.append({'source': 'announcement_attachment', 'error': str(err) if isinstance(err, mcas.ReaderError) else 'Missing attachment identifier.'})
    after_inbox = proxy(client, route)
    after_notice = proxy(client, announcement_route, variables['master_studentid'])
    after = read_flags(unwrap_list(after_inbox, 'Conversations'), unwrap_list(after_notice, 'Table'))
    # New messages arriving mid-run don't imply mutation; any changed existing flag is material.
    changed = [key for key in before if key not in after or before[key] != after[key]]
    if changed:
        raise mcas.ReaderError('MCAS read flags changed during collection; stop and inspect before further runs.')
    return {'conversations': conversations, 'announcements': announcements, 'attachments': files,
            'attachment_errors': gaps, 'read_flags_preserved': True,
            'portal_lists_fetched': True, 'bodies_fetched': True,
            'pagination': 'current_portal_client_uses_unpaginated_lists',
            'historical_retention_verified': False,
            'attachments_downloaded': not gaps and len(files) == len(attachments) + len(notice_attachments),
            'attachments_visually_reviewed': False,
            'source_url': mcas.BASE + '/MCAS/MCSInbox'}
