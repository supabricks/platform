#!/usr/bin/env python3
"""Freeze publication boundaries and calculate a conservative sampled lag bound."""
import argparse
import bisect
import json
from pathlib import Path
import sqlite3
from analyze_source_profile import rows, distribution


def collect(root):
    # Export only non-secret publication identity, counts and timestamps.
    with sqlite3.connect((root / 'state/state.sqlite3').resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        result = []
        for identifier, at, raw in db.execute("SELECT export_id,published_at_ms,descriptor FROM publications WHERE state='published' ORDER BY ordinal"):
            descriptor = json.loads(raw); manifest = descriptor['manifest']
            result.append(dict(id=identifier, at_ms=at, rows=sum(t['rows'] for t in manifest['tables']), epoch=descriptor['epoch_id']))
        return result


def summarize(root, publications, report, events=None):
    assert report['status'] == 'PREFIX_PASS' and report['stopped']
    assert publications[-1]['rows'] == report['committed_rows']
    assert all(a['rows'] <= b['rows'] and a['at_ms'] <= b['at_ms'] for a, b in zip(publications, publications[1:]))
    start = next(p for p in publications if p['rows'] >= 13_000_000)
    end = next(p for p in publications if p['rows'] >= 14_600_000)
    elapsed = (end['at_ms'] - start['at_ms']) / 1000
    observations = list(rows(root / 'observations.jsonl'))
    covered = [(o.get('publication') or {}).get('rows', 0) for o in observations]
    assert all(a <= b for a, b in zip(covered, covered[1:]))
    assert all(n <= o['committed_rows'] for n, o in zip(covered, observations))
    # The frozen sampler timestamps BEFORE fetching its publication. The next
    # sample's start is after this sample's read, giving an actual upper bound.
    # The completed checkpoint bounds the final read/drain and shutdown.
    upper_times = [o['elapsed_seconds'] for o in observations[1:]] + [report['elapsed_seconds']]
    assert all(a <= b for a, b in zip(upper_times, upper_times[1:]))
    if covered[-1] < report['committed_rows']:
        covered.append(report['committed_rows']); upper_times.append(report['elapsed_seconds'])
    count = 0; lags = []
    for ack in rows(root / 'commits.jsonl'):
        if ack['kind'] != 'ack': continue
        count += ack['rows']; index = bisect.bisect_left(covered, count)
        assert index < len(covered)
        assert upper_times[index] >= ack['elapsed_seconds']
        lags.append(upper_times[index] - ack['elapsed_seconds'])
    assert count == report['committed_rows']
    result = dict(late_publication_window=dict(first=start, last=end, seconds=elapsed,
                    rows=end['rows']-start['rows'], rows_s=(end['rows']-start['rows'])/elapsed),
                commit_ack_to_publication_upper_bound_seconds=distribution(lags),
                maximum_sampled_backlog_rows=max(o['committed_rows']-n for n, o in zip(covered, observations)),
                lag_scope='Conservative: next sample start after the first covering publication read, or final completed checkpoint. Includes an extra observation interval; not directly comparable with earlier pre-read timestamp estimates.',
                cohort_scope='Publication endpoints round up at 13.0m and 14.6m; separate from exact source acknowledgment cohort.')
    if events is not None:
        published = {}
        for event in events:
            if event['stage'] == 'apply.published':
                key = event['fields']['id']
                assert key not in published, 'ambiguous repeated publication event'
                published[key] = event['at_ms']
        positive = [p for p in publications if p['rows'] > 0]
        for publication in positive:
            assert published[publication['id']] >= publication['at_ms']
        totals = [p['rows'] for p in positive]; precise = []; before_ack = 0
        transactions = list(rows(root / 'source-profile/transactions.jsonl'))
        assert transactions[-1]['committed_rows'] == report['committed_rows']
        for transaction in transactions:
            assert transaction['success']
            publication = positive[bisect.bisect_left(totals, transaction['committed_rows'])]
            # This event is emitted after SQLite commits the publication/head.
            # Add 1 ms for the event timestamp's truncation, on the shared host
            # wall clock. Visibility before client acknowledgment means zero lag.
            lag = (published[publication['id']] + 1) / 1000 - transaction['end_unix_ns'] / 1e9
            before_ack += lag < 0; precise.append(max(0, lag))
        result['post_commit_event_lag_upper_bound_seconds'] = distribution(precise)
        result['post_commit_event_before_ack_count'] = before_ack
        result['event_lag_scope'] = 'First covering publication by exact cumulative rows in this single-loader insert-only fixture; apply.published is after the SQLite commit, plus 1 ms for timestamp truncation. Source end_unix_ns records client COMMIT acknowledgment. Shared host wall clock; includes event emission delay.'
    return result



if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path); p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.write_text(json.dumps(collect(args.root), indent=2) + '\n')
