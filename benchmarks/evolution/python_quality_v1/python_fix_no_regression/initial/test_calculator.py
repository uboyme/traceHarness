import unittest

from calculator import add


class CalculatorTest(unittest.TestCase):
    def test_addition(self) -> None:
        self.assertEqual(add(2, 3), 5)
