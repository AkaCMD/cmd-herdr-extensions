"""Synthetic DeepSeek tool events, with no real conversation data."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deepseek_changes as ds
import session_changes as sc


class DeepSeekTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.dsh = self.root / 'dsh'
        self.tui = self.root / 'tui'
        env = patch.dict(os.environ, DSH_HOME=str(self.dsh), DSH_TUI_HOME=str(self.tui))
        env.start()
        self.addCleanup(env.stop)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def log(self, session, events=()):
        path = self.dsh / 'sessions' / 'work' / session / 'session.v4.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {'type': 'session', 'version': 4, 'id': session, 'cwd': self.workspace.as_uri()}
        path.write_bytes(''.join(json.dumps(event) + '\n' for event in [header, *events]).encode())
        return path

    def test_mounted_selection_follows_exact_title_or_prompts(self):
        self.write_json(self.tui / 'session-mounts.json', {'owners': [
            {'pid': 11, 'sessionIds': ['dsh:one', 'two', 'claude:other', '../invalid']},
            {'pid': 22, 'sessionIds': ['three']},
        ]})
        for session, title in [('one', '项目一'), ('two', '项目二'), ('three', '另一个终端')]:
            self.log(session)
            self.write_json(self.dsh / f'storages/session_projcache/sessions/{session}.json',
                            {'record': {'rows': {'title': {'val': title}}}})
        self.assertIsNone(ds.mounted_session({33}, ''))
        self.assertEqual(ds.mounted_session({11}, '⠋ 🐋 项目二', 'one')['session'], 'two')
        self.assertEqual(ds.mounted_session({11}, '✦ 🐋 项目二', 'one', pinned=True)['session'], 'one')
        choices = ds.mounted_session({11}, 'unknown')['choices']
        self.assertEqual([choice['id'] for choice in choices], ['one', 'two'])
        self.assertIn('error', ds.mounted_session({11}, 'unknown', 'three', pinned=True))

    def test_fresh_mount_can_precede_the_first_log_event(self):
        self.write_json(self.tui / 'session-mounts.json', {'owners': [{'pid': 11, 'sessionIds': ['fresh']}]})
        self.assertEqual(ds.mounted_session({11}, '')['transcripts'], [])
        path = self.log('fresh')
        self.assertEqual(ds.mounted_session({11}, '')['transcripts'], [path])

    def test_replay_successful_writes_edits_and_partial_patch(self):
        def tool(tool_id, name, args, meta=None, text='', error=False):
            return [
                {'type': 'tool/call', 'data': {'name': name, 'callId': tool_id, 'arguments': json.dumps(args)}},
                {'type': 'tool/result', 'time': 1000, 'data': {
                    'message': {'toolCallId': tool_id, 'isError': error, 'content': [{'type': 'text', 'text': text}]},
                    'meta': meta or {},
                }},
            ]

        after = '{\n  "count": 2\n}'
        path = self.log('session', [
            *tool('w', 'write', {'file_path': 'new.yaml'}, {'operation': 'create', 'path': 'new.yaml'}),
            *tool('e', 'edit', {'file_path': 'config.json'}, {'diffs': [
                {'path': 'config.json', 'oldText': '{\n  "count": 1\n}', 'newText': after},
            ]}),
            *tool('bad', 'write', {'file_path': 'bad.txt'}, {'operation': 'create', 'path': 'bad.txt'}, error=True),
            *tool('shell', 'bash', {'command': 'touch untracked.txt'}),
            *tool('p', 'apply_patch', {'input': '*** Update File: old.md\n*** Move to: renamed.md\n'},
                  {'stopped': True, 'diffs': [{'path': 'obsolete.txt', 'oldText': 'old', 'newText': ''}]},
                  text='Patch stopped after 2 of 3 operations.\nCompleted:\nM renamed.md\nD obsolete.txt\nFailed:\nA skipped.txt'),
        ])
        for name, content in [('config.json', after), ('new.yaml', 'new: true'), ('renamed.md', 'renamed')]:
            (self.workspace / name).write_text(content, encoding='utf-8')
        ch = sc.Changes()
        cwd = ds.read_changes(path, 'session', ch, sc.stamp)
        result = {item['rel']: item for item in sc.build(ch, cwd)}
        self.assertEqual({name: item['status'] for name, item in result.items()}, {
            'new.yaml': 'A', 'config.json': 'M', 'old.md': 'D', 'renamed.md': 'A', 'obsolete.txt': 'D',
        })
        self.assertEqual(result['config.json']['rows'], [['C', '$/count', '1', '2']])
        self.assertEqual(ch.commands, ['touch untracked.txt'])
        with self.assertRaises(ValueError):
            ds.read_changes(path, 'other-session', sc.Changes(), sc.stamp)


if __name__ == '__main__':
    unittest.main()
