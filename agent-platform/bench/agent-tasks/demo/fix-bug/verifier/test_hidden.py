import unittest

from calc import average, median


class TestHidden(unittest.TestCase):
    def test_average_single(self):
        self.assertEqual(average([7]), 7)

    def test_median_even_unsorted(self):
        self.assertEqual(median([10, 2, 8, 4]), 6)

    def test_median_empty(self):
        with self.assertRaises(ValueError):
            median([])
