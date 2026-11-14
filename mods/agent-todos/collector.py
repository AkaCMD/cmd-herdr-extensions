"""Local, read-only agent session adapters. Emits only task data, never env/credentials.
psutil is isolated in this mod's vendor folder. Cache contains normalized tasks only.
"""
import hashlib, json, os, re, sys, time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / 'vendor'))
import psutil
HOME = Path.home()
APP = Path(os.environ.get('HERDR_APP_HOME', HOME / '.config/herdr-app'))
DATA = APP / 'data/agent-todos'
DSH = Path(os.environ.get('DSH_HOME', HOME / '.dsh'))
DSH_TUI = Path(os.environ.get('DSH_TUI_HOME', HOME / '.dsh-tui'))
OPENCODE_DB = Path(os.environ.get('OPENCODE_DATA_HOME', HOME / '.local/share/opencode')) / 'opencode.db'
# Live status glyphs the agents put in front of their terminal title.
SPINNER = re.compile(r'^[\u2800-\u28ff●○◉✓✔✗✕⏺⏵⏸\s]+')


def load(path, default=None):
    try: return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError): return default


def clean_title(title):
    return SPINNER.sub('', title or '').strip()


def normalized(tasks):
    return [dict(id=t.get('id', i + 1), subject=str(t.get('subject', t.get('content', t.get('step', '')))),
                 status={'inProgress': 'in_progress'}.get(t.get('status'), t.get('status', 'pending')), activeForm=t.get('activeForm', ''),
                 blockedBy=t.get('blockedBy', []), description=t.get('description', ''))
            for i, t in enumerate(tasks) if isinstance(t, dict) and t.get('status') != 'deleted']


def record_request(state, identity, content):
    if isinstance(content, list):
        content = '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') in ('text', 'input_text'))
    if not isinstance(content, str) or not content.strip(): return
    if content.lstrip().startswith(('[herdr-todo-request]', '请根据当前对话中最近一项明确的工作生成 Todo 清单。',
                                   '<environment_context>', '# AGENTS.md instructions', '<turn_aborted>')): return
    state['request'] = hashlib.sha256(json.dumps([identity, content], ensure_ascii=False).encode()).hexdigest()


def apply_record(state, e, kind):
    message = e.get('message') or {}
    payload = e.get('payload') or {}
    if kind == 'codex' and e.get('type') == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'user':
        record_request(state, payload.get('id') or e.get('timestamp'), payload.get('content'))
        state['response_requests'] = True
    elif kind == 'codex' and not state.get('response_requests') and e.get('type') == 'event_msg' and payload.get('type') == 'user_message':
        record_request(state, e.get('timestamp'), payload.get('message'))
    elif kind == 'dsh' and e.get('type') == 'user/message':
        record_request(state, e.get('seq'), (e.get('data') or {}).get('content'))
    elif kind in ('pi', 'claude') and message.get('role') == 'user' and not e.get('isMeta'):
        record_request(state, e.get('id') or e.get('uuid') or e.get('timestamp'), message.get('content'))
    if kind == 'dsh':
        data = e.get('data') or {}
        if e.get('type') == 'todo/write' and isinstance(data.get('todos'), list):
            state['tasks'] = normalized(data['todos']); state['plan_seen'] = True
            state['taskRequest'] = state.get('request')
        if e.get('type') == 'tool/call' and data.get('name') == 'todo_write':
            state['calls'][data['callId']] = True
        if e.get('type') == 'tool/result':
            result = data.get('message') or {}
            if state['calls'].pop(result.get('toolCallId'), None) and not result.get('isError') and not data.get('error'):
                state['taskRequest'] = state.get('request')
        return
    if kind == 'pi':
        if e.get('customType') == 'rpiv-todo-state' and isinstance(e.get('data', {}).get('tasks'), list):
            state['tasks'] = normalized(e['data']['tasks']); state['authoritative'] = True
            state['taskRequest'] = state.get('request')
        m = e.get('message', {})
        if not state.get('authoritative') and m.get('toolName') == 'todo' and isinstance((m.get('details') or {}).get('tasks'), list):
            state['tasks'] = normalized(m['details']['tasks']); state['taskRequest'] = state.get('request')
        return
    if kind == 'codex':
        p = e.get('payload', {})
        if e.get('method') == 'turn/plan/updated' and isinstance((e.get('params') or {}).get('plan'), list):
            state['tasks'] = normalized(e['params']['plan']); state['plan_seen'] = True
            state['taskRequest'] = state.get('request'); state['plan_time'] = e.get('timestamp')
        if e.get('type') == 'event_msg' and p.get('type') == 'plan_update' and isinstance(p.get('plan'), list):
            state['tasks'] = normalized(p['plan']); state['plan_seen'] = True
            state['taskRequest'] = state.get('request')
            state['plan_time'] = e.get('timestamp')
        if p.get('type') == 'function_call' and str(p.get('name', '')).split('.')[-1] == 'update_plan':
            try: state['calls'][p['call_id']] = json.loads(p['arguments'])
            except (ValueError, KeyError, TypeError): pass
        if p.get('type') == 'function_call_output':
            call = state['calls'].pop(p.get('call_id'), None)
            if call and 'plan' in call and 'error' not in str(p.get('output', '')).lower():
                state['tasks'] = normalized(call['plan']); state['plan_seen'] = True
                state['taskRequest'] = state.get('request')
                state['plan_time'] = e.get('timestamp')
        return
    content = e.get('message', {}).get('content', [])
    if not isinstance(content, list): return
    for b in content:
        if not isinstance(b, dict): continue
        if b.get('type') == 'tool_use' and b.get('name') in ['TodoWrite', 'TaskCreate', 'TaskUpdate', 'TaskList', 'TaskGet']:
            state['calls'][b['id']] = {'name': b['name'], 'input': b.get('input', {})}
        if b.get('type') != 'tool_result': continue
        call = state['calls'].pop(b.get('tool_use_id'), None)
        if not call or b.get('is_error'): continue
        name, arg = call['name'], call['input']
        if name not in ('TaskList', 'TaskGet'): state['taskRequest'] = state.get('request')
        if name == 'TodoWrite': state['tasks'] = normalized(arg.get('todos', []))
        elif name in ('TaskList', 'TaskGet'):
            result = b.get('content', '')
            if isinstance(result, list): result = '\n'.join(x.get('text', '') for x in result if isinstance(x, dict))
            try:
                parsed = json.loads(result)
                if name == 'TaskList':
                    tasks = parsed if isinstance(parsed, list) else parsed.get('tasks')
                    if isinstance(tasks, list): state['tasks'] = normalized(tasks)
                else:
                    task = parsed.get('task', parsed)
                    if isinstance(task, dict) and task.get('id') is not None:
                        state['tasks'] = normalized([t for t in state['tasks'] if str(t['id']) != str(task['id'])] + [task])
            except (ValueError, TypeError, AttributeError): pass
        elif name == 'TaskCreate':
            result = b.get('content', '')
            if isinstance(result, list): result = '\n'.join(x.get('text', '') for x in result if isinstance(x, dict))
            match = re.search(r'(?:Task\s*#?|"id"\s*:\s*"?)(\d+)', str(result), re.I)
            if match:
                task = dict(arg, id=match.group(1), status='pending')
                state['tasks'].append(normalized([task])[0])
        elif name == 'TaskUpdate':
            for task in state['tasks']:
                if str(task['id']) == str(arg.get('taskId')):
                    for key in ['status', 'subject', 'description', 'activeForm', 'owner']:
                        if key in arg: task[key] = arg[key]
                    deps = task.get('blockedBy', [])
                    task['blockedBy'] = [d for d in deps if d not in arg.get('removeBlockedBy', [])] + arg.get('addBlockedBy', [])
            state['tasks'] = normalized(state['tasks'])


@contextmanager
def open_records(path):
    if path.suffix == '.zstd':
        try:
            from compression import zstd
        except ImportError:
            import subprocess
            with subprocess.Popen(['zstd', '-dc', '--', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
                try:
                    yield proc.stdout
                finally:
                    proc.stdout.close()
                    proc.wait()
                if proc.returncode: raise OSError('DeepSeek log decompression failed')
        else:
            with zstd.open(path, 'rb') as stream: yield stream
    else:
        with path.open('rb') as stream: yield stream


def read_task_state(path, kind):
    path = Path(path)
    # Stable cache name across Python processes.
    cache = DATA / ('cache-' + hashlib.sha256(str(path).encode()).hexdigest()[:24] + '.json')
    stat = path.stat()
    state = load(cache, {})
    identity = [stat.st_dev, stat.st_ino]
    if state.get('adapter_version') != 5 or state.get('file_size', 0) > stat.st_size or state.get('identity') != identity:
        state = {}
    state.setdefault('tasks', []); state.setdefault('calls', {}); state.setdefault('size', 0)
    if state.get('mtime') != stat.st_mtime_ns or state.get('file_size') != stat.st_size:
        # A compressed stream uses decompressed offsets. Rebuild it only when its file changes.
        if path.suffix == '.zstd': state = {'tasks': [], 'calls': {}, 'size': 0}
        with open_records(path) as f:
            f.seek(state['size'])
            while True:
                pos = f.tell(); line = f.readline()
                if not line or not line.endswith(b'\n'): state['size'] = pos; break
                state['size'] = f.tell()
                try: apply_record(state, json.loads(line), kind)
                except (ValueError, TypeError, KeyError): continue
        state.update(mtime=stat.st_mtime_ns, identity=identity, file_size=stat.st_size, adapter_version=5)
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(f'.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8'); tmp.replace(cache)
    return state


def read_tasks(path, kind):
    return normalized(read_task_state(path, kind)['tasks'])


def codex_thread(title, open_paths=()):
    """Codex titles its terminal '<thread name> | <folder>'; the state db maps that to its rollout."""
    name = clean_title(title).rsplit(' | ', 1)[0].strip()
    if not name: return None
    db = HOME / '.codex/state_5.sqlite'
    if not db.is_file():
        found = sorted((HOME / '.codex').glob('state_*.sqlite'))
        if not found: return None
        db = found[-1]
    try:
        import sqlite3
        con = sqlite3.connect('file:' + str(db).replace('\\', '/') + '?mode=ro', uri=True, timeout=2)
        try:
            rows = con.execute('select rollout_path from threads where name = ? or title = ? '
                               'order by updated_at_ms desc', (name, name)).fetchall()
        finally:
            con.close()
    except Exception:
        return None
    found = []
    for (path,) in rows:
        path = path[4:] if path and path.startswith('\\\\?\\') else path
        if path and Path(path).is_file() and path not in found: found.append(path)
    # Same thread name in another terminal: the rollout this terminal holds open wins.
    wanted = {os.path.normcase(os.path.abspath(p)) for p in open_paths}
    for path in found:
        if os.path.normcase(os.path.abspath(path)) in wanted: return path
    return found[0] if found else None


def codex_tasks(path, terminal):
    state = read_task_state(path, 'codex')
    session = Path(path).stem[-36:]
    (DATA / 'codex').mkdir(parents=True, exist_ok=True)
    watch_files = [str(path), str(DATA / 'codex' / (session + '.json'))]
    bridge = load(DATA / 'codex' / (session + '.json'), {})
    try:
        native_time = datetime.fromisoformat(state.get('plan_time', '').replace('Z', '+00:00')).timestamp()
        newer_bridge = float(bridge.get('updatedAt', 0)) > native_time
    except (ValueError, TypeError, AttributeError):
        newer_bridge = False
    if not state.get('plan_seen') or newer_bridge:
        if bridge.get('sessionId') == session and bridge.get('terminal') == terminal and isinstance(bridge.get('plan'), list):
            return {'tasks': normalized(bridge['plan']), 'session': session, 'source': 'Codex / 本地 plan 桥接',
                    'request': state.get('request'), 'taskRequest': bridge.get('request'), 'watchFiles': watch_files}
    return {'tasks': normalized(state['tasks']), 'session': session, 'source': 'Codex / update_plan',
            'request': state.get('request'), 'taskRequest': state.get('taskRequest'), 'watchFiles': watch_files}


def dsh_sessions(processes):
    """dsh-tui registers the sessions it mounts per process pid; the terminal is that process."""
    pids = {p.pid for p in processes}
    raw = []
    ledger = load(DSH_TUI / 'session-mounts.json', {}) or {}
    for owner in ledger.get('owners') or []:
        if isinstance(owner, dict) and owner.get('pid') in pids:
            raw += [s for s in owner.get('sessionIds') or [] if isinstance(s, str)]
    if not raw:
        for server in load(DSH_TUI / 'inject/servers.json', []) or []:
            if isinstance(server, dict) and server.get('pid') in pids and isinstance(server.get('sessionId'), str):
                raw.append(server['sessionId'])
    sessions = []
    for entry in raw:
        session = entry.split(':', 1)[1] if ':' in entry else entry  # ledger keys may be '<backend>:<id>'
        if re.fullmatch(r'[\w-]+', session) and session not in sessions: sessions.append(session)
    return sessions


def dsh_state(session):
    data = load(DSH / 'storages/session_projcache/sessions' / (session + '.json'), {}) or {}
    rows = (data.get('record') or {}).get('rows') or {}
    todos = (rows.get('todos') or {}).get('val')
    if isinstance(todos, dict): todos = todos.get('todos', todos.get('tasks'))
    title = (rows.get('title') or {}).get('val')
    return (todos if isinstance(todos, list) else []), (title if isinstance(title, str) else '').strip()


def dsh_pick(sessions, titles, title):
    shown = clean_title(title)
    if not shown: return None
    hits = [s for s in sessions if titles.get(s) and titles[s] in shown]
    return max(hits, key=lambda s: len(titles[s])) if hits else None


def dsh_collect(processes, selected=None, title=None):
    if not processes: return {'tasks': [], 'message': '未找到此终端的 DeepSeek Harness 进程；远程会话尚未接入。'}
    sessions = dsh_sessions(processes)
    if not sessions: return {'tasks': [], 'message': '未找到此终端的 DeepSeek 会话；需要 dsh-tui 的 ~/.dsh-tui/session-mounts.json 登记。'}
    states = {s: dsh_state(s) for s in sessions}
    titles = {s: states[s][1] for s in sessions}
    choices = [{'path': s, 'name': titles[s] or s[:8]} for s in sessions]
    auto = dsh_pick(sessions, titles, title)
    picked = auto or (selected if selected in sessions else None) or (sessions[0] if len(sessions) == 1 else None)
    if not picked:
        return {'tasks': [], 'message': '此终端打开了多个 DeepSeek 会话，请选择对应会话。', 'choices': choices}
    todos, _ = states[picked]
    request = {}
    logs = list((DSH / 'sessions').glob(f'*/{picked}/session.v4.jsonl*'))
    logs = [p for p in logs if p.name in ('session.v4.jsonl', 'session.v4.jsonl.zstd')]
    if logs:
        try:
            state = read_task_state(sorted(logs, key=lambda p: p.suffix != '.zstd')[0], 'dsh')
            request = {k: state.get(k) for k in ('request', 'taskRequest')}
            if state.get('plan_seen'): todos = state['tasks']
        except OSError:
            # Older Python installations can still display the projection without a zstd reader.
            pass
    return {'tasks': normalized(todos), 'session': picked, 'source': 'DeepSeek / 会话 todo',
            'choices': [] if auto else choices, **request,
            'watchFiles': [str(DSH / 'storages/session_projcache/sessions' / (picked + '.json')), *map(str, logs)]}


def opencode_request(con, session):
    """Read user revisions on database versions with message/part records."""
    import sqlite3
    state = {}
    try:
        messages = con.execute('select id, time_created, data from message where session_id = ? order by time_created desc, id desc', (session,))
        for mid, created, raw in messages:
            message = json.loads(raw)
            if message.get('role') != 'user': continue
            parts = con.execute('select data from part where message_id = ? order by id', (mid,)).fetchall()
            record_request(state, mid, [json.loads(p[0]) for p in parts])
            if not state.get('request'): continue
            updated = con.execute('select max(time_updated) from todo where session_id = ?', (session,)).fetchone()[0]
            if updated is not None and updated >= created: state['taskRequest'] = state['request']
            break
    except (sqlite3.Error, ValueError, TypeError):
        return {}
    return state


def opencode_collect(processes, title=None):
    """OpenCode keeps its todos in a local SQLite table, keyed by session. The
    running terminal maps to the active session whose directory matches its cwd."""
    if not processes:
        return {'tasks': [], 'message': '未找到此终端的 OpenCode 进程。'}
    cwds = set()
    for p in processes:
        try:
            cwd = p.cwd()
        except (psutil.Error, OSError):
            continue
        if cwd:
            cwds.add(os.path.normcase(os.path.abspath(cwd)))
    if not cwds:
        return {'tasks': [], 'message': '无法读取 OpenCode 进程的工作目录。'}
    if not OPENCODE_DB.is_file():
        return {'tasks': [], 'message': '未找到 OpenCode 会话数据库。'}
    import sqlite3
    try:
        con = sqlite3.connect('file:' + str(OPENCODE_DB).replace('\\', '/') + '?mode=ro', uri=True, timeout=2)
    except Exception:
        return {'tasks': [], 'message': '无法打开 OpenCode 会话数据库。'}
    try:
        # Prefer the live (non-idle) session; fall back to the most recent in this directory.
        rows = con.execute(
            'select id, directory from session_v2 where time_archived is null '
            'order by case when time_idle is null then 0 else 1 end, time_updated desc'
        ).fetchall()
        session = None
        for sid, directory in rows:
            if directory and os.path.normcase(os.path.abspath(directory)) in cwds:
                session = sid
                break
        if session is None:
            return {'tasks': [], 'message': '未找到匹配此终端的 OpenCode 会话。'}
        todos = con.execute(
            'select content, status from todo where session_id = ? order by position, time_created', (session,)
        ).fetchall()
        revision = opencode_request(con, session)
    except Exception:
        return {'tasks': [], 'message': '读取 OpenCode 会话数据失败。'}
    finally:
        con.close()
    tasks = normalized([{'subject': c, 'status': s} for c, s in todos if s != 'cancelled'])
    return {'tasks': tasks, 'session': session, 'source': 'OpenCode / todowrite', **revision,
            'watchFiles': [str(OPENCODE_DB), str(OPENCODE_DB) + '-wal']}


def collect(terminal, kind, selected=None, title=None):
    if '/' in terminal: return {'message': '第一版仅支持本机代理；远程会话尚未接入。', 'tasks': []}
    if kind not in ['pi', 'claude-code', 'codex', 'dsh-tui', 'opencode']: return {'message': '此代理暂不支持 todo 数据。', 'tasks': []}
    processes = []
    for p in psutil.process_iter(['pid', 'name']):
        try:
            name = (p.info['name'] or '').lower().removesuffix('.exe')
            if name not in {'node', 'claude', 'codex', 'pi', 'dsh-tui', 'opencode'}: continue
            if p.environ().get('HC_TERMINAL_ID') != terminal: continue
            argv = p.cmdline()
            if kind == 'pi' and (name == 'pi' or
                                 (name == 'node' and any(Path(a).name == 'pi' or
                                                         ('pi-coding-agent' in a and 'cli.js' in a)
                                                         for a in argv[:3]))):
                processes.append(p)
            elif kind == 'claude-code' and name == 'claude' and '--print' not in argv and '-p' not in argv:
                processes.append(p)
            elif kind == 'codex' and name == 'codex' and 'exec' not in argv[1:3] and 'app-server' not in argv[1:3]:
                processes.append(p)
            elif kind == 'dsh-tui' and (name == 'dsh-tui' or
                                      (name == 'node' and any('dsh-tui' in a for a in argv[1:]))):
                processes.append(p)
            elif kind == 'opencode' and name == 'opencode':
                processes.append(p)
        except (psutil.Error, OSError): continue
    if kind == 'dsh-tui': return dsh_collect(processes, selected, title)
    if kind == 'opencode': return opencode_collect(processes, title)
    if kind == 'pi':
        bridge = load(DATA / 'pi' / (terminal + '.json'), {})
        if bridge.get('pid') not in [p.pid for p in processes]:
            return {'tasks': [], 'message': '请在此 Pi 会话执行 /reload，以启用实时 todo 桥接。'}
        if 'tasks' in bridge:
            return {'tasks': normalized(bridge['tasks']), 'session': bridge.get('sessionId'), 'source': 'Pi / todo',
                    'request': bridge.get('request'), 'taskRequest': bridge.get('taskRequest'),
                    'watchFiles': [str(DATA / 'pi' / (terminal + '.json'))]}
        # Bootstrap for the currently installing session only; extension replaces this after /reload.
        return {'tasks': read_tasks(bridge['sessionFile'], 'pi'), 'session': bridge.get('sessionId'), 'source': 'Pi / rpiv-todo', 'message': '已连接当前会话；完成后 /reload 启用分支切换同步。'}
    paths = set()
    native_task_dirs = set()
    for p in processes:
        try:
            if kind == 'claude-code':
                info = load(HOME / '.claude/sessions' / (str(p.pid) + '.json'), {})
                if info.get('sessionId'):
                    paths.update((HOME / '.claude/projects').glob('*/' + info['sessionId'] + '.jsonl'))
                    task_id = p.environ().get('CLAUDE_CODE_TASK_LIST_ID') or info['sessionId']
                    if re.fullmatch(r'[\w-]+', task_id): native_task_dirs.add(HOME / '.claude/tasks' / task_id)
            else:
                paths.update(Path(f.path) for f in p.open_files() if 'rollout-' in f.path and f.path.endswith('.jsonl'))
        except (psutil.Error, OSError): continue
    task_dirs = [p for p in native_task_dirs if p.is_dir()]
    if kind == 'claude-code' and len(task_dirs) == 1:
        tasks = [load(p) for p in task_dirs[0].glob('*.json')]
        state = read_task_state(next(iter(paths)), 'claude') if len(paths) == 1 else {}
        return {'tasks': normalized([t for t in tasks if isinstance(t, dict) and 'subject' in t]), 'session': task_dirs[0].name, 'source': 'Claude / Tasks',
                'request': state.get('request'), 'taskRequest': state.get('taskRequest'),
                'watchFiles': [str(task_dirs[0] / '*'), *map(str, paths)]}
    choices = [{'path': str(p), 'name': p.stem[-36:]} for p in sorted(paths)]
    picked = codex_thread(title, paths) if kind == 'codex' else None
    if selected not in [x['path'] for x in choices]: selected = None
    if picked: path = Path(picked)
    elif selected: path = Path(selected)
    elif len(paths) == 1: path = next(iter(paths))
    elif len(paths) > 1:
        return {'tasks': [], 'message': '此终端打开了多个 Codex 会话，且终端标题未能确定当前会话；请选择对应会话（不按修改时间猜测）。', 'choices': choices}
    else: path = None
    if path is None: return {'tasks': [], 'message': '未能精确关联此代理的会话文件。', 'choices': choices}
    # An auto-resolved thread hides the picker; a manual pick keeps the list available.
    visible = [x for x in choices if x['path'] == str(path)] if picked else choices
    if kind == 'codex': return dict(codex_tasks(path, terminal), choices=visible)
    state = read_task_state(path, 'claude')
    return {'tasks': normalized(state['tasks']), 'session': path.stem[-36:], 'source': 'Claude / Tasks · TodoWrite', 'choices': visible,
            'request': state.get('request'), 'taskRequest': state.get('taskRequest'), 'watchFiles': [str(path)]}


if __name__ == '__main__':
    try: result = collect(*sys.argv[1:])
    except Exception as e: result = {'tasks': [], 'error': f'{type(e).__name__}: {e}'}
    print(json.dumps(result, ensure_ascii=False))
