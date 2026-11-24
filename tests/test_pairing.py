"""Which removed and added lines the word diff takes for one edited line
(the rest show as removed or added whole)."""

import unittest

import gitr
from gitr import _pair_lines_for_word_diff as pair


def pairs(rem, add):
    return [(a[1], a[2]) for a in pair(rem, add) if a[0] == 'pair']


class PairingTest(unittest.TestCase):
    def test_version_bumps_pair(self):
        # a long word matches, short numbers differ: counted by length
        rem = ['PyJWT[crypto]==2.13.0', 'websockets==16.0', 'emoji==2.15.0']
        add = ['humanize==4.17.0', 'PyJWT[crypto]==2.15.1', 'websockets==17.4', 'emoji==2.16.0']
        self.assertEqual(pairs(rem, add), list(zip(rem, add[1:])))

    def test_shared_punctuation_alone_does_not_pair(self):
        self.assertEqual(pairs(['print(first_val(nums))'], ['p = back(P(3))']), [])
        self.assertEqual(pairs(['print(passthrough(7))'], ['c = C[int32]()']), [])

    def test_edits_pair(self):
        rem = ['  foo(a, b)', 'x = compute(1)', '}']
        add = ['    foo(a, c)', 'x = compute(2)', '});']
        self.assertEqual(pairs(rem, add), list(zip(rem, add)))

    def test_best_partner_not_the_next_one(self):
        rem = ['result = compute(value)']
        add = ['log("start")', 'result = compute(other_value)']
        self.assertEqual(pairs(rem, add), [(rem[0], add[1])])

    def test_whitespace_only_changes_flagged(self):
        out = pair(['    if a:', '   ', '        x = 1'], ['if a:', '', '    x = 1'])
        self.assertEqual([(a[0], a[3]) for a in out], [('pair', True)] * 3)

    def test_unpaired_lines_listed_in_order(self):
        out = pair(['gone()', 'x = compute(1)'], ['x = compute(2)', 'added()'])
        self.assertEqual([a[0] for a in out], ['rem', 'pair', 'add'])

    def test_big_block_pairs_in_order(self):
        # above WORD_DIFF_MAX_BLOCK the matching is sequential
        n = int(gitr.WORD_DIFF_MAX_BLOCK ** .5) + 1
        rem = [f'value_{i} = compute({i})' for i in range(n)]
        add = [f'value_{i} = compute({i + 1})' for i in range(n)]
        self.assertEqual(pairs(rem, add), list(zip(rem, add)))


if __name__ == '__main__':
    unittest.main()
