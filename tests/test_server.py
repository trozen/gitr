"""gitr serve's diff document and its git queries, on throwaway repos."""

import os
import re
import unittest
from pathlib import Path

from gitr import ReviewStore, parse_diff
from gitr_web import server

from tests.helpers import GitEnv, Repo


def setUpModule():
    GitEnv.enter()


def tearDownModule():
    GitEnv.leave()


class WordRangesTest(unittest.TestCase):
    """What /api/words tells the page: changed words of edited lines, and
    the lines of such a block added or removed whole (line, 0, 0)."""

    def ranges(self, diff: str):
        doc = server.Doc(parse_diff(diff))
        flat = doc.word_ranges(1, len(doc.lines))
        out: dict[str, list] = {}
        for i in range(0, len(flat), 3):
            line = doc.lines[flat[i] - 1]
            out.setdefault(line, []).append(None if flat[i + 1] == 0 else line[flat[i + 1] - 1:flat[i + 2] - 1])
        return out

    def test_edited_and_whole_lines(self):
        diff = ('diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n'
                '-x = compute(1)\n'
                ' keep\n'
                '+x = compute(2)\n'  # a block of its own, after the context: no pair
                '+x = compute(3)\n')
        got = self.ranges(diff)
        self.assertEqual(got, {})  # no block holds both removed and added lines

        diff = ('diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,1 +1,2 @@\n'
                '-x = compute(1)\n'
                '+# a new comment line\n'
                '+x = compute(2)\n')
        got = self.ranges(diff)
        self.assertEqual(got['-x = compute(1)'], ['1'])
        self.assertEqual(got['+x = compute(2)'], ['2'])
        self.assertEqual(got['+# a new comment line'], [None])

    def test_added_only_block_has_no_ranges(self):
        diff = 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,2 @@\n+a\n+b\n'
        self.assertEqual(self.ranges(diff), {})


class RepoTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()


class CommentCommitsTest(RepoTest):
    """Per commit, the comments on lines it changed (the commits pane's marks)."""

    def comment(self, rel: str, line_no: int, text: str, kind: str, side: str = '+', line_text: str = ''):
        store = ReviewStore(Path(self.repo.path) / '.gitr')
        content = self.repo.read(rel)
        sha = store.write_snapshot(content)
        line_text = line_text or side + content.splitlines()[line_no - 1]
        store.add(rel, sha, line_no, side, line_text, text, kind)

    def test_marks(self):
        r = self.repo
        r.write('a.py', 'l1\nl2\nl3\nl4\nl5\n')
        base = r.commit('base')
        r.git('checkout', '-q', '-b', 'feature')
        r.write('a.py', 'l1\nl2 changed\nl3\nl4\nl5\n')
        c1 = r.commit('c1')
        r.write('a.py', 'l1\nl2 changed\nl3\nl4\nl5\nnew6\n')
        c2 = r.commit('c2')
        r.write('a.py', 'l1\nl2 changed\nl3\nl5\nnew6\n')
        c3 = r.commit('c3')
        r.write('a.py', 'l1\nl2 changed\nl3\nl5 edited\nnew6\n')
        r.write('u.txt', 'untracked\n')
        r.write('s.txt', 'staged\n')
        r.git('add', 's.txt')

        self.comment('a.py', 1, 'context line', 'note')
        self.comment('a.py', 2, 'from c1', 'bad')
        self.comment('a.py', 5, 'from c2', 'note')
        self.comment('a.py', 4, 'not committed', 'good')
        self.comment('a.py', 4, 'removed in c3', 'bad', side='-', line_text='-l4')
        self.comment('u.txt', 1, 'untracked file', 'note')
        self.comment('s.txt', 1, 'staged, not in HEAD', 'bad')

        marks = server.comment_commits(r.path, 'main', ReviewStore(Path(r.path) / '.gitr'))
        self.assertEqual(marks, {
            base: {'note': 1}, c1: {'bad': 1}, c2: {'note': 1}, c3: {'bad': 1},
            server.UNCOMMITTED: {'good': 1, 'bad': 1}, server.UNTRACKED: {'note': 1},
        })

    def test_removed_in_the_working_tree(self):
        r = self.repo
        r.write('a.py', 'one\ntwo\nthree\n')
        r.commit('base')
        r.write('a.py', 'one\nthree\n')
        self.comment('a.py', 2, 'why gone?', 'note', side='-', line_text='-two')
        marks = server.comment_commits(r.path, 'main', ReviewStore(Path(r.path) / '.gitr'))
        self.assertEqual(marks, {server.UNCOMMITTED: {'note': 1}})

    def test_no_comments(self):
        self.repo.write('a', 'x\n')
        self.repo.commit('base')
        self.assertEqual(server.comment_commits(self.repo.path, 'main', ReviewStore(Path(self.repo.path) / '.gitr')), {})


class CommitsTest(RepoTest):
    def test_list_and_boundary(self):
        r = self.repo
        r.write('a', '1\n')
        base = r.commit('base')
        r.git('checkout', '-q', '-b', 'feature')
        r.write('a', '2\n')
        c1 = r.commit('one')
        r.write('a', '3\n')
        c2 = r.commit('two')
        commits = server.list_commits(r.path, 'main')
        self.assertEqual([(c['hash'], c['boundary']) for c in commits], [(c2, False), (c1, False), (base, True)])

    def test_refs_stamp_follows_a_remote_branch(self):
        r = self.repo
        r.write('a', '1\n')
        first = r.commit('one')
        r.write('a', '2\n')
        r.commit('two')
        before = server.refs_stamp(r.path)
        self.assertEqual(server.refs_stamp(r.path), before)
        r.git('update-ref', 'refs/remotes/origin/main', first)  # what a push or fetch does
        self.assertNotEqual(server.refs_stamp(r.path), before)


class HerdrReposTest(RepoTest):
    """--herdr: a pane's directory to the repo it belongs to."""

    def test_repo_of_dir(self):
        r = self.repo
        r.write('src/a', '1\n')
        r.commit('base')
        real = os.path.realpath(r.path)
        self.assertEqual(server.repo_of_dir(os.path.join(r.path, 'src')), real)
        wt = os.path.join(os.path.dirname(r.path), os.path.basename(r.path) + '-wt')
        r.git('worktree', 'add', '-q', '-b', 'agent', wt)
        try:
            self.assertEqual(server.repo_of_dir(wt), real)  # a linked worktree: its repo
        finally:
            r.git('worktree', 'remove', '--force', wt)
        self.assertIsNone(server.repo_of_dir('/'))
        self.assertIsNone(server.repo_of_dir(os.path.join(r.path, '.git', 'objects')))

    def test_not_the_home_directory(self):
        # a repo of dotfiles in ~ is no project of a pane outside the others
        self.repo.write('.bashrc', 'x\n')
        self.repo.commit('dotfiles')
        sub = os.path.join(self.repo.path, 'Downloads')
        os.mkdir(sub)
        old = os.environ.get('HOME')
        os.environ['HOME'] = self.repo.path
        try:
            self.assertIsNone(server.repo_of_dir(sub))
            self.assertIsNone(server.repo_of_dir(self.repo.path))
        finally:
            if old is None:
                del os.environ['HOME']
            else:
                os.environ['HOME'] = old


class HerdrPluginTest(unittest.TestCase):
    def test_version_is_gitrs(self):
        # the plugin runs the gitr of its checkout: herdr's marketplace
        # shows the plugin's version, which is to say which gitr it is
        root = Path(__file__).resolve().parent.parent
        version = r'^version = "([^"]+)"'
        manifest = re.search(version, (root / 'herdr-plugin' / 'herdr-plugin.toml').read_text(), re.M)
        project = re.search(version, (root / 'pyproject.toml').read_text(), re.M)
        self.assertEqual(manifest.group(1), project.group(1))


if __name__ == '__main__':
    unittest.main()
