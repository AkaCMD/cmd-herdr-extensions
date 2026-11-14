"""Exercise review rollback with synthetic files only."""
import tempfile
import unittest
from pathlib import Path

import review_changes as review


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / '中文.txt'
        self.before = ''.join(f'line {i}\n' for i in range(30))
        self.after = self.before.replace('line 2\n', 'changed 2\n').replace('line 25\n', 'changed 25\n')
        self.path.write_text(self.after)
        self.record = {'created': False, 'ops': [{'hunks': [('line 2', 'changed 2'), ('line 25', 'changed 25')]}]}

    def snapshot(self):
        file = {'path': str(self.path)}
        review.review(file, self.record, 'fixture', self.root)
        return file

    def test_partial_then_whole_undo_uses_persisted_baseline(self):
        file = self.snapshot()
        self.assertEqual(len(file['review']['blocks']), 2)
        review.undo({'review': file['review'], 'block': 0})
        self.assertEqual(self.path.read_text(), self.before.replace('line 25\n', 'changed 25\n'))
        fresh = self.snapshot()
        self.assertEqual(len(fresh['review']['blocks']), 1)
        review.undo({'review': fresh['review']})
        self.assertEqual(self.path.read_text(), self.before)
        self.assertEqual(self.snapshot()['rows'], [])

    def test_stale_view_rejects_undo(self):
        file = self.snapshot()
        self.path.write_text('later work\n')
        with self.assertRaises(ValueError):
            review.undo({'review': file['review']})
        self.assertEqual(self.path.read_text(), 'later work\n')

    def test_new_file_undo_preserves_backup(self):
        self.record = {'created': True, 'ops': []}
        file = self.snapshot()
        review.undo({'review': file['review']})
        self.assertFalse(self.path.exists())
        backups = list(self.root.glob('data/session-review/*/*.backup'))
        self.assertEqual(backups[0].read_bytes(), self.after.encode())

    def test_unverifiable_baseline_is_rejected(self):
        self.path.write_text('unrelated\n')
        with self.assertRaises(ValueError):
            self.snapshot()


if __name__ == '__main__':
    unittest.main()
