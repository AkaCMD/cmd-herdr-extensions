"""Synthetic transcripts only: no real conversation data."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import session_changes as sc


def jsonl(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(t))


class SessionChangesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # The fixtures live in %TEMP%; the temp-folder rule is tested on its own.
        self.temp_dirs, sc.TEMP_DIRS = sc.TEMP_DIRS, []
        self.addCleanup(setattr, sc, 'TEMP_DIRS', self.temp_dirs)
        self.real = os.path.join(self.dir, 'app.cs')
        self.tool = os.path.join(self.dir, 'export_graph.py')
        self.gone = os.path.join(self.dir, 'scratch.txt')
        self.addCleanup(shutil.rmtree, self.dir, True)
        for path, text in ((self.real, 'a\nB\nc\n'), (self.tool, 'print(1)\n')):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)

    @unittest.skipUnless(sys.platform == 'linux', 'requires Linux /proc')
    def test_linux_process_tree_and_start_time(self):
        started = time.time()
        with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']) as child:
            try:
                procs = sc.processes()
                parent, name, created = procs[child.pid]
                self.assertEqual(parent, os.getpid())
                self.assertTrue(name.startswith('python'))
                self.assertIn(child.pid, sc.descendants(procs, parent))
                self.assertLess(abs(started - created), 2)
            finally:
                child.terminate()

    @unittest.skipUnless(sys.platform == 'linux', 'requires Linux /proc')
    def test_linux_rollout_follows_open_file_and_rejects_ambiguity(self):
        first = os.path.join(self.dir, 'rollout-first.jsonl')
        second = os.path.join(self.dir, 'rollout-second.jsonl')
        with open(first, 'w', encoding='utf-8'):
            self.assertEqual(sc.linux_codex_rollout([os.getpid()]), first)
            with open(second, 'w', encoding='utf-8'):
                self.assertIsNone(sc.linux_codex_rollout([os.getpid()]))

    def claude(self):
        now = time.time()
        def use(i, name, inp, t):
            return {'timestamp': iso(t), 'message': {'content': [{'type': 'tool_use', 'id': i, 'name': name, 'input': inp}]}}
        def result(i, r, t, text='ok'):
            return {'timestamp': iso(t), 'toolUseResult': r,
                    'message': {'content': [{'type': 'tool_result', 'tool_use_id': i, 'content': text}]}}
        path = os.path.join(self.dir, 's.jsonl')
        jsonl(path, [
            use('1', 'Edit', {'file_path': self.real}, now - 50),
            result('1', {'filePath': self.real, 'structuredPatch': [
                {'oldStart': 2, 'oldLines': 1, 'newStart': 2, 'newLines': 1, 'lines': ['-b', '+B']}]}, now - 49),
            use('2', 'Write', {'file_path': self.tool, 'content': 'print(1)\n'}, now - 40),
            result('2', {'type': 'create', 'filePath': self.tool, 'content': 'print(1)\n'}, now - 39),
            use('3', 'Bash', {'command': f'python "{self.tool}"'}, now - 30),
            result('3', None, now - 29, '1'),
            use('4', 'Write', {'file_path': self.gone, 'content': 'x'}, now - 20),
            result('4', {'type': 'create', 'filePath': self.gone, 'content': 'x'}, now - 19),
        ])
        return path

    def test_claude_edits_scripts_and_cleanup(self):
        ch = sc.Changes()
        sc.read_claude([self.claude()], ch)
        files = {os.path.basename(f['path']): f for f in sc.build(ch, self.dir)}
        self.assertEqual(files['app.cs']['status'], 'M')
        self.assertEqual((files['app.cs']['add'], files['app.cs']['del']), (1, 1))
        self.assertIsNone(files['app.cs']['script'])
        self.assertEqual(files['export_graph.py']['script'], 'run by the agent')
        self.assertNotIn('scratch.txt', files)  # made, then removed during the conversation

    def test_codex_file_change(self):
        path = os.path.join(self.dir, 'r.jsonl')
        jsonl(path, [{'timestamp': iso(time.time()), 'payload': {'type': 'item_completed', 'item': {
            'type': 'FileChange', 'status': 'completed', 'changes': {
                self.real: {'type': 'update', 'unified_diff': '@@ -2,1 +2,1 @@\n-b\n+B\n'}}}}}])
        ch = sc.Changes()
        sc.read_codex(path, ch)
        rows = sc.build(ch, self.dir)[0]['rows']
        self.assertEqual([r[0] for r in rows], ['@', '-', '+'])
        self.assertEqual(rows[2][1], 2)

    def test_helper_name_and_embedded_run(self):
        ch = sc.Changes()
        ch.add(os.path.join(self.dir, 'verify_fix.py'), 'A', [], sc.stamp(None))
        ch.add(os.path.join(self.dir, 'patch_bp.py'), 'A', [], sc.stamp(None))
        ch.command("dcc_exec exec(open(r'C:/x/patch_bp.py').read())")
        recs = {os.path.basename(r['path']): r for r in ch.files.values()}
        self.assertEqual(sc.tool_script(recs['verify_fix.py'], ch), 'helper name')
        self.assertEqual(sc.tool_script(recs['patch_bp.py'], ch), 'run by the agent')

    def test_temp_folder_long_and_short_forms(self):
        temp = sc.long_path(tempfile.gettempdir())
        ch = sc.Changes()
        ch.add(os.path.join(temp, 'notes.md'), 'M', [], sc.stamp(None))
        rec = next(iter(ch.files.values()))
        self.assertIsNone(sc.tool_script(rec, ch))
        sc.TEMP_DIRS = self.temp_dirs
        self.assertEqual(sc.tool_script(rec, ch), 'temp folder')

    def test_write_calls_need_an_edit(self):
        ch = sc.Changes()
        ch.call(1, 2, 'export /Game/BP_Thing graph')
        ch.call(3, 4, 'lib.compile_blueprint(load("/Game/BP_Thing"))')
        self.assertEqual([t for t, _ in sc.write_calls(ch)], [3])


if __name__ == '__main__':
    unittest.main()
