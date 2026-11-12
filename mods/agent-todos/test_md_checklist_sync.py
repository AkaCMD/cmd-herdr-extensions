import json, tempfile, unittest
from pathlib import Path
import md_checklist_sync as sync

SAMPLE = """# Plan
- [x] Done item
- [~] Running item
- [ ] Waiting item
  - [ ] Indented child
not a task
"""


class MdChecklistSync(unittest.TestCase):
    def test_parse_markers(self):
        self.assertEqual(sync.parse(SAMPLE), [('completed', 'Done item'), ('in_progress', 'Running item'),
                                              ('pending', 'Waiting item'), ('pending', 'Indented child')])

    def test_build_marks_source_and_active_form(self):
        tasks = sync.build(sync.parse(SAMPLE))
        self.assertTrue(all(t['_source'] == sync.MARK for t in tasks))
        self.assertEqual(tasks[1]['activeForm'], '正在：Running item')
        self.assertNotIn('activeForm', tasks[0])

    def test_long_subject_is_shortened_but_description_is_full(self):
        text = 'x' * 100
        task = sync.build([('pending', text)])[0]
        self.assertEqual(len(task['subject']), 59)
        self.assertEqual(task['description'], text)

    def test_sync_replaces_only_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sync.sync(SAMPLE, 's1', home)
            _, count, counts = sync.sync('- [x] only one\n', 's1', home)
            files = sorted((home / 'tasks' / 's1').glob('*.json'))
            self.assertEqual((count, len(files)), (1, 1))
            self.assertEqual(counts['completed'], 1)

    def test_foreign_file_blocks_sync_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home / 'tasks' / 's2'; folder.mkdir(parents=True)
            native = folder / '1.json'
            native.write_text(json.dumps({'id': '1', 'subject': 'real', 'status': 'pending'}), encoding='utf-8')
            with self.assertRaises(RuntimeError): sync.sync(SAMPLE, 's2', home)
            self.assertEqual(json.loads(native.read_text(encoding='utf-8'))['subject'], 'real')

    def test_rejects_path_like_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ['../x', 'a/b', '']:
                with self.assertRaises(ValueError): sync.sync(SAMPLE, bad, Path(tmp))


if __name__ == '__main__':
    unittest.main()
