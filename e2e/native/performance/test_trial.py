"""Measurement accounting must reject missing history and respect commit order."""
import unittest
from types import SimpleNamespace
from trial import attribute,cpu_delta
from matrix import affinity


class Accounting(unittest.TestCase):
    def test_transactions_map_by_commit_boundary_not_submission_order(self):
        observer=SimpleNamespace(ends={9:150,8:120},captured={9:24,8:19},publications=[
            dict(end=130,at=30,run='a',prepared=27),dict(end=160,at=50,run='b',prepared=45)])
        runs={'a':dict(created_at_ms=20,started_at_ms=22),'b':dict(created_at_ms=32,started_at_ms=35)}
        result=attribute([dict(xid=9,ack_ms=21),dict(xid=8,ack_ms=15)],observer,runs)
        self.assertEqual(result['commit_to_publication']['p95'],29)
        self.assertEqual(result['commit_to_admission']['p95'],11)
        del observer.ends[8]
        with self.assertRaisesRegex(AssertionError,'missed a pruned'):attribute([dict(xid=8,ack_ms=15)],observer,runs)

    def test_unpublished_and_regressed_cursors_are_invalid(self):
        for cuts in ([20,10],[5]):
            observer=SimpleNamespace(ends={1:30},captured={},publications=[dict(end=n) for n in cuts])
            with self.assertRaises(AssertionError):attribute([dict(xid=1,ack_ms=0)],observer,{})

    def test_affinity_preserves_sibling_pairs(self):
        groups=[[0,8],[1,9],[2,10],[3,11]]
        self.assertEqual(affinity(groups,4),[0,1,8,9])
        with self.assertRaises(ValueError):affinity(groups,3)
        with self.assertRaises(ValueError):affinity(groups,16)

    def test_cpu_uses_cgroup_delta_and_wall_time(self):
        a={'cpu.stat':'usage_usec 9000000\nnr_throttled 2\n'}
        b={'cpu.stat':'usage_usec 15000000\nnr_throttled 3\n'}
        self.assertEqual(cpu_delta(a,b,3),dict(usage_usec=6000000,nr_throttled=1,average_cpu_cores=2.0))


if __name__=='__main__':unittest.main()
