"""Files an agent conversation changed, read from the agent's own transcript.

Usage: python session_changes.py <terminal pid> <terminal cwd> [title] [signature] [app] [session] [pinned]
Prints one JSON object. With a matching signature: {"same": true}.
Supports Claude Code, Codex, Pi and DeepSeek Harness.
"""
import ctypes
import ctypes.wintypes as wt
import datetime
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

import deepseek_changes
from structured_changes import (TEXT_FORMATS, WORKBOOK_EXTENSIONS, bridge_signature, load_records,
                                compare, decode, is_binary, read_bytes, restore_text_baseline)

HOME = os.path.expanduser('~')
MAX_ROWS = 1500
SCRIPT_EXT = {'.py', '.pyw', '.js', '.mjs', '.cjs', '.ts', '.mts', '.ps1', '.psm1', '.sh', '.bash',
              '.bat', '.cmd', '.lua', '.rb', '.pl', '.vbs', '.jsx', '.tsx'}
TOOLISH = re.compile(r'(^|[._\-])(verify|probe|check|validate|validation|test|tests|debug|tmp|temp|scratch|'
                     r'inspect|dump|repro|diag|diagnose|smoke|try|poc|sample)([._\-]|$)', re.I)
def long_path(path):
    """%TEMP% is often 8.3 (C:\\Users\\ABCDEF~1\\...); agents log the long form."""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetLongPathNameW(path, buf, 1024):
            return buf.value
    except Exception:
        pass
    return path


TEMP_DIRS = sorted({os.path.normcase(os.path.abspath(form)) for p in
                    {os.environ.get('TEMP', ''), os.environ.get('TMP', ''), '/tmp'} if p
                    for form in (p, long_path(p))})
INTERPRETER = r'(python[0-9.]*|py|pythonw|node|deno|bun|tsx|ts-node|pwsh|powershell|bash|sh|zsh|lua|luajit|wezterm|ruby|perl|cscript|wscript|cmd|uv|npx|source|\.|&)'


# ---------- processes ----------

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD), ('szExeFile', ctypes.c_wchar * 260)]


def processes():
    """{pid: (ppid, exe name, start unix seconds or None)}"""
    if sys.platform == 'linux':
        return linux_processes()
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    out = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        out[entry.th32ProcessID] = [entry.th32ParentProcessID, entry.szExeFile.lower(), None]
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return out


def linux_processes():
    ticks_per_second = os.sysconf('SC_CLK_TCK')
    boot_time = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
    out = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            # The command can contain spaces and parentheses. Fields after it cannot.
            command, fields = path.read_text(errors='replace').rsplit(') ', 1)
            fields = fields.split()
            name = command.split(' (', 1)[1].lower()
            out[int(path.parent.name)] = [
                int(fields[1]), name, boot_time + int(fields[19]) / ticks_per_second,
            ]
        except (OSError, ValueError, IndexError):
            continue  # A process can exit between enumeration and read.
    return out


def linux_pi(pid, name):
    if name == 'pi':
        return True
    if name != 'node':
        return False
    try:
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0', 3)
        return any(os.path.basename(arg) == b'pi' or
                   (b'pi-coding-agent' in arg and arg.endswith(b'/cli.js')) for arg in argv[:3])
    except OSError:
        return False


def start_time(pid):
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wt.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    c, e, kt, ut = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    ok = k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut))
    k32.CloseHandle(h)
    if not ok:
        return None
    ft = (c.dwHighDateTime << 32) | c.dwLowDateTime
    return ft / 1e7 - 11644473600


def descendants(procs, root):
    children = {}
    for pid, (ppid, _, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        for child in children.get(pid, []):
            if child != pid and child not in out:
                out.append(child)
                todo.append(child)
    return out


# ---------- helpers ----------

def norm(path):
    return os.path.normcase(os.path.abspath(path.replace('file:///', ''))) if path else ''


def same_dir(a, b):
    return bool(a) and bool(b) and norm(a).rstrip('\\/') == norm(b).rstrip('\\/')


def read_lines(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        pass
    except OSError:
        return


def first_json(path):
    for obj in read_lines(path):
        return obj
    return None


class Stamp(str):
    """'HH:MM' local, carrying the moment as .epoch (0 when unknown)."""
    def __new__(cls, value):
        try:
            if isinstance(value, (int, float)):
                t = datetime.datetime.fromtimestamp(value / 1000)
            else:
                t = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone()
            s = super().__new__(cls, t.strftime('%H:%M'))
            s.epoch = t.timestamp()
        except Exception:
            s = super().__new__(cls, '')
            s.epoch = 0
        return s


stamp = Stamp


def text_rows(text, start=1, kind='+'):
    lines = text.replace('\r\n', '\n').split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return [[kind, start + i, l] for i, l in enumerate(lines)]


def unified_rows(diff):
    rows, old, new = [], 0, 0
    for line in diff.replace('\r\n', '\n').split('\n'):
        m = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)', line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            rows.append(['@', None, line])
        elif line.startswith(('---', '+++')) and not rows:
            continue
        elif line.startswith('-'):
            rows.append(['-', old, line[1:]]); old += 1
        elif line.startswith('+'):
            rows.append(['+', new, line[1:]]); new += 1
        elif line.startswith(' '):
            rows.append([' ', new, line[1:]]); old += 1; new += 1
        elif line.startswith('\\'):
            continue
    return rows


def patch_rows(patch):
    """Claude Code structuredPatch."""
    rows = []
    for h in patch or []:
        old, new = h.get('oldStart', 0), h.get('newStart', 0)
        rows.append(['@', None, f"@@ -{old},{h.get('oldLines', 0)} +{new},{h.get('newLines', 0)} @@"])
        for line in h.get('lines', []):
            k = line[:1]
            if k == '-':
                rows.append(['-', old, line[1:]]); old += 1
            elif k == '+':
                rows.append(['+', new, line[1:]]); new += 1
            elif k == '\\':
                continue
            else:
                rows.append([' ', new, line[1:]]); old += 1; new += 1
    return rows


def pi_rows(diff):
    """pi's details.diff: '+ 5 text', '- 6 text', '  1 text', '    ...'."""
    rows = []
    for line in diff.replace('\r\n', '\n').split('\n'):
        m = re.match(r'^([+\- ])\s*(\d+) ?(.*)$', line)
        if m:
            rows.append([m.group(1), int(m.group(2)), m.group(3)])
        elif line.strip() == '...':
            rows.append(['@', None, '@@ … @@'])
    return rows


def pair_rows(old, new):
    rows = [['@', None, '@@ edit @@']]
    rows += text_rows(old, 1, '-') if old else []
    rows += text_rows(new, 1, '+') if new else []
    for r in rows[1:]:
        r[1] = None
    return rows


# ---------- sessions ----------

class Changes:
    def __init__(self):
        self.files = {}      # norm path -> record
        self.commands = []   # shell command strings, in order
        self.calls = []      # [start, end, text]: commands and MCP calls, with their output
        self.order = 0

    def add(self, path, op, rows, when, *, hunks=None):
        if not path:
            return
        key = norm(path)
        rec = self.files.get(key)
        if rec is None:
            rec = self.files[key] = {'path': os.path.abspath(path.replace('file:///', '')), 'ops': [],
                                     'created': op == 'A', 'first': self.order}
        rec['ops'].append({'op': op, 'rows': rows, 'when': when, 'cmd_at': len(self.commands)})
        if hunks:
            rec['ops'][-1]['hunks'] = hunks
        rec['last'] = self.order
        self.order += 1

    def call(self, start, end, *parts):
        if not end:
            return
        text = '\n'.join(p if isinstance(p, str) else json.dumps(p, ensure_ascii=False) for p in parts if p)
        self.calls.append([start or end, end, text[:200000]])

    def command(self, value):
        if isinstance(value, list):
            value = ' '.join(str(v) for v in value)
        if isinstance(value, str) and value.strip():
            self.commands.append(value)


def claude_transcripts(pid_list):
    for pid in pid_list:
        info_path = os.path.join(HOME, '.claude', 'sessions', f'{pid}.json')
        if not os.path.exists(info_path):
            continue
        try:
            info = json.load(open(info_path, encoding='utf-8'))
        except (OSError, ValueError):
            continue
        sid = info.get('sessionId')
        main = glob.glob(os.path.join(HOME, '.claude', 'projects', '*', f'{sid}.jsonl'))
        if not main:
            continue
        subs = glob.glob(os.path.join(os.path.dirname(main[0]), sid, 'subagents', '*.jsonl'))
        return {'agent': 'Claude Code', 'session': sid, 'name': info.get('name'), 'cwd': info.get('cwd'),
                'transcripts': main + sorted(subs)}
    return None


def read_claude(paths, ch):
    for path in paths:
        uses, starts = {}, {}
        for o in read_lines(path):
            msg = o.get('message') or {}
            content = msg.get('content') if isinstance(msg, dict) else None
            when = stamp(o.get('timestamp'))
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get('type') == 'tool_use':
                        inp = c.get('input') or {}
                        uses[c.get('id')] = (c.get('name'), inp)
                        if c.get('name') in ('Bash', 'PowerShell'):
                            ch.command(inp.get('command'))
                            starts[c.get('id')] = (when.epoch, inp.get('command'))
                        elif str(c.get('name', '')).startswith('mcp__'):
                            ch.command(json.dumps(inp, ensure_ascii=False))
                            starts[c.get('id')] = (when.epoch, inp)
                    elif c.get('type') == 'tool_result':
                        if c.get('tool_use_id') in starts:
                            t0, said = starts.pop(c.get('tool_use_id'))
                            ch.call(t0, when.epoch, said, c.get('content'))
                        r = o.get('toolUseResult')
                        use = uses.get(c.get('tool_use_id'))
                        if not use or c.get('is_error') or not isinstance(r, dict):
                            continue
                        name, inp = use
                        fp = r.get('filePath') or inp.get('file_path') or inp.get('notebook_path')
                        if name == 'Write' or r.get('type') in ('create', 'update'):
                            if r.get('type') == 'create' or (name == 'Write' and not r.get('structuredPatch')):
                                ch.add(fp, 'A', text_rows(r.get('content') or inp.get('content') or ''), when)
                            else:
                                ch.add(fp, 'M', patch_rows(r.get('structuredPatch')), when)
                        elif name in ('Edit', 'MultiEdit'):
                            rows = patch_rows(r.get('structuredPatch'))
                            if not rows:
                                rows = pair_rows(inp.get('old_string', ''), inp.get('new_string', ''))
                            ch.add(fp, 'M', rows, when)
                        elif name == 'NotebookEdit':
                            ch.add(fp, 'M', pair_rows(r.get('old_source', '') or '', r.get('new_source', '') or inp.get('new_source', '')), when)


def rollout_time(path):
    m = re.search(r'rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})', os.path.basename(path))
    if not m:
        return None
    return datetime.datetime.strptime(m.group(1), '%Y-%m-%dT%H-%M-%S').timestamp()


def pi_time(path):
    m = re.match(r'(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(\d{3})Z', os.path.basename(path))
    if not m:
        return None
    t = datetime.datetime.strptime(m.group(1), '%Y-%m-%dT%H-%M-%S').replace(tzinfo=datetime.timezone.utc)
    return t.timestamp() + int(m.group(2)) / 1000


def pick_recent(files, when_of, cwd_of, start, cwd, others):
    """The newest transcript started after this agent, in its folder, not started by another one."""
    best = None
    for f in files:
        t = when_of(f)
        if t is None or t < start - 3:
            continue
        # Opened right as a newer agent of the same kind started: that one's.
        if any(start < s <= t + 3 and t - s < 20 for s in others):
            continue
        if cwd and not same_dir(cwd_of(f), cwd):
            continue
        m = os.path.getmtime(f)
        if best is None or m > best[0]:
            best = (m, f)
    return best[1] if best else None


def codex_transcript(start, cwd, others):
    days = set()
    d = datetime.date.fromtimestamp(start - 86400)
    while d <= datetime.date.today():
        days.add(d); d += datetime.timedelta(days=1)
    files = []
    for d in days:
        files += glob.glob(os.path.join(HOME, '.codex', 'sessions', f'{d:%Y}', f'{d:%m}', f'{d:%d}', 'rollout-*.jsonl'))
    def cwd_of(f):
        meta = first_json(f) or {}
        return (meta.get('payload') or {}).get('cwd')
    return pick_recent(files, rollout_time, cwd_of, start, cwd, others)


def linux_codex_rollout(pids):
    files = set()
    for pid in pids:
        try:
            for fd in Path(f'/proc/{pid}/fd').iterdir():
                try:
                    path = fd.readlink()
                except OSError:
                    continue
                if path.name.startswith('rollout-') and path.suffix == '.jsonl' and path.is_file():
                    files.add(path)
        except OSError:
            continue
    return str(next(iter(files))) if len(files) == 1 else None


def codex_by_title(title, start):
    """Codex titles its terminal '<thread name> | <folder>'; resumed threads keep their old rollout."""
    name = clean_title(title)
    if not name:
        return None
    import sqlite3
    db = os.path.join(HOME, '.codex', 'state_5.sqlite')
    if not os.path.exists(db):
        found = sorted(glob.glob(os.path.join(HOME, '.codex', 'state_*.sqlite')))
        if not found:
            return None
        db = found[-1]
    try:
        con = sqlite3.connect('file:' + db.replace('\\', '/') + '?mode=ro', uri=True, timeout=2)
        rows = con.execute('select rollout_path, updated_at_ms, cwd from threads where name = ? or title = ? '
                           'order by updated_at_ms desc', (name, name)).fetchall()
        con.close()
    except Exception:
        return None
    unc = lambda p: p[4:] if p and p.startswith('\\\\?\\') else p
    rows = [(unc(p), u, unc(c)) for p, u, c in rows if os.path.exists(unc(p))]
    for path, updated, folder in rows:
        if not start or (updated or 0) / 1000 >= start - 5:
            return path, folder
    return (rows[0][0], rows[0][2]) if rows else None


def clean_title(title):
    # Codex prefixes the thread name with a live Braille spinner/status glyph.
    return re.sub(r'^[\u2800-\u28ff●○◉✓✔✗✕⏺⏵⏸\s]+', '',
                  (title or '').rsplit(' | ', 1)[0]).strip()


def took(item):
    d = item.get('duration') or item.get('duration_ms')
    if isinstance(d, dict):
        return d.get('secs', 0) + d.get('nanos', 0) / 1e9
    return d / 1000 if isinstance(d, (int, float)) else 600


def read_codex(path, ch):
    for o in read_lines(path):
        p = o.get('payload') or {}
        if p.get('type') != 'item_completed':
            continue
        it = p.get('item') or {}
        when = stamp(o.get('timestamp'))
        if it.get('type') == 'CommandExecution':
            cmd = it.get('command')
            if isinstance(cmd, list):
                for index, flag in enumerate(cmd[:-1]):
                    if str(flag).lower() in ('-command', '-c', '/c'):
                        cmd = cmd[index + 1]
                        break
            ch.command(cmd)
            ch.call(when.epoch - took(it), when.epoch, cmd, it.get('aggregated_output'))
        elif it.get('type') == 'McpToolCall':
            ch.command(json.dumps(it.get('arguments') or {}, ensure_ascii=False))
            ch.call(when.epoch - took(it), when.epoch, it.get('arguments'), it.get('result'))
        elif it.get('type') == 'FileChange' and it.get('status', 'completed') == 'completed':
            for fp, c in (it.get('changes') or {}).items():
                kind = c.get('type')
                if kind == 'add':
                    ch.add(fp, 'A', text_rows(c.get('content') or ''), when)
                elif kind == 'delete':
                    ch.add(fp, 'D', text_rows(c.get('content') or '', 1, '-'), when)
                else:
                    target = c.get('move_path') or fp
                    ch.add(target, 'M', unified_rows(c.get('unified_diff') or ''), when)


def pi_transcript(start, cwd, others):
    files = glob.glob(os.path.join(HOME, '.pi', 'agent', 'sessions', '*', '*.jsonl'))
    def cwd_of(f):
        return (first_json(f) or {}).get('cwd')
    return pick_recent(files, pi_time, cwd_of, start, cwd, others)


def read_pi(path, ch):
    calls, starts = {}, {}
    for o in read_lines(path):
        m = o.get('message') or {}
        when = stamp(o.get('timestamp'))
        if isinstance(m.get('content'), list):
            for c in m['content']:
                if isinstance(c, dict) and c.get('type') == 'toolCall':
                    calls[c.get('id')] = (c.get('name'), c.get('arguments') or {})
                    if c.get('name') not in ('edit', 'write', 'read'):
                        args = c.get('arguments') or {}
                        ch.command(args.get('command') or args.get('code') or json.dumps(args, ensure_ascii=False))
                        starts[c.get('id')] = (when.epoch, args)
        if m.get('role') == 'toolResult' and m.get('toolCallId') in starts:
            t0, said = starts.pop(m.get('toolCallId'))
            ch.call(t0, when.epoch, said, m.get('content'))
        if m.get('role') == 'toolResult' and not m.get('isError'):
            name, args = calls.get(m.get('toolCallId'), (m.get('toolName'), {}))
            path_ = args.get('path')
            if name == 'write':
                ch.add(path_, 'A' if norm(path_) not in ch.files else 'M', text_rows(args.get('content') or ''), when)
            elif name == 'edit':
                diff = (m.get('details') or {}).get('diff')
                rows = pi_rows(diff) if diff else []
                if not rows:
                    for e in args.get('edits') or [args]:
                        rows += pair_rows(e.get('oldText', ''), e.get('newText', ''))
                ch.add(path_, 'M', rows, when)


# ---------- tool scripts ----------

def executed(rec, ch):
    """Run by the agent after it wrote it: `python x.py`, `& .\\x.ps1`, `node work/x.mjs`, ..."""
    base = re.escape(os.path.basename(rec['path']))
    first = rec['ops'][0]['cmd_at']
    run = re.compile(INTERPRETER + r'(\.exe)?["\']?\s+[^;|\n]*?' + base + r'(["\'\s;|)]|$)', re.I)
    direct = re.compile(r'(^|[;&|\n]\s*)["\']?[^\s;&|"\']*' + base + r'["\']?(\s|$)', re.I)
    # Loaded by an embedded interpreter: exec(open(...x.py).read()), runpy.run_path, dofile, ...
    load = re.compile(r'(exec|execfile|run_path|run_module|dofile|loadfile|import_module|spawn|Popen|subprocess)\b[^\n]{0,300}?' + base, re.I)
    return any(run.search(c) or direct.search(c) or load.search(c) for c in ch.commands[first:])


def tool_script(rec, ch):
    path = norm(rec['path'])
    if any(t and path.startswith(t + os.sep) for t in TEMP_DIRS):
        return 'temp folder'
    if not rec['created'] or os.path.splitext(path)[1] not in SCRIPT_EXT:
        return None
    if executed(rec, ch):
        return 'run by the agent'
    if TOOLISH.search(os.path.splitext(os.path.basename(path))[0]):
        return 'helper name'
    return None


def current_rows(path):
    try:
        with open(path, 'rb') as f:
            data = f.read(400000)
    except OSError:
        return None
    if is_binary(data):
        return [['', None, 'Binary file']]
    try:
        return text_rows(decode(data))
    except UnicodeDecodeError:
        return text_rows(data.decode('utf-8', errors='replace'))


def build(ch, cwd):
    out = []
    for key, rec in ch.files.items():
        exists = os.path.exists(rec['path'])
        last = rec['ops'][-1]['op']
        if rec['created'] and (not exists or last == 'D'):
            continue  # made and removed during the conversation
        status = 'D' if (last == 'D' or not exists) else 'A' if rec['created'] else 'M'
        if status == 'A':
            rows = current_rows(rec['path']) or [r for op in rec['ops'] for r in op['rows']]
            if rows and rows[0][0] != '@':
                rows = [['@', None, f'@@ -0,0 +1,{len(rows)} @@ new file']] + rows
        else:
            rows = []
            for i, op in enumerate(rec['ops']):
                label = f"edit {i + 1}/{len(rec['ops'])}" + (f" · {op['when']}" if op['when'] else '')
                op_rows = op['rows'] or [['', None, '(no text diff)']]
                if op_rows[0][0] == '@':
                    op_rows = [['@', None, f"{op_rows[0][2]}  {label}"]] + op_rows[1:]
                else:
                    op_rows = [['@', None, f'@@ {label} @@']] + op_rows
                rows += op_rows
        add = sum(1 for r in rows if r[0] == '+')
        dele = sum(1 for r in rows if r[0] == '-')
        rel = rec['path']
        if cwd:
            try:
                r = os.path.relpath(rec['path'], cwd)
                if not r.startswith('..'):
                    rel = r
            except ValueError:
                pass
        out.append({'path': rec['path'], 'rel': rel.replace('\\', '/'), 'status': status, 'add': add, 'del': dele,
                    'edits': len(rec['ops']), 'script': tool_script(rec, ch), 'last': rec['last'],
                    'time': max((getattr(op['when'], 'epoch', 0) for op in rec['ops']), default=0),
                    'rows': [[k, n, t[:4000]] for k, n, t in rows[:MAX_ROWS]], 'cut': len(rows) > MAX_ROWS})
        ext = os.path.splitext(rec['path'])[1].lower()
        if status == 'A' and ext in WORKBOOK_EXTENSIONS | TEXT_FORMATS:
            try:
                out[-1].update(compare(rec['path'], None, read_bytes(rec['path'])))
                out[-1].update({'structured': True, 'label': 'New file in this session'})
            except Exception as e:
                out[-1]['rows'].insert(0, ['', None, f'Structured comparison unavailable: {e}'])
        elif status == 'M' and ext in TEXT_FORMATS:
            try:
                current = read_bytes(rec['path'])
                before = restore_text_baseline(current, rec['ops'])
                out[-1].update(compare(rec['path'], before, current))
                out[-1].update({'structured': True, 'label': 'Session edits → current file'})
            except (OSError, ValueError, RecursionError):
                pass  # preserve the recorded text diff when reconstruction cannot be verified
    return out


# ---------- Unreal assets ----------
# Blueprints and maps change inside the editor (MCP, editor scripts), never through an edit tool:
# the conversation only names them. Every asset it names that changed on disk since it began counts.

GAME_PATH = re.compile(r'/Game/[A-Za-z0-9_\-/]+(?:\.[A-Za-z0-9_]+)?')
UPROJECT = re.compile(r'[A-Za-z]:(?:\\\\|\\|/)[^"\'\s*?<>|]*?\.uproject', re.I)


def session_start(paths):
    for p in paths:
        for i, o in enumerate(read_lines(p)):
            ts = o.get('timestamp') or (o.get('payload') or {}).get('timestamp')
            if ts:
                e = Stamp(ts).epoch
                if e:
                    return e
            if i > 30:
                break
    return min((os.path.getctime(p) for p in paths if os.path.exists(p)), default=0)


def content_roots(text, cwd, ch):
    projects = set()
    for m in UPROJECT.findall(text):
        projects.add(m.replace('\\\\', '\\'))
    bases = set()
    for p in [cwd] + [r['path'] for r in ch.files.values()]:
        d = os.path.dirname(p) if p != cwd else p
        for _ in range(8):
            if not d or d in bases:
                break
            bases.add(d)
            d = os.path.dirname(d) if os.path.dirname(d) != d else None
    for b in bases:
        projects.update(glob.glob(os.path.join(b, '*.uproject')) + glob.glob(os.path.join(b, '*', '*.uproject')))
    if cwd:
        projects.update(glob.glob(os.path.join(cwd, '*', '*', '*.uproject')))
    roots = []
    for p in projects:
        c = os.path.join(os.path.dirname(p), 'Content')
        if os.path.isdir(c) and norm(c) not in [norm(r) for r in roots]:
            roots.append(c)
    return roots


WRITE = re.compile(r'save_asset|save_loaded|save_package|savepackage|save_directory|saveasset|compile_blueprint|compileblueprint|'
                   r'mark_package_dirty|markpackagedirty|set_editor_propert|add_function|add_node|addnode|create_asset|duplicate_asset|'
                   r'rename_asset|delete_asset|connect_pins|connect_blueprint_pins|connectpins|add_variable|addvariable|add_member|'
                   r'set_pin_default|make_link|link_pins|spawn_node|add_component|import_asset|\.modify\(', re.I)
CACHE = os.path.join(os.environ.get('TEMP', HOME), 'herdr-session-changes-writes.json')


def write_calls(ch):
    """[(start, text)] of calls that change editor content; a script this conversation wrote
    counts as part of the call that runs it."""
    written = {}
    for rec in ch.files.values():
        if rec['created']:
            written[os.path.basename(rec['path']).lower()] = '\n'.join(
                r[2] for op in rec['ops'] for r in op['rows'] if r[0] == '+').lower()
    out = []
    for t0, _t1, text in ch.calls:
        low = text.lower()
        full = '\n'.join([low] + [body for base, body in written.items() if base in low])
        if WRITE.search(full):
            out.append((t0, full[:300000]))
    return out


def read_any(path, ch):
    n = norm(path)
    if os.sep + '.claude' + os.sep in n:
        read_claude([path], ch)
    elif os.sep + '.codex' + os.sep in n:
        read_codex(path, ch)
    else:
        read_pi(path, ch)


def other_writes(mine, since):
    """Write calls of every other conversation still active after `since` (cached per transcript)."""
    if since is None:
        return []
    files = glob.glob(os.path.join(HOME, '.claude', 'projects', '*', '*.jsonl'))
    files += glob.glob(os.path.join(HOME, '.claude', 'projects', '*', '*', 'subagents', '*.jsonl'))
    d = datetime.date.fromtimestamp(since - 86400)
    while d <= datetime.date.today():
        files += glob.glob(os.path.join(HOME, '.codex', 'sessions', f'{d:%Y}', f'{d:%m}', f'{d:%d}', 'rollout-*.jsonl'))
        d += datetime.timedelta(days=1)
    try:
        import sqlite3  # resumed Codex threads keep writing to older rollouts
        db = sorted(glob.glob(os.path.join(HOME, '.codex', 'state_*.sqlite')))[-1]
        con = sqlite3.connect('file:' + db.replace('\\', '/') + '?mode=ro', uri=True, timeout=2)
        files += [r[0][4:] if r[0].startswith('\\\\?\\') else r[0] for r in
                  con.execute('select rollout_path from threads where updated_at_ms >= ?', (int(since * 1000),))]
        con.close()
    except Exception:
        pass
    files += glob.glob(os.path.join(HOME, '.pi', 'agent', 'sessions', '*', '*.jsonl'))
    skip = {norm(p) for p in mine}
    try:
        cache = json.load(open(CACHE, encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    fresh, out = {}, []
    for f in {norm(x): x for x in files}.values():
        try:
            st = os.stat(f)
        except OSError:
            continue
        if norm(f) in skip or st.st_mtime < since:
            continue
        sig = f'{st.st_size}:{st.st_mtime}'
        hit = cache.get(f)
        if not hit or hit.get('sig') != sig:
            ch = Changes()
            try:
                read_any(f, ch)
            except Exception:
                continue
            hit = {'sig': sig, 'calls': [[t0, text[:100000]] for t0, text in write_calls(ch)]}
        fresh[f] = hit
        out += [tuple(c) for c in hit['calls']]
    try:
        tmp = CACHE + '.tmp'
        json.dump(fresh, open(tmp, 'w', encoding='utf-8'))
        os.replace(tmp, CACHE)
    except OSError:
        pass
    return out


def ue_assets(paths, cwd, ch):
    text = ''
    for p in paths:
        try:
            text += open(p, encoding='utf-8', errors='replace').read()
        except OSError:
            pass
    names = set(GAME_PATH.findall(text))
    if not names:
        return []
    roots = content_roots(text, cwd, ch)
    if not roots:
        return []
    start = session_start(paths) - 60
    found = {}
    def consider(f):
        try:
            st = os.stat(f)
        except OSError:
            return
        if st.st_mtime >= start and norm(f) not in found:
            found[norm(f)] = (f, st)
    for name in names:
        rest = name[len('/Game/'):].rstrip('/')
        if not rest or 'UEDPIE' in rest:
            continue
        rest = rest.split('.')[0]
        for root in roots:
            base = os.path.join(root, *rest.split('/'))
            hit = False
            for ext in ('.uasset', '.umap'):
                if os.path.isfile(base + ext):
                    consider(base + ext); hit = True
            # A folder it worked in: assets it may have made there.
            if not hit and os.path.isdir(base):
                for f in sorted(os.listdir(base))[:300]:
                    if f.endswith(('.uasset', '.umap')):
                        consider(os.path.join(base, f))
    # Which conversation saved it: the one that last wrote to it (compile, save, add node, ...)
    # before the file changed. Reading or exporting it doesn't count.
    mine = write_calls(ch)
    candidates = []
    for f, st in found.values():
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        last = max((t0 for t0, text in mine if t0 - 5 <= st.st_mtime and stem in text), default=None)
        if len(stem) >= 4 and last is not None:
            candidates.append((f, st, stem, last))
    rivals = other_writes(paths, min((c[3] for c in candidates), default=None))
    out = []
    for f, st, stem, last in candidates:
        if any(last < t0 <= st.st_mtime + 5 and stem in text for t0, text in rivals):
            continue  # another conversation wrote it after this one did
        rel = f
        for base in [cwd] + [os.path.dirname(os.path.dirname(r)) for r in roots]:
            try:
                r = os.path.relpath(f, base)
                if not r.startswith('..'):
                    rel = r
                    break
            except ValueError:
                pass
        when = datetime.datetime.fromtimestamp(st.st_mtime).strftime('%H:%M:%S')
        rows = [['@', None, '@@ Unreal asset @@'], ['', None, 'Binary asset, no text diff'],
                ['', None, f'Saved {when} · {st.st_size / 1024:,.0f} KB'],
                ['', None, 'Saved by a call in this conversation']]
        out.append({'path': f, 'rel': rel.replace('\\', '/'), 'status': 'M', 'add': 0, 'del': 0,
                    'edits': 0, 'script': None, 'asset': True, 'last': -1, 'time': st.st_mtime,
                    'rows': rows, 'cut': False})
    return out[:500]


def main():
    pid, cwd = int(sys.argv[1]), sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else ''
    signature = sys.argv[4] if len(sys.argv) > 4 else ''
    app, selected, pinned = (sys.argv[5:] + ['', '', ''])[:3]
    if sys.platform not in ('win32', 'linux'):
        print(json.dumps({'error': 'Session changes supports local Windows and Linux agents'}))
        return
    procs = processes()
    mine = descendants(procs, pid)
    if sys.platform == 'linux':
        if pid in procs:
            # Herdr can exec the agent directly, making it the terminal's root process.
            mine.insert(0, pid)
    else:
        for p in mine:
            procs[p][2] = start_time(p)
    kind = 'claude'
    found = None
    if app in ('', 'dsh-tui'):
        found = deepseek_changes.mounted_session({pid, *mine}, title, selected, pinned == 'true')
        if found and 'error' in found:
            print(json.dumps(found, ensure_ascii=False))
            return
        if found:
            kind = 'deepseek'
        elif app == 'dsh-tui':
            print(json.dumps({'error': 'No DeepSeek conversation mounted in this terminal'}))
            return
    if not found:
        found = claude_transcripts(mine)
    if not found:
        names = {p: procs[p][1] for p in mine}
        codex_pids = [p for p, name in names.items() if name.startswith('codex')]
        start_pids = mine
        if sys.platform == 'linux':
            start_pids = codex_pids or [p for p, name in names.items() if linux_pi(p, name)]
        start = min((procs[p][2] for p in start_pids if procs[p][2]), default=None)
        if codex_pids:
            kind = 'codex'
            hit = codex_by_title(title, start)
            t, folder = hit or (None, cwd)
            if not t and sys.platform == 'linux':
                t = linux_codex_rollout(codex_pids)
            if not t and start:
                others = [v[2] or start_time(p) or 0 for p, v in procs.items()
                          if v[1].startswith('codex') and p not in mine]
                t = codex_transcript(start, cwd, others)
            found = t and {'agent': 'Codex', 'session': os.path.basename(t)[-42:-6], 'transcripts': [t], 'cwd': folder or cwd}
        elif start:
            t = pi_transcript(start, cwd, [])
            if t:
                kind = 'pi'
                found = {'agent': 'pi', 'session': os.path.basename(t).split('_')[-1][:-6], 'transcripts': [t], 'cwd': cwd}
    if not found:
        print(json.dumps({'error': 'No conversation found for this terminal'}))
        return
    try:
        sig = found['session'] + '|' + '|'.join(
            f'{p}:{os.path.getsize(p)}:{os.stat(p).st_mtime_ns}' for p in found['transcripts'])
        sig += '|' + bridge_signature(found['session'])
    except OSError:
        print(json.dumps({'error': 'Session log changed during refresh'}))
        return
    if sig == signature:
        response = {'same': True}
        if 'choices' in found:
            response['choices'] = found['choices']
        print(json.dumps(response, ensure_ascii=False))
        return
    ch = Changes()
    for t in found['transcripts']:
        if kind == 'claude':
            read_claude([t], ch)
        elif kind == 'codex':
            read_codex(t, ch)
        elif kind == 'deepseek':
            try:
                cwd = deepseek_changes.read_changes(t, found['session'], ch, stamp)
            except (OSError, EOFError, ValueError) as e:
                print(json.dumps({'error': f'Cannot read DeepSeek session: {e}'}, ensure_ascii=False))
                return
        else:
            read_pi(t, ch)
    root = found.get('cwd') or cwd
    registered = load_records(found['session'], root)
    tracked = {norm(f['source_path']) for f in registered if f['scope'] == 'workspace'}
    files = [f for f in build(ch, root) if norm(f['path']) not in tracked]
    files += registered
    if kind != 'deepseek':
        files += ue_assets(found['transcripts'], root, ch)
    files.sort(key=lambda f: (-f['time'], -f['last']))
    print(json.dumps({'agent': found['agent'], 'session': found['session'], 'name': found.get('name'),
                      'choices': found.get('choices', []),
                      'cwd': root, 'signature': sig, 'files': files}, ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
