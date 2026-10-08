"""Source capacity cannot count late acknowledgments or hide weak rate windows."""
import unittest
from source_trial import summarize,windows
from source_capacity import blocks

class SourceAccounting(unittest.TestCase):
    def sample(self,stamp,latency=10):return [stamp,latency,1,2,1,6]
    def test_window_boundaries_and_late_commits(self):
        samples=[self.sample(t) for t in (0,59.999,60,119.999,120,299.999,300,301)]
        result=summarize(samples,300,301)
        self.assertEqual(result['in_window_transactions'],6)
        self.assertEqual(result['tail_transactions'],2)
        self.assertEqual(result['committed_changed_rows_s'],.04)
        self.assertEqual([x['transactions'] for x in result['windows']],[2,2,1,0,1])
        self.assertIsNone(result['windows'][3]['latency_ms'])
        self.assertEqual(sum(w['transactions'] for w in result['windows']),6)
    def test_disabled_timing_is_unavailable_not_zero(self):
        report=summarize([[1,10,None,None,None,None]],60,60)
        self.assertEqual(report['sql_ms'],{})
        self.assertEqual(report['transaction_ms']['p95'],10)
    def test_short_elapsed_cannot_claim_complete_measurement(self):
        with self.assertRaises(AssertionError):summarize([self.sample(1)],300,299)
    def test_screen_is_balanced_and_reproducible(self):
        screen=blocks('screen',[8,16],[4,8,16],3,20260926)
        self.assertEqual(screen,blocks('screen',[8,16],[4,8,16],3,20260926))
        for cpu in (8,16):
            selected=[b for b in screen if b['cpus']==cpu]
            self.assertEqual(len(selected),3)
            self.assertEqual({b['variants'][0]['clients'] for b in selected},{4,8,16})
            self.assertTrue(all(len({v['clients'] for v in b['variants']})==3 for b in selected))
    def test_activation_controls_change_only_profiler(self):
        control=blocks('controls',[8,16],[8],3,20260926)
        for b in control:
            self.assertEqual({v['clients'] for v in b['variants']},{8})
            self.assertEqual({v['profile'] for v in b['variants']},{True,False})
        self.assertEqual({b['variants'][0]['profile'] for b in control},{True,False})

class SourceSelection(unittest.TestCase):
    def trials(self):
        return [dict(cpus=cpu,clients=client,repeat=repeat,profile=True,seconds=300,
                     minute_rows_s=[{4:850,8:1500,16:2400}[client]]*5)
                for cpu in (8,16) for client in (4,8,16) for repeat in (1,2,3)]
    def test_smallest_count_must_pass_every_cpu_repeat_and_minute(self):
        from source_analysis import select_client
        trials=self.trials()
        self.assertEqual(select_client(trials)['clients'],8)
        next(t for t in trials if t['clients']==8)['minute_rows_s'][4]=1200
        self.assertEqual(select_client(trials)['clients'],16)
        next(t for t in trials if t['clients']==16)['minute_rows_s'][0]=999
        chosen=select_client(trials)
        self.assertEqual(chosen,dict(clients=8,qualification_floor_rows_s=1000,preferred_headroom=False))
    def test_incomplete_cell_and_low_minute_cannot_qualify(self):
        from source_analysis import select_client
        trials=[t for t in self.trials() if t['clients']==8]
        self.assertIsNone(select_client(trials[:-1]))
        trials[-1]['minute_rows_s'][2]=999
        self.assertIsNone(select_client(trials))
    def test_duplicate_repeat_cannot_replace_independent_trial(self):
        from source_analysis import select_client
        trials=[t for t in self.trials() if t['clients']==8]
        trials[-1]['repeat']=trials[-2]['repeat']
        self.assertIsNone(select_client(trials))

class HostDiskAttribution(unittest.TestCase):
    def test_intermediate_decrease_is_unavailable_even_if_end_exceeds_start(self):
        from source_analysis import host_disk_summary
        rows=[dict(at_ms=i*1000,host_disks={'1:0':dict(io_ms=v,weighted_io_ms=v*2)})
              for i,v in enumerate((100,50,200))]
        result=host_disk_summary(rows)['1:0']
        self.assertIsNone(result['busy_percent'])
        self.assertIsNone(result['delta'])
        self.assertEqual(len(result['discontinuities']),1)
    def test_missing_device_is_unavailable_and_stable_device_retains_delta(self):
        from source_analysis import host_disk_summary
        rows=[dict(at_ms=0,host_disks={'1:0':dict(io_ms=100,weighted_io_ms=200),'2:0':dict(io_ms=0,weighted_io_ms=0)}),
              dict(at_ms=1000,host_disks={'1:0':dict(io_ms=150,weighted_io_ms=400)})]
        result=host_disk_summary(rows)
        self.assertEqual(result['1:0']['busy_percent'],5)
        self.assertEqual(result['1:0']['weighted_queue_mean'],.2)
        self.assertIsNone(result['2:0']['delta'])
