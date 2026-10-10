import json
from pathlib import Path
import tempfile
import unittest
from cache_publications import summarize


class LagBoundTests(unittest.TestCase):
    def test_pre_read_timestamp_and_final_drain_use_later_bound(self):
        # The first publication read can complete after its timestamp. Its next
        # sample bounds that completion; a missed final drain uses completion.
        observations = [
            dict(elapsed_seconds=0, committed_rows=0, publication=dict(rows=0)),
            dict(elapsed_seconds=1, committed_rows=13_000_000, publication=dict(rows=13_000_000)),
            dict(elapsed_seconds=2, committed_rows=14_600_000, publication=dict(rows=14_600_000)),
            dict(elapsed_seconds=3, committed_rows=14_600_000, publication=dict(rows=14_600_000)),
        ]
        acks = [dict(kind='ack', rows=n, elapsed_seconds=t) for n, t in
                [(13_000_000, .5), (1_600_000, 1.9), (170_127, 3.2)]]
        publications = [dict(at_ms=t, rows=n) for t, n in
                        [(100, 0), (1750, 13_000_000), (2000, 14_600_000), (3500, 14_770_127)]]
        report = dict(status='PREFIX_PASS', stopped=True, committed_rows=14_770_127, elapsed_seconds=4)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, records in [('observations', observations), ('commits', acks)]:
                (root / (name + '.jsonl')).write_text(''.join(json.dumps(r) + '\n' for r in records))
            result = summarize(root, publications, report)
            lag = result['commit_ack_to_publication_upper_bound_seconds']
            self.assertEqual(lag['n'], 3)
            self.assertAlmostEqual(lag['maximum'], 1.5)
            self.assertAlmostEqual(lag['p50'], 1.1)
            self.assertEqual(result['late_publication_window']['rows'], 1_600_000)
            with self.assertRaises(AssertionError):
                summarize(root, publications[:-1], report)
            for index, publication in enumerate(publications): publication['id'] = str(index)
            events = [dict(stage='apply.published', at_ms=p['at_ms']+2, fields=dict(id=p['id'])) for p in publications[1:]]
            (root / 'source-profile').mkdir()
            totals = (13_000_000, 14_600_000, 14_770_127)
            (root / 'source-profile/transactions.jsonl').write_text(''.join(
                json.dumps(dict(success=True, committed_rows=n, end_unix_ns=int(ack['elapsed_seconds']*1e9))) + '\n'
                for n, ack in zip(totals, acks)))
            exact = summarize(root, publications, report, events)
            lag = exact['post_commit_event_lag_upper_bound_seconds']
            self.assertAlmostEqual(lag['maximum'], 1.253)
            self.assertAlmostEqual(lag['p50'], .303)
            with self.assertRaisesRegex(AssertionError, 'ambiguous repeated'):
                summarize(root, publications, report, events + events[:1])

