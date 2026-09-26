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
