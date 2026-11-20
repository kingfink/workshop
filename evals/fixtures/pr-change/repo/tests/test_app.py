import unittest

from app import total


class TotalTest(unittest.TestCase):
    def test_sum(self):
        self.assertEqual(total([2, 3]), 5)

    def test_empty(self):
        self.assertEqual(total([]), 0)
