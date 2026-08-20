import unittest


class DeclaredFailureTest(unittest.TestCase):
    def test_declared_failure_is_not_hidden(self) -> None:
        self.fail("the fixed L3 task intentionally fails verification")
