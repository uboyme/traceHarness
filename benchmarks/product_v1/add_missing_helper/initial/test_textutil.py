import unittest

import textutil


class NormalizeTests(unittest.TestCase):
    def test_collapses_whitespace(self) -> None:
        self.assertEqual(textutil.normalize("  a   b \n c "), "a b c")


class SlugifyTests(unittest.TestCase):
    def test_lowercases_and_joins_with_hyphens(self) -> None:
        self.assertEqual(textutil.slugify("Hello   World"), "hello-world")

    def test_drops_characters_that_are_not_letters_or_digits(self) -> None:
        self.assertEqual(textutil.slugify("A/B: c!"), "a-b-c")

    def test_rejects_text_with_no_usable_characters(self) -> None:
        with self.assertRaises(ValueError):
            textutil.slugify("   ***   ")


if __name__ == "__main__":
    unittest.main()
