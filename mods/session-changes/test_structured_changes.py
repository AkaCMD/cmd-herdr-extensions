"""Synthetic files only. Compared files are never written by the diff reader."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

import session_changes as sc
import structured_changes as sd


def workbook(cells, title='配置', shared=None, style=''):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{sd.NS[1:-1]}" xmlns:r="{sd.REL[1:-1]}"><sheets><sheet name="{title}" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{sd.NS[1:-1]}"><sheetData><row r="1">{cells}</row></sheetData>{style}</worksheet>')
        if shared is not None:
            z.writestr('xl/sharedStrings.xml', f'<sst xmlns="{sd.NS[1:-1]}"><si><t>{shared}</t></si></sst>')
    return stream.getvalue()


class StructuredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / 'herdr'
        self.path = self.root / 'config.json'
        self.path.write_bytes(b'{"value":1}')

    def test_json_paths_types_arrays_and_escaping(self):
        result = sd.compare('a.json', b'{"a/b":{"~x":true},"list":[1]}', b'{"a/b":{"~x":1},"list":[1,2]}')
        self.assertEqual(result['rows'], [['C', '$/a~1b/~0x', 'true', '1'], ['C', '$/list/1', '∅', '2']])
        self.assertEqual((result['add'], result['del']), (2, 1))

    def test_json_format_only(self):
        result = sd.compare('a.json', b'{"a":1,"b":2}', b'{ "b": 2, "a": 1 }')
        self.assertEqual(result['changes'], 0)
        self.assertEqual(result['rows'][0][0], '')

    def test_xlsx_sheet_and_cell_values_and_formula(self):
        before = workbook('<c r="A1" t="s"><v>0</v></c><c r="B1"><v>1</v></c><c r="C1"><f>B1+1</f><v>2</v></c>', shared='旧值')
        after = workbook('<c r="A1" t="inlineStr"><is><t>新值</t></is></c><c r="B1"><v>1.0</v></c><c r="C1"><f>B1+2</f><v>3</v></c><c r="D1" s="5"/>')
        result = sd.compare('config.xlsx', before, after)
        self.assertEqual(result['changes'], 2)
        self.assertEqual(result['rows'], [['C', '[配置] A1', '"旧值"', '"新值"'], ['C', '[配置] C1', '=B1+1', '=B1+2']])

    def test_xlsx_shared_and_inline_strings_equal(self):
        a = workbook('<c r="A1" t="s"><v>0</v></c>', shared='same')
        b = workbook('<c r="A1" t="inlineStr"><is><t>same</t></is></c>')
        self.assertEqual(sd.compare('a.xlsx', a, b)['changes'], 0)

    def test_xlsx_sheet_added_and_deleted(self):
        a = workbook('<c r="A1"><v>5</v></c>', title='Old')
        b = workbook('<c r="A1"><v>5</v></c>', title='New')
        result = sd.compare('a.xlsx', a, b)
        self.assertEqual(result['changes'], 4)
        self.assertTrue(any(r[1] == '[Old]' and r[3] == '∅' for r in result['rows']))

    def test_csv_bom_quoted_newline_and_chinese(self):
        a = '名称,值\r\n中文,"第一行\n第二行"\r\n'.encode('utf-8-sig')
        b = '名称,值\r\n中文,"改动\n第二行"\r\n'.encode('gb18030')
        result = sd.compare('a.csv', a, b)
        self.assertEqual(result['changes'], 1)
        self.assertEqual(result['rows'][0][1], '[Table] B2')

    def test_snapshot_first_baseline_and_refresh_and_session_isolation(self):
        sd.register(self.path, 'session-1', snapshot=True, app_home=self.app)
        signature = sd.bridge_signature('session-1', self.app)
        self.path.write_bytes(b'{"value":2}')
        sd.register(self.path, 'session-1', snapshot=True, app_home=self.app)
        self.assertNotEqual(signature, sd.bridge_signature('session-1', self.app))
        rows = sd.load_records('session-1', str(self.root), self.app)
        self.assertEqual(rows[0]['rows'][1], ['C', '$/value', '1', '2'])
        self.assertEqual(self.path.read_bytes(), b'{"value":2}')
        self.assertEqual(sd.load_records('session-2', str(self.root), self.app), [])

    def test_comparison_frozen_and_labeled_without_changing_source(self):
        after = self.root / 'shelf.json'
        after.write_bytes(b'{"value":3}')
        sd.register(self.path, 'session-1', self.path, after, 'Shelf CL 123', app_home=self.app)
        after.write_bytes(b'{"value":99}')
        result = sd.load_records('session-1', str(self.root), self.app)[0]
        self.assertEqual(result['rows'][1], ['C', '$/value', '1', '3'])
        self.assertEqual(result['scope'], 'comparison')
        self.assertEqual(result['label'], 'Shelf CL 123')
        self.assertEqual(self.path.read_bytes(), b'{"value":1}')

    def test_added_deleted_and_reverted_files(self):
        new = self.root / 'new.json'
        sd.register(new, 'add', snapshot=True, app_home=self.app)
        self.assertEqual(sd.load_records('add', str(self.root), self.app), [])
        new.write_bytes(b'{}')
        self.assertEqual(sd.load_records('add', str(self.root), self.app)[0]['status'], 'A')
        new.unlink()
        self.assertEqual(sd.load_records('add', str(self.root), self.app), [])
        sd.register(self.path, 'delete', snapshot=True, app_home=self.app)
        self.path.unlink()
        self.assertEqual(sd.load_records('delete', str(self.root), self.app)[0]['status'], 'D')
        self.path.write_bytes(b'{"value":1}')
        self.assertEqual(sd.load_records('delete', str(self.root), self.app), [])

    def test_invalid_files_isolated_and_visible(self):
        sd.register(self.path, 'bad', snapshot=True, app_home=self.app)
        self.path.write_bytes(b'invalid json')
        result = sd.load_records('bad', str(self.root), self.app)
        self.assertEqual(result[0]['kind'], 'Text')
        self.assertIn('Structured comparison unavailable', result[0]['rows'][1][2])
        self.assertIn(['+', None, 'invalid json'], result[0]['rows'])
        with self.assertRaises(ValueError):
            sd.session_dir('../other', self.app)

    def test_native_json_edits_reverse_with_verification(self):
        before = '{\n  "n": 1\n}\n'
        after = '{\n  "n": 2\n}\n'
        ops = [{'rows': sc.unified_rows('@@ -1,3 +1,3 @@\n {\n-  "n": 1\n+  "n": 2\n }\n')}]
        restored = sd.restore_text_baseline(after.encode(), ops)
        self.assertEqual(json.loads(restored), json.loads(before))
        with self.assertRaises(ValueError):
            sd.restore_text_baseline(b'{\n  "n": 3\n}', ops)

    def test_real_build_native_json_side_by_side(self):
        self.path.write_bytes(b'{\n  "n": 2\n}\n')
        ch = sc.Changes()
        ch.add(str(self.path), 'M', sc.unified_rows('@@ -1,3 +1,3 @@\n {\n-  "n": 1\n+  "n": 2\n }'), sc.stamp(None))
        result = sc.build(ch, str(self.root))[0]
        self.assertTrue(result['structured'])
        self.assertEqual(result['rows'][0], ['C', '$/n', '1', '2'])

    def test_jsonl_notebook_and_tab_formats(self):
        result = sd.compare('a.ndjson', b'{"n":1}\n\n{"n":2}\n', b'{"n":1}\n\n{"n":3}\n')
        self.assertEqual(result['rows'], [['C', 'Line 3/n', '2', '3']])
        self.assertEqual(sd.compare('a.ipynb', b'{"cells":[]}', b'{"cells":[1]}')['rows'],
                         [['C', '$/cells/0', '∅', '1']])
        self.assertEqual(sd.compare('a.tab', b'a\tb', b'a\tc')['rows'],
                         [['C', '[Table] B1', '"b"', '"c"']])
        self.assertEqual(sd.compare('a.xltx', None, workbook('<c r="A1"><v>1</v></c>'))['kind'], 'XLSX')

    @unittest.skipUnless(sd.tomllib, 'requires Python 3.11 or later')
    def test_toml_values_and_typed_dates(self):
        result = sd.compare('pyproject.toml', b'[project]\ndate = 2026-11-10\n', b'[project]\ndate = "2026-11-10"\n')
        self.assertEqual(result['changes'], 1)
        self.assertEqual(result['rows'], [['C', '$/project/date', 'date 2026-11-10', '"2026-11-10"']])

    def test_utf16_and_utf32_preview_and_diff_header_like_lines(self):
        for encoding in ('utf-16', 'utf-32'):
            with self.subTest(encoding=encoding):
                before, after = '--old\n'.encode(encoding), '++new\n'.encode(encoding)
                self.path.write_bytes(after)
                self.assertEqual(sc.current_rows(self.path), [['+', 1, '++new']])
                self.assertEqual(sd.compare('a.ini', before, after)['rows'][1:],
                                 [['-', None, '--old'], ['+', None, '++new']])

    def test_binary_snapshots_show_metadata_even_when_lengths_match(self):
        old, new = b'%PDF-1.7\nA', b'%PDF-1.7\nB'
        result = sd.compare('a.pdf', old, new)
        self.assertEqual(result['kind'], 'Binary')
        self.assertNotEqual(result['rows'][1][2].split()[-1], result['rows'][2][2].split()[-1])
        self.assertEqual(result['rows'][0][2], 'Binary content differs')

    def test_recorded_hunks_require_a_unique_post_edit_match(self):
        ops = [{'hunks': [('old', 'new')]}]
        self.assertEqual(sd.restore_text_baseline(b'prefix\nnew\n', ops), b'prefix\nold')
        for current in (b'new\nnew\n', b'changed\n'):
            with self.assertRaises(ValueError):
                sd.restore_text_baseline(current, ops)

    def test_truncation_keeps_full_counts(self):
        a = {str(i): 1 for i in range(sd.MAX_ROWS + 1)}
        b = {str(i): 2 for i in range(sd.MAX_ROWS + 1)}
        result = sd.compare('a.json', json.dumps(a).encode(), json.dumps(b).encode())
        self.assertTrue(result['cut'])
        self.assertEqual(len(result['rows']), sd.MAX_ROWS)
        self.assertEqual(result['add'], sd.MAX_ROWS + 1)

    def test_spinner_title_and_powershell_flags(self):
        self.assertEqual(sc.clean_title('⠴ 会话名字 | project'), '会话名字')
        path = self.root / 'synthetic.jsonl'
        path.write_text(json.dumps({'timestamp': '2026-11-10T00:00:00Z', 'payload': {'type': 'item_completed', 'item': {
            'type': 'CommandExecution', 'command': ['pwsh.exe', '-NoProfile', '-Command', 'python edit.py'], 'duration_ms': 100,
            'aggregated_output': 'done'}}}), encoding='utf-8')
        ch = sc.Changes()
        sc.read_codex(str(path), ch)
        self.assertEqual(ch.commands, ['python edit.py'])


if __name__ == '__main__':
    unittest.main()
