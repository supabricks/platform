import gzip
import tempfile
import unittest
from unittest.mock import Mock, patch
from sp11_campaign import stop_interrupted
from pathlib import Path
from sp11_analysis import EVENT, analyze, read_events


class InterruptedContainer(unittest.TestCase):
    @patch('sp11_campaign.subprocess.run')
    def test_exited_client_does_not_imply_container_stopped(self,run):
        child=Mock();child.poll.return_value=-15
        stop_interrupted(child,'owned-sp11-fixture',False)
        run.assert_called_once_with(['docker','stop','--time','30','owned-sp11-fixture'],capture_output=True,timeout=45)
        child.wait.assert_not_called()

    @patch('sp11_campaign.subprocess.run')
    def test_completed_fixture_needs_no_additional_stop(self,run):
        stop_interrupted(Mock(),'completed',True);run.assert_not_called()


class SteadyWindows(unittest.TestCase):
    def fixture(self):
        # 1000 rows/s for ten minutes; last publications require a short drain.
        events=[(t,t+2000) for t in range(1,600000,2)]
        series=[dict(at_ms=t,backlog_bytes=100,memory_bytes=1024**3) for t in range(0,600001,1000)]
        report=dict(status='measured',measurement_start_ms=0,measurement_end_ms=600000,
                    source=dict(completed_transactions=len(events)),backlog_series=series,drain_seconds=2)
        report['cgroup_before']=report['cgroup_after']={'memory.events':'high 0\nmax 0\noom 0\noom_kill 0\n'}
        resources=[dict(at_ms=t,spool_bytes=1024,processes=[],qualifier_rss_bytes=1024,
            cgroup_memory_bytes=1024**3,cgroup_memory_stat=dict(anon=1024**3,file=0,kernel=0,
                inactive_file=0,file_dirty=0,file_writeback=0)) for t in range(0,600001,1000)]
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
        for row in s:
            if row['at_ms']>300000:row['cgroup_memory_bytes']=3*1024**3
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

    def test_only_clean_inactive_cache_is_discounted(self):
        r,e,s=self.fixture()
        for row in r['backlog_series']:
            if row['at_ms']>300000:row['memory_bytes']=3*1024**3
        for row in s:
            if row['at_ms']>300000:
                row['cgroup_memory_bytes']=3*1024**3
                row['cgroup_memory_stat'].update(file=2*1024**3,inactive_file=2*1024**3)
        out=analyze(r,e,s)
        self.assertTrue(out['gates']['memory_growth'])
        self.assertFalse(out['raw_cgroup_memory_growth_screen'])
        # Dirty or writeback cache cannot disguise retained working memory.
        for field in ('file_dirty','file_writeback'):
            for row in s:
                if row['at_ms']>300000:row['cgroup_memory_stat'][field]=1024**3
            self.assertFalse(analyze(r,e,s)['gates']['memory_growth'])
            for row in s:row['cgroup_memory_stat'][field]=0

    def test_pressure_and_missing_attribution_cannot_qualify(self):
        r,e,s=self.fixture()
        r['cgroup_after']={'memory.events':'high 0\nmax 1\noom 0\noom_kill 0\n'}
        self.assertFalse(analyze(r,e,s)['gates']['no_memory_pressure'])
        del s[-1]['cgroup_memory_stat']['inactive_file']
        with self.assertRaises(KeyError):analyze(r,e,s)


if __name__=='__main__':unittest.main()
