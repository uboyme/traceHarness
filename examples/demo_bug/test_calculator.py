import unittest

from calculator import add


class CalculatorTests(unittest.TestCase):
    def test_addition(self) -> None:
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-1, 1), 0)


if __name__ == "__main__":
    unittest.main()
