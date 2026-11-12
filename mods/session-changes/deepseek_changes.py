"""Find the DeepSeek log mounted by a terminal and replay confirmed file changes."""
from collections import defaultdict
from contextlib import contextmanager
from difflib import unified_diff
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit
from urllib.request import url2pathname


def read_json(path, default):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def mounted_session(pids, title, selected='', pinned=False):
    dsh = Path(os.environ.get('DSH_HOME') or Path.home() / '.dsh')
    tui = Path(os.environ.get('DSH_TUI_HOME') or Path.home() / '.dsh-tui')
    owners = read_json(tui / 'session-mounts.json', {}).get('owners') or []
    ids = [sid for owner in owners if isinstance(owner, dict) and owner.get('pid') in pids
           for sid in owner.get('sessionIds') or []]
    if not ids:
        servers = read_json(tui / 'inject/servers.json', [])
        ids = [server.get('sessionId') for server in servers
               if isinstance(server, dict) and server.get('pid') in pids]
    sessions = {}
    for value in ids:
        if not isinstance(value, str):
            continue
        session = value.removeprefix('dsh:')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', session) or session in sessions:
            continue
        projection = read_json(dsh / 'storages/session_projcache/sessions' / f'{session}.json', {})
        row = (projection.get('record') or {}).get('rows', {}).get('title') or {}
        sessions[session] = str(row.get('val') or '').strip()
    if not sessions:
        return None

    shown = re.sub(r'^[\u2800-\u28ff✦\s]*(?:🐋\s*)?', '', title).strip()
    matches = [sid for sid, name in sessions.items() if name and name == shown]
    choices = []
    if not pinned and len(matches) != 1 and len(sessions) > 1:
        choices = [{'id': sid, 'name': f'{name} · {sid[:8]}' if name else sid[:8]}
                   for sid, name in sessions.items()]
    if pinned:
        if selected not in sessions:
            return {'error': 'This DeepSeek conversation is no longer mounted'}
        session = selected
    elif len(matches) == 1:
        session = matches[0]
    elif selected in sessions:
        session = selected
    elif len(sessions) == 1:
        session = next(iter(sessions))
    else:
        return {'error': 'Select a DeepSeek conversation', 'choices': choices}

    logs = {}
    for name in ('session.v4.jsonl.zstd', 'session.v4.jsonl'):
        for path in (dsh / 'sessions').glob(f'*/{session}/{name}'):
            if path.is_file():
                logs.setdefault(path.parent, path)
    if len(logs) > 1:
        return {'error': 'DeepSeek session log is ambiguous'}
    # DSH can mount a fresh conversation before it writes its first event.
    return {'agent': 'DeepSeek', 'session': session, 'name': sessions[session], 'choices': choices,
            'transcripts': list(logs.values())}


@contextmanager
def open_log(path):
    if Path(path).suffix != '.zstd':
        with open(path, 'rb') as stream:
            yield stream
        return
    try:
        from compression import zstd
    except ImportError:
        executable = shutil.which('zstd')
        if not executable:
            raise OSError('DeepSeek logs need Python 3.14 or zstd on PATH')
        with subprocess.Popen([executable, '-dc', '--', str(path)], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as proc:
            with proc.stdout as stream:
                yield stream
            if proc.wait():
                raise OSError('DeepSeek session log could not be decompressed')
    else:
        try:
            with zstd.open(path, 'rb') as stream:
                yield stream
        except zstd.ZstdError as e:
            raise OSError('DeepSeek session log could not be decompressed') from e


def local_path(path, cwd=''):
    if not isinstance(path, str) or not path or '\0' in path:
        return None
    if '://' in path:
        url = urlsplit(path)
        if url.scheme != 'file' or url.netloc not in ('', 'localhost'):
            return None
        path = url2pathname(url.path)
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(cwd, path)) if cwd else None


def patch_paths(text):
    """The model-facing result lists committed A/M/D paths, including before a partial failure."""
    listing = False
    for line in text.splitlines():
        if line in ('Success. Updated the following files:', 'Completed:'):
            listing = True
        elif listing:
            if not re.match(r'^[AMD] .+', line):
                break
            yield line[0], line[2:]


def recorded_rows(hunks):
    rows = []
    for old, new in hunks:
        before = [] if old is None else old.split('\n')
        after = new.split('\n') if new else []
        diff = iter(unified_diff(before, after, lineterm=''))
        next(diff, None)
        next(diff, None)
        for line in diff:
            rows.append(['@', None, '@@ recorded hunk @@'] if line.startswith('@@')
                        else [line[0], None, line[1:]])
    return rows


def read_changes(path, session, ch, stamp):
    """Use the tool's recorded effects. Shell command arguments only inform script filtering."""
    calls = {}
    with open_log(path) as stream:
        header = json.loads(stream.readline())
        if not isinstance(header, dict) or header.get('type') != 'session' or header.get('id') != session:
            raise ValueError('DeepSeek log header does not match the mounted session')
        cwd = local_path(header.get('cwd'))
        if cwd is None:
            raise ValueError('DeepSeek session does not have a local workspace')
        for line in stream:
            if not line.endswith(b'\n'):
                break  # an in-progress record will be read on the next refresh
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            data = event.get('data') or {}
            if event.get('type') == 'tool/call':
                name = data.get('name')
                if name not in ('write', 'edit', 'apply_patch', 'bash'):
                    continue
                args = data.get('arguments')
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except ValueError:
                    args = None
                calls[data.get('callId')] = name, args if isinstance(args, dict) else {}
            elif event.get('type') == 'tool/result':
                message = data.get('message') or {}
                call = calls.pop(message.get('toolCallId'), None)
                if call is None or message.get('isError') or data.get('error'):
                    continue
                name, args = call
                if name == 'bash':
                    ch.command(args.get('command'))
                    continue
                meta = data.get('meta') or {}
                hunks = defaultdict(list)
                for diff in meta.get('diffs', []):
                    file = local_path(diff.get('path'), cwd)
                    old, new = diff.get('oldText'), diff.get('newText')
                    if file and (old is None or isinstance(old, str)) and isinstance(new, str):
                        hunks[file].append((old, new))
                when = stamp(event.get('time'))
                if name == 'apply_patch':
                    text = '\n'.join(block.get('text', '') for block in message.get('content', [])
                                     if block.get('type') == 'text')
                    moves = {}
                    patch = args.get('input')
                    if isinstance(patch, str):
                        for source, target in re.findall(
                                r'^\*\*\* Update File: ([^\r\n]+)\r?\n\*\*\* Move to: ([^\r\n]+)\r?$', patch, re.M):
                            moves[local_path(target, cwd)] = local_path(source, cwd)
                    for status, raw in patch_paths(text):
                        file = local_path(raw, cwd)
                        if file:
                            source = moves.get(file) if status == 'M' else None
                            if source and source != file:
                                ch.add(source, 'D', recorded_rows(hunks[source]), when)
                                status = 'A'
                            changes = hunks[file]
                            ch.add(file, status, recorded_rows(changes), when, hunks=changes)
                else:
                    file = local_path(meta.get('path') or args.get('file_path'), cwd)
                    if not file:
                        continue
                    if name == 'write' and meta.get('operation') == 'create':
                        ch.add(file, 'A', [], when)
                    elif hunks[file] or meta.get('operation') == 'update':
                        changes = hunks[file]
                        ch.add(file, 'M', recorded_rows(changes), when, hunks=changes)
    return cwd
