import unittest

from gitr import parse_diff

MODIFY = """\
diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1,4 +1,4 @@
 one
-two
+TWO
 three
--- not a header
"""


class ParseDiffTest(unittest.TestCase):
    def test_kinds_and_line_numbers(self):
        [f] = parse_diff(MODIFY)
        self.assertEqual(f.path, 'a.py')
        self.assertEqual(f.status, 'M')
        body = [(d.kind, d.old_line_no, d.new_line_no) for d in f.lines if d.kind != 'fileheader']
        self.assertEqual(body, [
            ('hunk', None, None),
            ('context', 1, 1),
            ('removed', 2, 2),   # a removed line sits where the next line is
            ('added', None, 2),
            ('context', 3, 3),
            ('removed', 4, 4),
        ])

    def test_removed_line_like_a_file_header(self):
        # inside a hunk only the first character counts: "--- x" removed "-- x"
        [f] = parse_diff(MODIFY)
        self.assertEqual(f.lines[-1].kind, 'removed')
        self.assertEqual(f.lines[-1].text, '--- not a header')

    def test_status_of_new_deleted_and_renamed_files(self):
        text = """\
diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 1111111..0000000
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
diff --git a/old/name.txt b/new/name.txt
similarity index 100%
rename from old/name.txt
rename to new/name.txt
"""
        files = parse_diff(text)
        self.assertEqual([(f.path, f.status, f.old_path) for f in files], [
            ('new.txt', 'A', ''), ('gone.txt', 'D', ''), ('new/name.txt', 'R', 'old/name.txt')])
        self.assertEqual([d.new_line_no for d in files[0].lines if d.kind == 'added'], [1])

    def test_hunk_without_counts(self):
        # "@@ -3 +3 @@" holds one line on each side
        text = 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -3 +3 @@\n-a\n+b\n'
        [f] = parse_diff(text)
        body = [(d.kind, d.old_line_no, d.new_line_no) for d in f.lines if d.kind in ('removed', 'added')]
        self.assertEqual(body, [('removed', 3, 3), ('added', None, 3)])


if __name__ == '__main__':
    unittest.main()
