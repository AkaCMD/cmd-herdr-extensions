"""Synthetic installer checks; never write to the real Herdr or client directories."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('extension_install', Path(__file__).with_name('install.py'))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

permissions_spec = importlib.util.spec_from_file_location('enable_permissions', Path(__file__).with_name('enable_permissions.py'))
permissions_installer = importlib.util.module_from_spec(permissions_spec)
permissions_spec.loader.exec_module(permissions_installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.herdr = self.base / 'herdr'
        self.pi = self.base / 'pi'
        self.files = {
            'herdr': ['mods/demo/main.js'],
            'pi': ['pi/extensions/demo.ts'],
            'plugin': ['plugins/herdr-personal-dispatch/install.py', 'plugins/herdr-personal-dispatch/.codex-plugin/plugin.json'],
        }
        for entries in self.files.values():
            for entry in entries:
                source = self.repo / entry
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b'synthetic source\n')
        # This file must never be copied, even though it is beside listed sources.
        (self.repo / 'plugins/herdr-personal-dispatch/unlisted.txt').write_bytes(b'local only')
        self.manifest = {'personal-dispatch': {'files': self.files}}
        self.write_manifest()
        self.root_patch = patch.object(installer, 'ROOT', self.repo)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def write_manifest(self):
        (self.repo / 'extensions.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def invoke(self, *options):
        output = io.StringIO()
        argv = ['install.py', 'personal-dispatch', '--herdr-home', str(self.herdr), '--pi-home', str(self.pi), *options]
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = installer.main()
        return result, output.getvalue()

    def test_host_mapping_preserves_old_destinations(self):
        pairs = installer.plan('personal-dispatch', self.herdr, self.pi)
        self.assertEqual([target for _, _, target in pairs], [
            self.herdr / 'mods/demo/main.js',
            self.pi / 'extensions/demo.ts',
            self.herdr / 'plugins/herdr-personal-dispatch/install.py',
            self.herdr / 'plugins/herdr-personal-dispatch/.codex-plugin/plugin.json',
        ])

    def test_preview_is_read_only(self):
        result, output = self.invoke()
        self.assertEqual(result, 0)
        self.assertIn('Preview: 4 file(s)', output)
        self.assertFalse(self.herdr.exists())
        self.assertFalse(self.pi.exists())
        self.assertFalse((self.repo / '.local').exists())

    def test_apply_copies_only_manifest_and_is_idempotent(self):
        result, output = self.invoke('--apply')
        self.assertEqual(result, 0)
        self.assertIn(str(self.herdr / 'plugins/herdr-personal-dispatch/install.py'), output)
        for _, source, target in installer.plan('personal-dispatch', self.herdr, self.pi):
            self.assertEqual(source.read_bytes(), target.read_bytes())
        self.assertFalse((self.herdr / 'plugins/herdr-personal-dispatch/unlisted.txt').exists())
        result, output = self.invoke('--apply')
        self.assertEqual(result, 0)
        self.assertIn('Installed 0 file(s)', output)
        self.assertFalse((self.repo / '.local').exists())

    def test_conflicts_refused_then_backed_up_before_replace(self):
        target = self.herdr / 'plugins/herdr-personal-dispatch/install.py'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'original local file')
        self.assertEqual(self.invoke('--apply')[0], 1)
        self.assertEqual(target.read_bytes(), b'original local file')
        self.assertFalse((self.herdr / 'mods').exists())
        self.assertEqual(self.invoke('--apply', '--replace')[0], 0)
        saved = list((self.repo / '.local/backups').glob('*/plugin/plugins/herdr-personal-dispatch/install.py'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), b'original local file')
        self.assertEqual(target.read_bytes(), b'synthetic source\n')

    def test_invalid_kinds_paths_and_missing_sources_rejected(self):
        outside = self.base / 'outside.py'
        outside.write_bytes(b'outside')
        for kind, entry in [
            ('unknown', 'mods/demo/main.js'),
            ('plugin', 'mods/demo/main.js'),
            ('plugin', '../outside.py'),
            ('plugin', 'plugins/missing.py'),
        ]:
            with self.subTest(kind=kind, entry=entry):
                self.manifest['personal-dispatch']['files'] = {kind: [entry]}
                self.write_manifest()
                with self.assertRaises(ValueError):
                    installer.plan('personal-dispatch', self.herdr, self.pi)


class PermissionsInstallerTests(unittest.TestCase):
    def test_native_programs_keep_original_arguments_and_metadata(self):
        original = {
            "programs": {
                "codex": {"run": ["codex", "--no-daemon"], "name": "My Codex"},
                "dsh-tui": {"run": ["dsh-tui", "--backend", "dsh"]},
                "pi": {"run": ["pi"]},
            },
            "shells": ["fish"],
        }
        module = Path("/fixture/data/codex-bridge/permissions.py")
        with patch.object(permissions_installer.shutil, "which", side_effect=lambda tool: None if tool == "claude" else tool):
            result, changed = permissions_installer.patch_programs(original, module)
            self.assertEqual(changed, ["codex", "dsh-tui"])
            self.assertEqual(result["programs"]["codex"]["run"], [
                sys.executable, str(module), "codex", "2", "codex", "--no-daemon",
            ])
            self.assertEqual(result["programs"]["dsh-tui"]["run"][2:], ["dsh", "3", "dsh-tui", "--backend", "dsh"])
            self.assertEqual(result["programs"]["codex"]["name"], "My Codex")
            self.assertEqual(result["programs"]["pi"], original["programs"]["pi"])
            self.assertEqual(result["shells"], original["shells"])
            self.assertEqual(original["programs"]["codex"]["run"], ["codex", "--no-daemon"])
            self.assertEqual(permissions_installer.patch_programs(result, module)[1], [])


if __name__ == '__main__':
    unittest.main()
