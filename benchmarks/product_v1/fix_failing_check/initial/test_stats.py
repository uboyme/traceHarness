import unittest

from stats import mean, spread


class MeanTests(unittest.TestCase):
    def test_mean_of_three_values(self) -> None:
        self.assertEqual(mean([1, 2, 3]), 2)

    def test_mean_of_one_value(self) -> None:
        self.assertEqual(mean([7]), 7)

    def test_mean_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            mean([])


class SpreadTests(unittest.TestCase):
    def test_spread(self) -> None:
        self.assertEqual(spread([4, 1, 9]), 8)


if __name__ == "__main__":
    unittest.main()
