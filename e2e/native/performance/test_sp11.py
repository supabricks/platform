import gzip
import tempfile
import unittest
from pathlib import Path
from sp11_analysis import EVENT, analyze, read_events


class SteadyWindows(unittest.TestCase):
    def fixture(self):
        # 1000 rows/s for ten minutes; last publications require a short drain.
        events=[(t,t+2000) for t in range(1,600000,2)]
        series=[dict(at_ms=t,backlog_bytes=100,memory_bytes=1024**3) for t in range(0,600001,1000)]
        report=dict(status='measured',measurement_start_ms=0,measurement_end_ms=600000,
                    source=dict(completed_transactions=len(events)),backlog_series=series,drain_seconds=2)
        resources=[dict(at_ms=t,spool_bytes=1024,processes=[]) for t in range(0,600001,1000)]
        return report,events,resources

    def test_healthy_headroom_passes_all_windows(self):
        r,e,s=self.fixture();e=[(t,t+2000) for t in range(1,600000)]
        r["source"]["completed_transactions"]=len(e)
        self.assertEqual(analyze(r,e,s)["status"],"passed")

    def test_late_publications_not_counted_in_measurement_window(self):
        r,e,s=self.fixture();out=analyze(r,e,s)
        self.assertLess(out['windows'][0]['published_rows_s'],out['windows'][0]['committed_rows_s'])
        self.assertEqual(out['worst_window_p95_ms'],2000)
        self.assertEqual(out['status'],'failed') # exactly 1000 offered; initial publication delay loses the floor

    def test_slow_tail_window_cannot_hide_in_whole_run_average(self):
        r,e,s=self.fixture();e=[(a,p+6000 if a>400000 else p) for a,p in e]
        out=analyze(r,e,s);self.assertFalse(out['gates']['all_fresh_windows'])
        self.assertEqual(out['worst_window_p95_ms'],8000)

    def test_missing_transaction_invalidates_result(self):
        r,e,s=self.fixture()
        with self.assertRaisesRegex(ValueError,'count'):analyze(r,e[:-1],s)

    def test_growth_and_missing_resources_fail_closed(self):
        r,e,s=self.fixture()
        for row in r['backlog_series']:
            if row['at_ms']>300000:row.update(backlog_bytes=4*1024**2,memory_bytes=3*1024**3)
        out=analyze(r,e,s)
        self.assertFalse(out['gates']['backlog_growth']);self.assertFalse(out['gates']['memory_growth'])
        with self.assertRaisesRegex(ValueError,'resource'):analyze(r,e,[])

    def test_sampler_gap_prevents_qualification(self):
        r,e,s=self.fixture();out=analyze(r,e,s[:5]+s[20:])
        self.assertFalse(out['gates']['continuous_resource_evidence'])

    def test_partial_event_stream_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'events.gz'
            with gzip.open(p,'wb') as f:f.write(EVENT.pack(1,2)+b'x')
            with self.assertRaisesRegex(ValueError,'size'):read_events(p)


if __name__=='__main__':unittest.main()
