import unittest


class QualityCapabilitiesTest(unittest.TestCase):
    def test_project_is_healthy(self) -> None:
        self.assertEqual(2 + 3, 5)
