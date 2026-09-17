import unittest
from magi1.coverage_stats import reception_summary


class CoverageTests(unittest.TestCase):
    def test_missing_is_not_failure(self):
        x = reception_summary(20, 60, 20)
        self.assertEqual(x['observed_probability'], .25)
        self.assertEqual(x['missing_outcome_bounds'], [.2, .4])
        self.assertEqual(x['coverage'], .8)

    def test_all_missing_and_empty(self):
        self.assertIsNone(reception_summary(0, 0, 10)['observed_probability'])
        self.assertEqual(reception_summary(0, 0, 10)['missing_outcome_bounds'], [0., 1.])
        self.assertEqual(reception_summary(0, 0, 0)['missing_outcome_bounds'], [None, None])
