import unittest

from netconfig import parse_port


class ParsePortTests(unittest.TestCase):
    def test_accepts_a_valid_port(self) -> None:
        self.assertEqual(parse_port("8080"), 8080)

    def test_accepts_the_boundaries(self) -> None:
        self.assertEqual(parse_port("1"), 1)
        self.assertEqual(parse_port("65535"), 65535)

    def test_rejects_zero_and_negative_ports(self) -> None:
        for value in ("0", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_port(value)

    def test_rejects_ports_above_the_maximum(self) -> None:
        with self.assertRaises(ValueError):
            parse_port("65536")

    def test_rejects_text_that_is_not_a_number(self) -> None:
        with self.assertRaises(ValueError):
            parse_port("http")


if __name__ == "__main__":
    unittest.main()
