import unittest
from host_io_analysis import summarize

class HostIOAnalysisTests(unittest.TestCase):
    def test_only_load_window_and_readable_stable_identity_count(self):
        def row(at,write):
            return dict(at_ms=at,permission_denied=1,processes=[dict(pid=1,start_ticks=1,role='postgres',cgroup_sha256='owned',user_ticks=0,system_ticks=0,io=dict(read_bytes=0,write_bytes=write)),dict(pid=2,start_ticks=1,role='other',cgroup_sha256='external',user_ticks=0,system_ticks=0,io=None)])
        d=dict(owned_container_cgroup_sha256='owned',error=None,samples=[row(0,0),row(1000,100),row(2000,150),row(3000,1000)])
        out=summarize(d,1000,2000)
        self.assertEqual(out['process_counter_deltas'][0]['write_bytes'],50)
        self.assertEqual(out['permission_denied_samples'],2)
        self.assertEqual(out['coverage_gaps']['unavailable_intervals'],1)
        self.assertTrue(all(r['owned'] for r in out['process_counter_deltas']))

    def test_missing_owned_binding_cannot_be_attributed(self):
        with self.assertRaises(AssertionError):summarize(dict(owned_container_cgroup_sha256=None,error=None),0,100)

    def test_parent_child_counters_are_not_summed_as_physical_io(self):
        def row(at,values):
            return dict(at_ms=at,permission_denied=0,processes=[dict(pid=i,start_ticks=1,role='other',cgroup_sha256='owned',user_ticks=0,system_ticks=0,io=dict(read_bytes=0,write_bytes=v)) for i,v in enumerate(values,1)])
        data=dict(owned_container_cgroup_sha256='owned',error=None,samples=[row(0,[0,0]),row(1000,[100,100])])
        out=summarize(data,0,1000)
        self.assertNotIn('visible_group_deltas',out)
        self.assertEqual([r['write_bytes'] for r in out['process_counter_deltas']],[100,100])
        self.assertIn('never sum',out['scope'])
