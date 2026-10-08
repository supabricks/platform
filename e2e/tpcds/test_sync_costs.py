import unittest
from sync_costs import batch_cost


class BatchCosts(unittest.TestCase):
    def test_phase_intervals_and_nested_metrics_are_not_added_twice(self):
        record = dict(id='batch', started_at_ms=100, journal_reads=[dict(elapsed_ms=7)])
        manifest = dict(observed_at_ms=300, files=[dict(bytes=1000)], apply_metrics=[dict(
            metrics=dict(num_added_rows=10, new_parquet_bytes=100, execution_time_ms=5))])
        cost = batch_cost(record, 800, dict(manifest=manifest))
        self.assertEqual((cost['worker_ms'], cost['after_manifest_ms'], cost['cycle_ms']), (200, 500, 700))
        self.assertEqual((cost['delta_execution_ms'], cost['journal_ms']), (5, 7))
        self.assertEqual((cost['live_bytes'], cost['new_bytes'], cost['rows']), (1000, 100, 10))

    def test_inherited_bootstrap_time_is_not_worker_elapsed_time(self):
        self.assertIsNone(batch_cost(dict(started_at_ms=100), 200,
                                     dict(manifest=dict(observed_at_ms=50))))


if __name__ == '__main__': unittest.main()
