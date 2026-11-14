import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('enable_controls', Path(__file__).resolve().parents[3] / 'mods/dispatch-control/enable.py')
enable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enable)


class EnableTests(unittest.TestCase):
    def test_personal_layout_and_keys_are_preserved_and_idempotent(self):
        original = {'id': 'root', 'type': 'row', 'terminal_padding': [14, 12], 'children': [
            {'id': 'main', 'type': 'block', 'content': {'tab': 'active'}}]}
        keys = {'terminal': {'ctrl-v': 'terminal.paste_image'}, 'global': {'alt-1': 'space.select:1'}}
        layout, bound = enable.patch(original, keys, 'personal', '/python')
        self.assertEqual(original['type'], 'row')
        self.assertEqual(layout['terminal_padding'], [14, 12])
        self.assertEqual(bound['terminal'], keys['terminal'])
        self.assertEqual(bound['global'], keys['global'])
        again = enable.patch(layout, bound, 'personal', '/python')
        self.assertEqual(again, (layout, bound))
        helper = next(n for n in enable.walk(layout) if n.get('content', {}).get('mod') == 'dispatch-control')
        self.assertEqual(helper['size'], {'px': 0})
        self.assertNotIn('ctrl-c', bound['layout:personal'])
        self.assertEqual(len([n for n in enable.walk(layout) if n.get('content', {}).get('mod') == 'dispatch-control']), 1)

    def test_conflicting_keys_and_shipped_layouts_are_refused(self):
        layout = {'type': 'column', 'children': []}
        with self.assertRaises(ValueError):
            enable.patch(layout, {'terminal': {'escape': 'user.command'}}, 'personal', '/python')
        with self.assertRaises(ValueError):
            enable.patch(layout, {}, 'main', '/python')
