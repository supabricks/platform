"""Lossless SP11 observer storage must preserve attribution and fail closed."""
import unittest
from types import SimpleNamespace
from packed_observer import PackedMarkers, PackedSamples
from trial import attribute


class PackedEvidence(unittest.TestCase):
    def test_source_order_and_values_are_preserved(self):
        records=[dict(xid=x,ack_ms=a,latency_ms=.125,late_ms=.0625)
                 for x,a in [(2**32-1,3.25),(0,1.125),(42,2.5)]]
        combined=PackedSamples()
        for r in records:
            local=PackedSamples();local.append(r);combined.extend(local)
        self.assertEqual(list(combined.sort_by_ack()),sorted(records,key=lambda r:r['ack_ms']))
        self.assertEqual(combined[-1],records[0])
        self.assertEqual(len(combined.data),3*28)
        with self.assertRaises(ValueError):combined.append(records[0])
        with self.assertRaises(IndexError):_ = combined[3]

    def test_sparse_out_of_order_and_wrapped_xids_preserve_presence(self):
        for code in ('Q','d'):
            values=PackedMarkers(code)
            expected={2**32-1:10,0:0,90000:27,4:11}
            for k,v in expected.items():values[k]=v
            self.assertEqual(dict(values),expected)
            self.assertNotIn(3,values)
            self.assertEqual(values.setdefault(4,99),11)
            with self.assertRaisesRegex(ValueError,'ambiguous'):values[4]=12
            with self.assertRaises(KeyError):_ = values[2**32]

    def test_budget_exhaustion_cannot_silently_drop_evidence(self):
        samples=PackedSamples(budget=28)
        row=dict(xid=1,ack_ms=1,latency_ms=1,late_ms=0)
        samples.append(row)
        with self.assertRaisesRegex(ValueError,'budget'):samples.append(row)
        with self.assertRaisesRegex(ValueError,'budget'):samples.extend(samples)
        with self.assertRaisesRegex(ValueError,'fields'):PackedSamples().append(dict(row,sql_ms={}))
        values=PackedMarkers('Q',budget=4096*9);values[0]=1
        with self.assertRaisesRegex(ValueError,'budget'):values[4096]=1
        self.assertEqual(dict(values),{0:1})

    def test_stage_attribution_matches_original_including_negative_lag_floor(self):
        records=[dict(xid=9,ack_ms=51,latency_ms=2,late_ms=0),
                 dict(xid=8,ack_ms=15,latency_ms=3,late_ms=1)]
        observer=SimpleNamespace(ends={9:150,8:120},captured={9:24,8:19},publications=[
            dict(end=130,at=30,run='a',prepared=27),dict(end=160,at=50,run='b',prepared=45)])
        runs={'a':dict(created_at_ms=20,started_at_ms=22),'b':dict(created_at_ms=32,started_at_ms=35)}
        expected=attribute(records,observer,runs)
        samples=PackedSamples()
        for row in records:samples.append(row)
        for name,code in [('ends','Q'),('captured','d')]:
            compact=PackedMarkers(code);compact.update(getattr(observer,name));setattr(observer,name,compact)
        self.assertEqual(attribute(samples.sort_by_ack(),observer,runs),expected)
        del observer.ends[8]
        with self.assertRaisesRegex(AssertionError,'missed a pruned'):attribute(samples,observer,runs)


if __name__=='__main__':unittest.main()
