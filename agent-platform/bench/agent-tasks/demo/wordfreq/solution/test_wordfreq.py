import collections
import unittest

from wordfreq import top


class T(unittest.TestCase):
    def test_top_order(self):
        self.assertEqual(
            top(collections.Counter({"b": 2, "a": 2, "c": 3}), 2), [("c", 3), ("a", 2)]
        )
