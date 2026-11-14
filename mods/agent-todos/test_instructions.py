import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from enable_instructions import END, START, install, updated_text


class NativeInstructions(unittest.TestCase):
    def test_preview_apply_reapply_and_remove_preserve_existing_guidance(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            app = home / 'herdr'
            guide = app / 'mods/agent-todos/instructions.md'
            guide.parent.mkdir(parents=True)
            guide.write_text('native todo instructions')
            codex = home / '.codex'
            codex.mkdir()
            target = codex / 'AGENTS.md'
            target.write_text('Existing user rules\n')
            self.assertEqual(install(app, home, {}, apply=False), [str(target)])
            self.assertEqual(target.read_text(), 'Existing user rules\n')
            install(app, home, {}, apply=True)
            applied = target.read_text()
            self.assertTrue(applied.startswith('Existing user rules\n'))
            self.assertEqual(applied.count(START), 1)
            self.assertIn(str(guide), applied)
            self.assertEqual(install(app, home, {}, apply=True), [])
            install(app, home, {}, apply=True, remove=True)
            self.assertIn('Existing user rules\n', target.read_text())
            self.assertNotIn(START, target.read_text())
            self.assertTrue(list((app / 'data/agent-todos/instruction-backups').glob('*/*')))

    def test_codex_override_and_custom_home_are_respected(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            app = home / 'herdr'
            guide = app / 'mods/agent-todos/instructions.md'
            guide.parent.mkdir(parents=True)
            guide.write_text('native todo instructions')
            custom = home / 'custom-codex'
            custom.mkdir()
            override = custom / 'AGENTS.override.md'
            override.write_text('Override rules\n')
            install(app, home, {'CODEX_HOME': str(custom)}, apply=True)
            self.assertIn(START, override.read_text())
            self.assertFalse((custom / 'AGENTS.md').exists())
            self.assertFalse((home / '.codex').exists())

    def test_malformed_markers_are_rejected(self):
        for text in [START, END, START + END + START + END]:
            with self.assertRaises(ValueError): updated_text(text, Path('/synthetic/instructions.md'))


if __name__ == '__main__': unittest.main()
