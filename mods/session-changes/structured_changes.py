"""Read-only structured diffs and an explicit, session-scoped snapshot bridge.

snapshot FILE before a script edits it; record FILE --before BASE [--after RESULT]
registers an existing comparison. No command here modifies the compared files.
Only the standard library is used; snapshots stay in Herdr's local data directory.
"""
import argparse
import csv
from datetime import date, time as clock_time
from difflib import unified_diff
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET

try:
    import tomllib
except ImportError:
    tomllib = None

MAX_BYTES = 64 * 1024 * 1024
MAX_CELLS = 1000000
MAX_ROWS = 1500
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
MISSING = object()
WORKBOOK_EXTENSIONS = {'.xlsx', '.xlsm', '.xltx', '.xltm'}
TEXT_FORMATS = {'.json', '.ipynb', '.jsonl', '.ndjson', '.csv', '.tsv', '.tab'}
if tomllib is not None:
    TEXT_FORMATS.add('.toml')
UNICODE_BOMS = (b'\xff\xfe', b'\xfe\xff', b'\x00\x00\xfe\xff')


def data_root(app_home=None):
    return Path(app_home or os.environ.get('HERDR_APP_HOME') or
                Path.home() / '.config/herdr-app') / 'data/session-changes'


def session_dir(session, app_home=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', session or ''):
        raise ValueError('An exact session ID is required (CODEX_THREAD_ID or --session)')
    return data_root(app_home) / session


def read_bytes(path):
    with open(path, 'rb') as f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('File exceeds the 64 MiB comparison limit')
    return data


def atomic_write(path, data, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        if exclusive:
            try:
                os.link(tmp, path)  # publish atomically without replacing an earlier baseline
            except FileExistsError:
                pass
        else:
            os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def freeze(path, folder):
    if path is None:
        return None
    data = read_bytes(path)
    name = hashlib.sha256(data).hexdigest() + '.snapshot'
    if not (folder / name).exists():
        atomic_write(folder / name, data)
    return name


def register(path, session, before=None, after=None, label='', snapshot=False, app_home=None):
    folder = session_dir(session, app_home)
    path = str(Path(path).resolve())
    scope = 'comparison' if after is not None else 'workspace'
    key = hashlib.sha256((os.path.normcase(path) + '\0' + scope + '\0' + (label if scope == 'comparison' else '')).encode()).hexdigest()
    entry = folder / (key + '.json')
    if snapshot and entry.exists():
        return entry  # retain the earliest baseline across repeated edits
    if snapshot:
        before = path if Path(path).exists() else None
    elif before is None:
        raise ValueError('record requires --before; use snapshot before creating a file')
    record = {'version': 1, 'session': session, 'path': path, 'scope': scope, 'label': label,
              'before': freeze(before, folder), 'after': freeze(after, folder), 'time': time.time()}
    atomic_write(entry, json.dumps(record, ensure_ascii=False).encode('utf-8'), exclusive=snapshot)
    return entry


def bridge_signature(session, app_home=None):
    folder = session_dir(session, app_home)
    parts = []
    for p in sorted(folder.glob('*.json')):
        try:
            st = p.stat()
            parts.append(f'{p.name}:{st.st_mtime_ns}:{st.st_size}')
            rec = json.loads(p.read_text(encoding='utf-8'))
            if rec.get('scope') == 'workspace':
                try:
                    st = Path(rec['path']).stat()
                    parts.append(f"{rec['path']}:{st.st_mtime_ns}:{st.st_size}")
                except FileNotFoundError:
                    parts.append('deleted')
        except (OSError, ValueError, KeyError):
            continue
    return '|'.join(parts)


def decode(data):
    if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        return data.decode('utf-32')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('gb18030')


def display(value):
    if value is MISSING:
        return '∅'
    if isinstance(value, (date, clock_time)):
        return f'{type(value).__name__} {value.isoformat()}'
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def json_changes(old, new, path='$'):
    # Compare types too: true and 1 must not compare equal.
    if not isinstance(old, (dict, list)) and type(old) is type(new) and old == new:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(old.keys() | new.keys()):
            escaped = k.replace('~', '~0').replace('/', '~1')
            yield from json_changes(old.get(k, MISSING), new.get(k, MISSING), path + '/' + escaped)
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            yield from json_changes(old[i] if i < len(old) else MISSING,
                                    new[i] if i < len(new) else MISSING, path + '/' + str(i))
    else:
        yield path, display(old), display(new)


def jsonl_values(data):
    if data is None:
        return {}
    return {i: json.loads(line) for i, line in enumerate(io.StringIO(decode(data)), 1) if line.strip()}


def is_binary(data):
    if data is None or data.startswith(UNICODE_BOMS):
        return False
    return data.startswith((b'%PDF-', b'PK\x03\x04', b'PK\x05\x06', b'\xd0\xcf\x11\xe0',
                            b'\x89PNG', b'\xff\xd8\xff', b'GIF87a', b'GIF89a', b'\x1f\x8b')) or b'\0' in data


def binary_compare(old, new):
    rows = [['', None, 'Binary content differs']] if old != new else []
    for label, data in (('Before', old), ('After', new)):
        if data is not None:
            rows.append(['', None, f'{label}: {len(data):,} bytes · SHA-256 {hashlib.sha256(data).hexdigest()}'])
    return {'kind': 'Binary', 'rows': rows, 'cut': False, 'add': 0, 'del': 0}


def text_compare(old, new):
    if is_binary(old) or is_binary(new):
        return binary_compare(old, new)
    try:
        a = decode(old).splitlines() if old is not None else []
        b = decode(new).splitlines() if new is not None else []
    except UnicodeDecodeError:
        return binary_compare(old, new)
    diff = iter(unified_diff(a, b, n=3, lineterm=''))
    next(diff, None)  # file headers, not text lines that happen to begin with -- or ++
    next(diff, None)
    rows, added, deleted, count = [], 0, 0, 0
    for line in diff:
        count += 1
        added += line.startswith('+')
        deleted += line.startswith('-')
        if len(rows) < MAX_ROWS:
            rows.append(['@', None, line] if line.startswith('@@') else [line[0], None, line[1:][:4000]])
    if not count and old != new:
        rows.append(['', None, 'Only encoding or line endings changed'])
    return {'kind': 'Text', 'rows': rows, 'cut': count > MAX_ROWS, 'add': added, 'del': deleted}


def xlsx_values(data):
    if data is None:
        return {}, {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if sum(i.file_size for i in z.infolist()) > 256 * 1024 * 1024:
            raise ValueError('Workbook expanded size exceeds 256 MiB')
        def xml(name):
            return ET.fromstring(z.read(name))
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(t.text or '' for t in s.iter(NS + 't'))
                       for s in xml('xl/sharedStrings.xml')]
        rels = {r.get('Id'): r.get('Target') for r in xml('xl/_rels/workbook.xml.rels')}
        cells, styles = {}, {}
        for sheet in xml('xl/workbook.xml').find(NS + 'sheets'):
            target = rels[sheet.get(REL + 'id')]
            name = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            # No package extraction; normalize internal relative references only.
            import posixpath
            name = posixpath.normpath(name)
            title = sheet.get('name')
            cells[title] = {}
            root = xml(name)
            for c in root.iter(NS + 'c'):
                address = c.get('r')
                if not address:
                    continue
                styles[(title, address)] = c.get('s', '0')
                formula, v = c.find(NS + 'f'), c.find(NS + 'v')
                typ = c.get('t', 'n')
                if formula is not None:
                    value = ('formula', '=' + (formula.text or ''), tuple(sorted(formula.attrib.items())))
                elif typ == 'inlineStr':
                    value = ('string', ''.join(t.text or '' for t in c.iter(NS + 't')))
                elif v is None or v.text is None:
                    continue  # styled blank cells are not value additions
                elif typ == 's':
                    value = ('string', strings[int(v.text)])
                elif typ == 'n':
                    from decimal import Decimal
                    value = ('number', str(Decimal(v.text).normalize()))
                elif typ == 'b':
                    value = ('bool', v.text == '1')
                else:
                    value = (typ, v.text)
                cells[title][address] = value
                if sum(len(s) for s in cells.values()) > MAX_CELLS:
                    raise ValueError('Workbook exceeds one million nonempty cells')
        return cells, styles


def cell_order(ref):
    m = re.fullmatch(r'([A-Z]+)(\d+)', ref)
    if not m:
        return (0, 0, ref)
    col = 0
    for char in m[1]:
        col = col * 26 + ord(char) - 64
    return int(m[2]), col, ref


def cell_display(value):
    if value is MISSING:
        return '∅'
    if value[0] == 'string':
        return display(value[1])
    if value[0] == 'bool':
        return 'true' if value[1] else 'false'
    return str(value[1])


def table_changes(old, new):
    for sheet in sorted(old.keys() | new.keys()):
        if sheet not in old or sheet not in new:
            yield f'[{sheet}]', '∅' if sheet not in old else 'Worksheet', '∅' if sheet not in new else 'Worksheet'
        a, b = old.get(sheet, {}), new.get(sheet, {})
        for ref in sorted(a.keys() | b.keys(), key=cell_order):
            x, y = a.get(ref, MISSING), b.get(ref, MISSING)
            if x != y:
                yield f'[{sheet}] {ref}', cell_display(x), cell_display(y)


def csv_values(data, delimiter):
    if data is None:
        return {}
    rows = csv.reader(io.StringIO(decode(data)), delimiter=delimiter)
    return {'Table': {f'{col_name(c)}{r}': ('string', v)
                      for r, row in enumerate(rows, 1) for c, v in enumerate(row, 1)}}


def col_name(col):
    name = ''
    while col:
        col, rem = divmod(col - 1, 26)
        name = chr(65 + rem) + name
    return name


def compare(path, old, new):
    ext = Path(path).suffix.lower()
    notes = []
    if ext in ('.json', '.ipynb'):
        a = json.loads(decode(old)) if old is not None else MISSING
        b = json.loads(decode(new)) if new is not None else MISSING
        changes, kind = json_changes(a, b), 'JSON'
    elif ext in ('.jsonl', '.ndjson'):
        a, b = jsonl_values(old), jsonl_values(new)
        changes = (change for i in sorted(a.keys() | b.keys())
                   for change in json_changes(a.get(i, MISSING), b.get(i, MISSING), f'Line {i}'))
        kind = 'JSONL'
    elif ext == '.toml' and tomllib is not None:
        a = tomllib.loads(decode(old)) if old is not None else MISSING
        b = tomllib.loads(decode(new)) if new is not None else MISSING
        changes, kind = json_changes(a, b), 'TOML'
    elif ext in WORKBOOK_EXTENSIONS:
        a, sa = xlsx_values(old)
        b, sb = xlsx_values(new)
        changed_styles = sum(sa[k] != sb[k] for k in sa.keys() & sb.keys())
        if changed_styles:
            notes.append(f'{changed_styles} existing cells changed style references')
        changes, kind = table_changes(a, b), 'XLSX'
    elif ext in ('.csv', '.tsv', '.tab'):
        delim = ',' if ext == '.csv' else '\t'
        changes, kind = table_changes(csv_values(old, delim), csv_values(new, delim)), 'Table'
    else:
        return text_compare(old, new)
    rows, added, deleted, count = [], 0, 0, 0
    for location, before, after in changes:
        count += 1
        added += after != '∅'
        deleted += before != '∅'
        if len(rows) < MAX_ROWS:
            rows.append(['C', location, before[:4000], after[:4000]])
    if not count and old != new:
        notes.append('No value/formula changes; formatting or package metadata changed')
    return {'kind': kind, 'rows': rows + [['', None, n] for n in notes], 'cut': count > MAX_ROWS,
            'add': added, 'del': deleted, 'changes': count}


def restore_text_baseline(current, ops):
    """Reverse complete native hunks, verifying every line against current content.

    Refuse incomplete/contextless patches and later disk edits rather than invent
    a baseline. This lets native JSON edits use the same field comparison view.
    """
    lines = decode(current).splitlines()
    for op in reversed(ops):
        if op.get('hunks'):
            end = len(lines)
            for old, new in reversed(op['hunks']):
                if not new:
                    raise ValueError('Recorded hunk has no post-edit context')
                after = new.split('\n')
                matches = [i for i, line in enumerate(lines) if line == after[0]
                           and lines[i:i + len(after)] == after]
                if len(matches) != 1 or matches[0] + len(after) > end:
                    raise ValueError('Recorded hunk cannot be located uniquely in the file')
                end = matches[0]
                lines[end:end + len(after)] = old.split('\n') if old is not None else []
            continue
        groups, group = [], None
        for row in op['rows']:
            if row[0] == '@':
                match = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)', row[2])
                if not match:
                    raise ValueError('Incomplete patch baseline')
                group = [int(match[1]), [], []]
                groups.append(group)
            elif row[0] in (' ', '+', '-'):
                if group is None:
                    raise ValueError('No hunk coordinates')
                if row[0] in (' ', '-'):
                    group[1].append(row[2])
                if row[0] in (' ', '+'):
                    group[2].append(row[2])
        if not groups:
            raise ValueError('No reversible hunks')
        for start, before, after in reversed(groups):
            offset = max(0, start - 1) if after else start
            if lines[offset:offset + len(after)] != after:
                raise ValueError('Disk content differs from recorded edit')
            lines[offset:offset + len(after)] = before
    return '\n'.join(lines).encode('utf-8')


def load_records(session, cwd, app_home=None):
    folder = session_dir(session, app_home)
    out = []
    for entry in sorted(folder.glob('*.json')):
        rec = None
        try:
            rec = json.loads(entry.read_text(encoding='utf-8'))
            if rec.get('version') != 1 or rec.get('session') != session:
                continue
            def stored(name):
                if name is None:
                    return None
                if not re.fullmatch(r'[a-f0-9]{64}\.snapshot', name):
                    raise ValueError('Invalid snapshot reference')
                data = read_bytes(folder / name)
                if hashlib.sha256(data).hexdigest() != name[:-9]:
                    raise ValueError('Snapshot integrity check failed')
                return data
            old = stored(rec['before'])
            frozen = rec.get('scope') == 'comparison'
            new = stored(rec['after']) if frozen else read_bytes(rec['path']) if Path(rec['path']).exists() else None
            if old == new:
                continue
            try:
                result = compare(rec['path'], old, new)
            except (ValueError, RecursionError) as e:
                if Path(rec['path']).suffix.lower() not in TEXT_FORMATS:
                    raise
                result = text_compare(old, new)
                result['rows'].insert(0, ['', None, f'Structured comparison unavailable: {e}'])
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, IndexError, RecursionError) as e:
            if not rec or not rec.get('path'):
                continue
            result = {'kind': 'Error', 'rows': [['', None, f'Cannot compare: {e}']], 'cut': False, 'add': 0, 'del': 0}
            frozen = rec.get('scope') == 'comparison'
            old, new = True, True
        path = rec['path']
        rel = os.path.relpath(path, cwd) if cwd and os.path.splitdrive(path)[0] == os.path.splitdrive(cwd)[0] else path
        if rel.startswith('..'):
            rel = path
        label = rec.get('label') or ('Saved comparison' if frozen else 'Session baseline → current file')
        result.update({'path': path + '::' + entry.stem if frozen else path, 'source_path': path,
                       'rel': rel.replace('\\', '/'), 'status': 'A' if old is None else 'D' if new is None else 'M',
                       'scope': 'comparison' if frozen else 'workspace', 'label': label, 'structured': True,
                       'script': None, 'last': 0, 'time': rec.get('time', 0), 'edits': 1})
        result['rows'].insert(0, ['@', None, label])
        out.append(result)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['snapshot', 'record'])
    parser.add_argument('path')
    parser.add_argument('--before')
    parser.add_argument('--after')
    parser.add_argument('--label', default='')
    parser.add_argument('--app-home')
    parser.add_argument('--session', default=os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID') or os.environ.get('CLAUDE_SESSION_ID'))
    args = parser.parse_args()
    entry = register(args.path, args.session, args.before, args.after, args.label,
                     args.command == 'snapshot', args.app_home)
    print(json.dumps({'registered': str(entry), 'session': args.session}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as e:
        raise SystemExit(str(e))
