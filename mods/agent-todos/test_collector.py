import unittest
from collector import apply_record, collect, normalized, read_tasks, codex_tasks, clean_title, codex_thread, dsh_collect, opencode_collect
from codex_plan import write_plan
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import sqlite3

class Adapters(unittest.TestCase):
    def state(self): return {'tasks': [], 'calls': {}}

    def test_pi_process_names_on_windows_and_linux(self):
        cases = [
            ('node.exe', ['node.exe', r'C:\pi-coding-agent\dist\cli.js']),
            ('node', ['node', '/usr/local/bin/pi']),
            ('pi', ['pi']),
        ]
        with TemporaryDirectory() as d, patch('collector.DATA', Path(d)):
            (Path(d) / 'pi').mkdir()
            (Path(d) / 'pi/t1.json').write_text(
                json.dumps({'pid': 42, 'sessionId': 'pi-test', 'tasks': [{'subject': 'task'}]}), encoding='utf-8')
            for name, argv in cases:
                with self.subTest(name=name):
                    process = SimpleNamespace(pid=42, info={'name': name}, cmdline=lambda: argv,
                                              environ=lambda: {'HC_TERMINAL_ID': 't1'})
                    with patch('collector.psutil.process_iter', return_value=[process]):
                        self.assertEqual(collect('t1', 'pi')['session'], 'pi-test')
                        self.assertEqual(collect('t2', 'pi')['tasks'], [])
    def test_pi_authoritative_and_clear(self):
        s = self.state()
        apply_record(s, {'customType': 'rpiv-todo-state', 'data': {'tasks': [{'id': 1, 'subject': 'a', 'status': 'in_progress'}]}}, 'pi')
        apply_record(s, {'message': {'toolName': 'todo', 'details': {'tasks': []}}}, 'pi')
        self.assertEqual(len(s['tasks']), 1)
        apply_record(s, {'customType': 'rpiv-todo-state', 'data': {'tasks': []}}, 'pi')
        self.assertEqual(s['tasks'], [])
    def test_claude_success_and_failed_write(self):
        s = self.state()
        def entry(blocks): return {'message': {'content': blocks}}
        call = {'type': 'tool_use', 'id': 'a', 'name': 'TodoWrite', 'input': {'todos': [{'content': '中文任务', 'status': 'pending'}]}}
        apply_record(s, entry([call]), 'claude')
        self.assertEqual(s['tasks'], [])
        apply_record(s, entry([{'type': 'tool_result', 'tool_use_id': 'a', 'is_error': True}]), 'claude')
        self.assertEqual(s['tasks'], [])
        call['id'] = 'b'; apply_record(s, entry([call]), 'claude')
        apply_record(s, entry([{'type': 'tool_result', 'tool_use_id': 'b', 'content': 'ok'}]), 'claude')
        self.assertEqual(s['tasks'][0]['subject'], '中文任务')
    def test_claude_task_create_update_delete(self):
        s = self.state()
        def tool(name, arg, result):
            apply_record(s, {'message': {'content': [{'type': 'tool_use', 'id': 'x', 'name': name, 'input': arg}]}}, 'claude')
            apply_record(s, {'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': result}]}}, 'claude')
        tool('TaskCreate', {'subject': 'step'}, 'Task #2 created successfully')
        tool('TaskUpdate', {'taskId': '2', 'status': 'in_progress'}, 'ok')
        self.assertEqual(s['tasks'][0]['status'], 'in_progress')
        tool('TaskUpdate', {'taskId': '2', 'status': 'deleted'}, 'ok')
        self.assertEqual(s['tasks'], [])
    def test_codex_plan_result(self):
        s = self.state()
        apply_record(s, {'payload': {'type': 'function_call', 'name': 'functions.update_plan', 'call_id': 'p', 'arguments': json.dumps({'plan': [{'step': 'test', 'status': 'completed'}]})}}, 'codex')
        self.assertEqual(s['tasks'], [])
        apply_record(s, {'payload': {'type': 'function_call_output', 'call_id': 'p', 'output': 'Plan updated'}}, 'codex')
        self.assertEqual(s['tasks'][0]['subject'], 'test')
    def test_incremental_partial_line_and_truncation(self):
        with TemporaryDirectory() as d, patch('collector.DATA', Path(d) / 'cache'):
            p = Path(d) / 'session.jsonl'
            line = json.dumps({'customType': 'rpiv-todo-state', 'data': {'tasks': [{'id': 1, 'subject': 'x', 'status': 'pending'}]}})
            p.write_text(line, encoding='utf-8')
            self.assertEqual(read_tasks(p, 'pi'), [])
            with p.open('a') as f: f.write('\n')
            self.assertEqual(len(read_tasks(p, 'pi')), 1)
            p.write_text('{}\n', encoding='utf-8')
            self.assertEqual(read_tasks(p, 'pi'), [])
    def test_isolation(self):
        a, b = self.state(), self.state()
        apply_record(a, {'customType': 'rpiv-todo-state', 'data': {'tasks': [{'subject': 'a', 'status': 'pending'}]}}, 'pi')
        self.assertEqual(b['tasks'], [])
    def test_local_codex_plan_isolation_and_updates(self):
        session = '00000000-0000-0000-0000-000000000001'
        env = {'HC_TERMINAL_ID': 't1', 'CODEX_THREAD_ID': session}
        with TemporaryDirectory() as d, patch('collector.DATA', Path(d) / 'data/agent-todos'):
            path = Path(d) / ('rollout-test-' + session + '.jsonl')
            path.write_text('{}\n', encoding='utf-8')
            for status in ['pending', 'in_progress', 'completed']:
                write_plan({'plan': [{'step': '中文测试', 'status': status}]}, d, env)
                self.assertEqual(codex_tasks(path, 't1')['tasks'][0]['status'], status)
                self.assertEqual(codex_tasks(path, 't2')['tasks'], [])
            write_plan({'plan': []}, d, env)
            self.assertEqual(codex_tasks(path, 't1')['tasks'], [])
    def test_native_codex_plan_overrides_bridge_even_when_cleared(self):
        session = '00000000-0000-0000-0000-000000000001'
        env = {'HC_TERMINAL_ID': 't1', 'CODEX_THREAD_ID': session}
        with TemporaryDirectory() as d, patch('collector.DATA', Path(d) / 'data/agent-todos'):
            path = Path(d) / ('rollout-test-' + session + '.jsonl')
            write_plan({'plan': [{'step': 'bridge', 'status': 'pending'}]}, d, env)
            path.write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'plan_update', 'plan': []}}) + '\n', encoding='utf-8')
            result = codex_tasks(path, 't1')
            self.assertEqual(result['tasks'], [])
            self.assertEqual(result['source'], 'Codex / update_plan')
    def test_bridge_invalid_identity_and_status_do_not_write(self):
        with TemporaryDirectory() as d:
            env = {'HC_TERMINAL_ID': 't1', 'CODEX_THREAD_ID': '00000000-0000-0000-0000-000000000001'}
            with self.assertRaises(ValueError): write_plan({'plan': [{'step': 'x', 'status': 'done'}]}, d, env)
            with self.assertRaises(ValueError): write_plan({'plan': []}, d, dict(env, HC_TERMINAL_ID='../other'))
            with self.assertRaises(ValueError): write_plan({'plan': []}, d, dict(env, CODEX_THREAD_ID=''))
            self.assertFalse((Path(d) / 'data').exists())
    def test_clean_title_drops_live_glyph_only(self):
        self.assertEqual(clean_title('⠂ 🐋 herdr右侧进度自动识别对话'), '🐋 herdr右侧进度自动识别对话')
        self.assertEqual(clean_title('⠏ 优化关卡原型架构 | dev_workspace'), '优化关卡原型架构 | dev_workspace')
        self.assertEqual(clean_title(None), '')
    def test_codex_title_selects_thread(self):
        with TemporaryDirectory() as d:
            home = Path(d)
            (home / '.codex').mkdir()
            old, new, other = home / 'old.jsonl', home / 'new.jsonl', home / 'other.jsonl'
            for f in (old, new, other): f.write_text('', encoding='utf-8')
            con = sqlite3.connect(home / '.codex/state_5.sqlite')
            con.execute('create table threads (id text, name text, title text, rollout_path text, updated_at_ms integer)')
            con.execute('insert into threads values (?,?,?,?,?)', ('a', '旧会话', '', str(old), 1))
            con.execute('insert into threads values (?,?,?,?,?)', ('b', '优化关卡原型架构', 'prompt', str(new), 2))
            con.execute('insert into threads values (?,?,?,?,?)', ('c', '优化关卡原型架构', '', str(other), 3))
            con.commit(); con.close()
            with patch('collector.HOME', home):
                # Same thread name in another terminal: the rollout this terminal holds open wins.
                self.assertEqual(codex_thread('⠏ 优化关卡原型架构 | dev_workspace', [str(new)]), str(new))
                self.assertEqual(codex_thread('⠏ 优化关卡原型架构 | dev_workspace'), str(other))
                self.assertIsNone(codex_thread('⠏ 不存在的会话 | dev_workspace'))
                self.assertIsNone(codex_thread(''))
    def test_dsh_todos_follow_open_session_title(self):
        with TemporaryDirectory() as d, TemporaryDirectory() as u:
            dsh, tui = Path(d), Path(u)
            cache = dsh / 'storages/session_projcache/sessions'
            cache.mkdir(parents=True)
            rows = {'s1': ('会话一', {'val': [{'content': '会话一任务', 'status': 'pending'}]}),
                    's2': ('会话二', {'val': {'todos': [{'content': '会话二任务', 'status': 'in_progress'}]}})}
            for sid, (title, todos) in rows.items():
                (cache / (sid + '.json')).write_text(json.dumps(
                    {'record': {'rows': {'title': {'val': title}, 'todos': todos}}}), encoding='utf-8')
            (tui / 'session-mounts.json').write_text(json.dumps(
                {'version': 1, 'owners': [{'pid': 7, 'sessionIds': ['s1', 's2']}]}), encoding='utf-8')
            procs = [SimpleNamespace(pid=7)]
            with patch('collector.DSH', dsh), patch('collector.DSH_TUI', tui):
                auto = dsh_collect(procs, None, '⠂ 🐋 会话二')
                self.assertEqual(auto['session'], 's2')
                self.assertEqual(auto['tasks'][0]['status'], 'in_progress')
                self.assertEqual(auto['choices'], [])
                picker = dsh_collect(procs, None, '⠂ 🐋 未匹配的标题')
                self.assertEqual(picker['tasks'], [])
                self.assertEqual([c['path'] for c in picker['choices']], ['s1', 's2'])
                manual = dsh_collect(procs, 's1', '⠂ 🐋 未匹配的标题')
                self.assertEqual(manual['session'], 's1')
                self.assertEqual(manual['tasks'][0]['subject'], '会话一任务')
    def test_dsh_without_mount_ledger_explains(self):
        with TemporaryDirectory() as d:
            with patch('collector.DSH', Path(d)), patch('collector.DSH_TUI', Path(d)):
                result = dsh_collect([SimpleNamespace(pid=7)], None, '标题')
                self.assertEqual(result['tasks'], [])
                self.assertIn('session-mounts.json', result['message'])
    def test_opencode_todos_read_active_session(self):
        with TemporaryDirectory() as d:
            db = Path(d) / 'opencode.db'
            work = Path(d) / 'work'
            con = sqlite3.connect(db)
            con.execute('create table session_v2 (id text, directory text, title text, time_idle integer, time_archived integer, time_updated integer)')
            con.execute('create table todo (session_id text, content text, status text, priority integer, position integer, time_created integer, time_updated integer)')
            con.execute("insert into session_v2 values ('ses_active', ?, 'active', null, null, 2)", (str(work),))
            con.execute("insert into session_v2 values ('ses_old', ?, 'old', 1, null, 1)", (str(work),))
            con.execute("insert into todo values ('ses_active', '写代码', 'in_progress', 0, 0, 1, 1)")
            con.execute("insert into todo values ('ses_active', '写测试', 'pending', 0, 1, 1, 1)")
            con.execute("insert into todo values ('ses_active', '已取消', 'cancelled', 0, 2, 1, 1)")
            con.commit(); con.close()
            proc = SimpleNamespace(cwd=lambda: str(work))
            with patch('collector.OPENCODE_DB', db):
                result = opencode_collect([proc])
            self.assertEqual(result['session'], 'ses_active')
            self.assertEqual([t['subject'] for t in result['tasks']], ['写代码', '写测试'])
            self.assertEqual(result['tasks'][0]['status'], 'in_progress')
    def test_opencode_without_session_explains(self):
        with TemporaryDirectory() as d:
            db = Path(d) / 'opencode.db'
            con = sqlite3.connect(db)
            con.execute('create table session_v2 (id text, directory text, title text, time_idle integer, time_archived integer, time_updated integer)')
            con.execute('create table todo (session_id text, content text, status text, priority integer, position integer, time_created integer, time_updated integer)')
            con.commit(); con.close()
            proc = SimpleNamespace(cwd=lambda: str(Path(d) / 'elsewhere'))
            with patch('collector.OPENCODE_DB', db):
                result = opencode_collect([proc])
            self.assertEqual(result['tasks'], [])
            self.assertIn('OpenCode 会话', result['message'])

if __name__ == '__main__': unittest.main()
