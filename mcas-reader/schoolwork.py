"""Read the portal's rendered school calendar and homework data, never execute JS."""
import ast
import datetime as dt
import html
from html.parser import HTMLParser
import json
import re
import urllib.parse

import mcas

WANTED = {'holiday', 'staff only'}


class PlainText(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.feed(value or '')

    def handle_data(self, value):
        self.parts.append(value)


def plain(value):
    return ' '.join(' '.join(PlainText(value).parts).split())


def calendar_days(page):
    decoder = json.JSONDecoder()
    days = {}
    found = False
    for match in re.finditer(r'"targetControls"\s*:', page):
        try:
            value, _ = decoder.raw_decode(page[match.end():].lstrip())
            if not isinstance(value, str) or len(value) > 2 * 1024 * 1024:
                continue
            rows = ast.literal_eval(value)
            if not isinstance(rows, list) or len(rows) > 5000:
                continue
            for row in rows:
                if not isinstance(row, list) or len(row) < 3:
                    continue
                item = json.loads(row[2])
                if not isinstance(item, dict) or 'date' not in item or 'text' not in item:
                    continue
                day = dt.date.fromisoformat(item['date'][:10]).isoformat()
                label = plain(item['text'])
                if day in days and days[day] != label:
                    raise mcas.ReaderError('School calendar has conflicting labels for one date.')
                days[day] = label
                found = True
        except (ValueError, SyntaxError, TypeError, KeyError):
            continue
    if not found:
        raise mcas.ReaderError('School calendar date labels could not be read; this is not an empty calendar.')
    return [{'date': day, 'label': days[day]} for day in sorted(days)]


def important_ranges(days, start=None):
    start = start or dt.datetime.now(mcas.LONDON).date().isoformat()
    selected = [d for d in days if d['date'] >= start and d['label'].strip().lower() in WANTED]
    ranges = []
    for row in sorted(selected, key=lambda r: r['date']):
        date = dt.date.fromisoformat(row['date'])
        if ranges and ranges[-1]['label'] == row['label'] and dt.date.fromisoformat(ranges[-1]['end']) + dt.timedelta(days=1) == date:
            ranges[-1]['end'] = row['date']
        else:
            ranges.append({'start': row['date'], 'end': row['date'], 'label': row['label']})
    return ranges


def homework(page):
    match = re.search(r'\bvar\s+homeWorkListItem\s*=\s*', page)
    if not match:
        raise mcas.ReaderError('Homework data is missing from the page; coverage is not established.')
    try:
        rows, _ = json.JSONDecoder().raw_decode(page[match.end():])
    except ValueError:
        raise mcas.ReaderError('Homework format changed.') from None
    if not isinstance(rows, list):
        raise mcas.ReaderError('Homework is not a task list.')
    result = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get('HomeworkID') or not row.get('HomeworkTitle'):
            raise mcas.ReaderError('Homework source identifiers are missing.')
        key = str(row.get('SchoolID', '')) + ':' + str(row['HomeworkID'])
        if key in ids:
            raise mcas.ReaderError('Duplicate homework IDs need inspection.')
        ids.add(key)
        dates = {}
        for field in ['AssignedDate', 'DueDate']:
            raw = row.get(field)
            if raw:
                try:
                    dt.datetime.fromisoformat(raw)
                except (ValueError, TypeError):
                    raise mcas.ReaderError('A homework date could not be parsed.') from None
            dates[field] = raw
        result.append({'id': key, 'title': plain(row['HomeworkTitle']), 'subject': row.get('Subject'),
                       'set_at': dates['AssignedDate'], 'due_at': dates['DueDate'],
                       'instructions': plain(row.get('HomeworkDescription', '')),
                       'teacher': row.get('SubjectTeacher'), 'resources': row.get('Resources') or [],
                       'submission_evidence': plain(row.get('SubmittedWork', '')),
                       'completion_status': 'unknown', 'raw_source': row})
    return sorted(result, key=lambda r: (r['due_at'] or '9999', r['id']))


def collect(client, url, dashboard):
    links = {}
    for href in mcas.Page(dashboard).links:
        target = urllib.parse.urljoin(url, href)
        parsed = urllib.parse.urlsplit(target)
        if parsed[:2] != urllib.parse.urlsplit(mcas.BASE)[:2] or parsed.query:
            continue
        if re.fullmatch(r'/MCAS/MCS(?:AcademicCalendar|Homework)(?:\.aspx)?', parsed.path, re.I):
            links['homework' if 'homework' in parsed.path.lower() else 'calendar'] = target
    name_match = re.search(r'\bcurrentStudentFullName\s*=\s*[\"\x27]([^\"\x27]+)', dashboard)
    school_match = re.search(r'\bmaster_schoolName\s*=\s*[\"\x27]([^\"\x27]+)', dashboard)
    result = {'pupil': html.unescape(name_match.group(1)) if name_match else 'Pupil',
              'school': html.unescape(school_match.group(1)) if school_match else None,
              'calendar_available': 'calendar' in links, 'homework_available': 'homework' in links,
              'errors': [], 'homework': [], 'calendar_ranges': []}
    for kind, target in links.items():
        try:
            final, page = client.request(target)
            if 'MCSParentLogin' in final or 'MCSContactSelect' in final:
                raise mcas.ReaderError('Session expired while reading schoolwork.')
            if kind == 'calendar':
                days = calendar_days(page)
                result.update(calendar_days=days, calendar_ranges=important_ranges(days), calendar_source=final,
                              calendar_coverage={'first_date': days[0]['date'], 'last_date': days[-1]['date'], 'date_count': len(days)})
            else:
                result['homework'] = homework(page)
                result['homework_source'] = final
                result['homework_coverage'] = 'Tasks in the current portal page; older/other academic-year views not yet verified.'
        except mcas.ReaderError as err:
            result['errors'].append({'source': kind, 'error': str(err)})
    return result
