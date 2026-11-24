"""Comments in .gitr/review.json, anchored to a snapshot of the file and
remapped to where their line is now."""

import tempfile
import unittest
from pathlib import Path

from gitr import ReviewStore, _comment_id, _resolve_anchors

BEFORE = 'def f():\n    a = 1\n    return a\n'


class CommentsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gitr_dir = Path(self._tmp.name) / '.gitr'
        self.files: dict[str, str] = {'f.py': BEFORE}

    def tearDown(self):
        self._tmp.cleanup()

    def comment(self, line_no: int, text: str, kind: str = 'note', side: str = '+') -> None:
        store = ReviewStore(self.gitr_dir)
        sha = store.write_snapshot(self.files['f.py'])
        line = self.files['f.py'].splitlines()[line_no - 1]
        store.add('f.py', sha, line_no, side, side + line, text, kind)

    def resolved(self):
        return _resolve_anchors(ReviewStore(self.gitr_dir), self.files.get)['f.py']

    def test_comment_follows_its_line(self):
        self.comment(2, 'why 1?', 'bad')
        self.files['f.py'] = 'import os\n\n' + BEFORE   # two lines above it
        [a] = self.resolved()
        self.assertEqual((a.target_line_no, a.kind, a.comment), (4, 'bad', 'why 1?'))

    def test_comment_stays_when_lines_below_change(self):
        self.comment(2, 'ok')
        self.files['f.py'] = BEFORE.replace('return a', 'return a + 1')
        [a] = self.resolved()
        self.assertEqual(a.target_line_no, 2)

    def test_stored_and_read_back(self):
        self.comment(3, 'first')
        self.comment(1, 'second', 'good')
        got = sorted((a.target_line_no, a.kind, a.comment) for a in self.resolved())
        self.assertEqual(got, [(1, 'good', 'second'), (3, 'note', 'first')])

    def test_same_line_replaces(self):
        self.comment(2, 'one')
        self.comment(2, 'two')
        self.assertEqual([a.comment for a in self.resolved()], ['two'])

    def test_comment_id_stable_across_edits(self):
        entry = {'snapshot': 'abc', 'line_no': 3, 'side': '+'}
        self.assertEqual(_comment_id('f.py', entry), _comment_id('f.py', {**entry, 'comment': 'edited'}))
        self.assertNotEqual(_comment_id('f.py', entry), _comment_id('f.py', {**entry, 'line_no': 4}))


if __name__ == '__main__':
    unittest.main()
