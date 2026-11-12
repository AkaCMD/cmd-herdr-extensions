"""Tests for the per-space permission profiles. Synthetic input only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import permissions


class FakeRun:
    """Stands in for `hc get <terminal>`."""

    def __init__(self, labels):
        self.labels = labels
        self.calls = []

    def __call__(self, command, **options):
        self.calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({'labels': self.labels}), '')


class Policy(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'agent-permissions.json'

    def tearDown(self):
        self.folder.cleanup()

    def write(self, spaces):
        self.path.write_text(json.dumps({'version': 1, 'spaces': spaces}), encoding='utf-8')

    def test_missing_file_is_empty(self):
        self.assertEqual(permissions.load_policy(self.path)['spaces'], {})

    def test_invalid_file_is_empty(self):
        self.path.write_text('{not json', encoding='utf-8')
        self.assertEqual(permissions.load_policy(self.path)['spaces'], {})

    def test_spaces_are_read(self):
        self.write({'sp1': {'profile': 'auto', 'override': True}})
        self.assertEqual(permissions.load_policy(self.path)['spaces']['sp1']['profile'], 'auto')

    def test_nothing_applies_without_override(self):
        spaces = {'sp1': {'profile': 'yolo', 'override': False}}
        self.assertIsNone(permissions.setting(spaces, 'sp1'))

    def test_nothing_applies_for_default_or_unknown_profile(self):
        for profile in ('default', 'nonsense'):
            self.assertIsNone(permissions.setting({'sp1': {'profile': profile, 'override': True}}, 'sp1'))

    def test_applied_profile(self):
        chosen = permissions.setting({'sp1': {'profile': 'plan', 'override': True}}, 'sp1')
        self.assertEqual(chosen, {'profile': 'plan'})

    def test_unknown_space(self):
        self.assertIsNone(permissions.setting({'sp1': {'profile': 'plan', 'override': True}}, 'sp2'))


class Space(unittest.TestCase):
    def test_no_terminal(self):
        self.assertIsNone(permissions.space_of({}, FakeRun({})))

    def test_no_hc(self):
        self.assertIsNone(permissions.space_of({'HC_TERMINAL_ID': 't1', 'HC_BIN': 'no-such-hc-anywhere'}, FakeRun({})))

    def test_space_from_labels(self):
        environ = {'HC_TERMINAL_ID': 't7', 'HC_BIN': sys.executable}
        run = FakeRun({'herdr.space': 'sp9', 'herdr.tab': 'tb9'})
        self.assertEqual(permissions.space_of(environ, run), 'sp9')
        self.assertEqual(run.calls[0][-1], 't7')

    def test_unreadable_output(self):
        environ = {'HC_TERMINAL_ID': 't7', 'HC_BIN': sys.executable}

        def run(command, **options):
            return subprocess.CompletedProcess(command, 1, 'not json', '')

        self.assertIsNone(permissions.space_of(environ, run))


class Adapt(unittest.TestCase):
    def test_default_changes_nothing(self):
        self.assertIsNone(permissions.adapt('claude', 'default'))

    def test_claude(self):
        self.assertEqual(permissions.adapt('claude', 'plan')['argv'], ['--permission-mode', 'plan'])
        self.assertEqual(permissions.adapt('claude', 'yolo')['argv'], ['--permission-mode', 'bypassPermissions'])

    def test_codex(self):
        self.assertEqual(permissions.adapt('codex', 'auto')['argv'], ['-s', 'workspace-write', '-a', 'never'])
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', permissions.adapt('codex', 'yolo')['argv'])

    def test_dsh_uses_a_patch_layer(self):
        patch = permissions.adapt('dsh', 'plan')['patch']
        self.assertEqual(patch[0], {'id': 'sandbox-policy', 'config': {'mode': 'read-only'}})
        self.assertEqual(patch[1], {'id': 'approval', 'config': {'policy': 'never'}})

    def test_agents_without_an_adaptation(self):
        for agent in ('pi', 'opencode', 'dsh-headless', 'unknown'):
            self.assertIsNone(permissions.adapt(agent, 'yolo'))


class Launch(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'agent-permissions.json'
        self.path.write_text(json.dumps({'version': 1, 'spaces': {'sp9': {'profile': 'auto', 'override': True}}}), encoding='utf-8')
        self.environ = {'HC_TERMINAL_ID': 't7', 'HC_BIN': sys.executable}
        self.run = FakeRun({'herdr.space': 'sp9'})

    def tearDown(self):
        self.folder.cleanup()

    def test_same_space_elsewhere(self):
        self.assertEqual(permissions.plan('claude', self.path, self.environ, self.run)['profile'], 'auto')

    def test_other_space_gets_nothing(self):
        choice = permissions.plan('claude', self.path, {'HC_TERMINAL_ID': 't7', 'HC_BIN': sys.executable}, FakeRun({'herdr.space': 'sp8'}))
        self.assertIsNone(choice)

    def test_unsupported_agent_gets_nothing(self):
        self.assertIsNone(permissions.plan('pi', self.path, self.environ, self.run))

    def test_arguments_go_before_the_persons(self):
        command = ['claude.exe', '--model', 'x']
        built, _ = permissions.apply('claude', list(command), os.environ, 3, permissions.plan('claude', self.path, self.environ, self.run))
        self.assertEqual(built, ['claude.exe', '--model', 'x', '--permission-mode', 'acceptEdits'])

    def test_person_arguments_follow(self):
        command = ['claude.exe', '--model', 'x', '--resume', 'abc']
        built, _ = permissions.apply('claude', list(command), os.environ, 3, permissions.plan('claude', self.path, self.environ, self.run))
        self.assertEqual(built[3:], ['--permission-mode', 'acceptEdits', '--resume', 'abc'])

    def test_dsh_patch_file(self):
        built, _ = permissions.apply('dsh', ['dsh', '--profile', 'dsh-tui'], os.environ, 3, permissions.plan('dsh', self.path, self.environ, self.run))
        self.assertEqual(built[3], '--patch')
        written = json.loads(Path(built[4]).read_text(encoding='utf-8'))
        self.assertEqual(written[0]['config']['mode'], 'workspace-write')

    def test_nothing_chosen_is_a_no_op(self):
        command = ['claude.exe', '--model', 'x']
        built, _ = permissions.apply('claude', list(command), os.environ, 3, None)
        self.assertEqual(built, command)

    def test_native_codex_uses_space_profile_and_keeps_explicit_flags(self):
        command = ['codex', '--no-daemon', 'message']
        chosen = permissions.adapt('codex', 'plan')
        with patch.object(permissions, 'plan', return_value=chosen) as plan, patch.object(permissions.subprocess, 'call') as call:
            permissions.native_launch('codex', command, 2, self.path)
            self.assertEqual(call.call_args.args[0], ['codex', '--no-daemon', '-s', 'read-only', '-a', 'never', 'message'])
            plan.reset_mock()
            explicit = ['codex', '--no-daemon', '--sandbox=workspace-write']
            permissions.native_launch('codex', explicit, 2, self.path)
            self.assertEqual(call.call_args.args[0], explicit)
            plan.assert_not_called()

    def test_native_dsh_inserts_patch_before_workspace_arguments(self):
        command = ['dsh-tui', '--backend', 'dsh', '/work/space', '--', 'message']
        with patch.object(permissions, 'plan', return_value=permissions.adapt('dsh', 'plan')), patch.object(permissions.subprocess, 'call') as call:
            permissions.native_launch('dsh', command, 4, self.path)
        launch = call.call_args.args[0]
        self.assertEqual(launch[1], '--patch')
        self.assertEqual(launch[3:], command[1:])
        self.assertEqual(json.loads(Path(launch[2]).read_text())[0]['config']['mode'], 'read-only')


if __name__ == '__main__':
    unittest.main()
