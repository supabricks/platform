import copy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from q39_review import moments, oracle, review_rows, selected, ulps, validate_query, run


class Q39ReviewTest(unittest.TestCase):
    def setUp(self):
        self.groups = [dict(key=[1, 2, m], rows=[[1, 0], [2, 0], [3, 0], [4, 10]]) for m in (1, 2)]
        self.stats = moments(self.groups)
        self.rows = [['1', '2', '1', '2.5', '2.0', '1', '2', '2', '2.5', '2.0']]

    def test_independent_membership_and_means(self):
        self.assertEqual(selected(self.stats, 'q39b'), [(1, 2)])
        self.assertEqual(review_rows(self.rows, self.stats, 'q39a')['oracle_ulps'], {0: 2})

    def test_exact_threshold_excluded(self):
        groups = [dict(key=[1, 2, m], rows=[[1, 0], [2, 1], [3, 2]]) for m in (1, 2)]
        self.assertEqual(selected(moments(groups), 'q39a'), [])  # CV = exactly 1

    def test_null_empty_singleton_and_zero(self):
        for values in ([], [None], [7], [0, 0]):
            groups = [dict(key=[1, 2, m], rows=list(enumerate(values))) for m in (1, 2)]
            self.assertEqual(selected(moments(groups), 'q39a'), [])
        self.groups[0]['rows'].append([5, None])
        self.assertEqual(moments(self.groups), self.stats)

    def test_bound_is_two_representable_steps(self):
        for steps in (1, 2, 3):
            rows = copy.deepcopy(self.rows)
            value = 2.0
            for _ in range(steps):
                value = math.nextafter(value, math.inf)
            rows[0][4] = str(value)
            if steps <= 2:
                self.assertEqual(review_rows(rows, self.stats, 'q39a')['cells'][0]['oracle_ulps'], steps)
            else:
                with self.assertRaisesRegex(ValueError, 'exceeds 2 ULP'):
                    review_rows(rows, self.stats, 'q39a')

    def test_exact_columns_cannot_use_tolerance(self):
        for column in (0, 1, 2, 3, 5, 6, 7, 8):
            rows = copy.deepcopy(self.rows)
            rows[0][column] = str(math.nextafter(float(rows[0][column]), math.inf))
            with self.assertRaises(ValueError):
                review_rows(rows, self.stats, 'q39a')

    def test_extra_missing_duplicate_and_reordered_rows(self):
        for rows in ([], self.rows*2):
            with self.assertRaises(ValueError):
                review_rows(rows, self.stats, 'q39a')
        stats = moments(self.groups + [dict(g, key=[2, 2, g['key'][2]]) for g in self.groups])
        row2 = copy.deepcopy(self.rows[0]); row2[0] = row2[5] = '2'
        with self.assertRaisesRegex(ValueError, 'keys/order'):
            review_rows([row2, self.rows[0]], stats, 'q39a')

    def test_nonfinite_and_nonpositive_rejected(self):
        for value in ('nan', 'inf', '-inf', '0', '-1'):
            rows = copy.deepcopy(self.rows); rows[0][9] = value
            with self.assertRaises(ValueError):
                review_rows(rows, self.stats, 'q39a')

    def test_invalid_fixture(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            moments(self.groups * 2)
        self.groups[0]['rows'][0][1] = -1
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            moments(self.groups)

    def test_sql_schema_and_incomplete_status_rejected(self):
        query = dict(sql_sha256='original', status='complete', schema=dict(fields=[
            dict(type=t) for t in ['integer']*3+['double']*2+['integer']*3+['double']*2]))
        validate_query(query, 'original', 'spark')
        for field, value in [('sql_sha256', 'rewritten'), ('status', 'failed')]:
            altered = copy.deepcopy(query); altered[field] = value
            with self.assertRaises(ValueError):
                validate_query(altered, 'original', 'spark')
        query['schema']['fields'][4]['type'] = 'decimal(38,18)'
        with self.assertRaisesRegex(ValueError, 'types changed'):
            validate_query(query, 'original', 'spark')

    def test_group_fixture_tampering_rejected_before_reading_results(self):
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); groups = root/'groups.json'
            groups.write_text('{"groups": []}')
            with self.assertRaisesRegex(ValueError, 'fixture hash mismatch'):
                run(root/'absent-product', root/'absent-reference', groups, root/'strict', root/'output')
            self.assertFalse((root/'output').exists())

    def test_independent_high_precision_value(self):
        stat = moments([dict(key=[1, 2, 1], rows=list(enumerate([10, 404, 13, 814])))])[(1, 2, 1)]
        decimal80, nearest = oracle(stat)
        self.assertTrue(decimal80.startswith('1.235881377586132'))
        self.assertEqual(ulps(nearest, nearest), 0)


if __name__ == '__main__':
    unittest.main()
