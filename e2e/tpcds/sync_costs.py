#!/usr/bin/env python3
"""Read-only batch timing attribution from retained native publication receipts.

Worker time is started_at -> manifest observed_at; the following interval ends
at publication commit. Delta execution and journal reads are nested worker
measurements, not additional wall time. This is not a sampled CPU profile.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import statistics


def batch_cost(record, published, descriptor):
    manifest = descriptor['manifest']
    started = record.get('started_at_ms')
    observed = manifest.get('observed_at_ms')
    if started is None or observed is None or observed < started:
        return None
    metrics = [item['metrics'] for item in manifest.get('apply_metrics', [])]
    return dict(run_id=record['id'], started_ms=started, manifest_ms=observed,
                published_ms=published, worker_ms=observed-started,
                after_manifest_ms=published-observed, cycle_ms=published-started,
                live_bytes=sum(f['bytes'] for f in manifest['files']),
                new_bytes=sum(m.get('new_parquet_bytes', 0) for m in metrics),
                rows=sum(m.get('num_added_rows', 0) for m in metrics),
                delta_execution_ms=sum(m.get('execution_time_ms', 0) for m in metrics),
                journal_ms=sum(r.get('elapsed_ms', 0) for r in record.get('journal_reads', [])))


def run(state):
    db = sqlite3.connect((state/'state.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    batches = []
    try:
        for record, published, descriptor in db.execute('''
            SELECT i.record,p.published_at_ms,p.descriptor FROM incremental_runs i
            JOIN publications p ON p.export_id=i.id
            WHERE p.state='published' ORDER BY p.ordinal'''):
            cost = batch_cost(json.loads(record), published, json.loads(descriptor))
            if cost is not None:
                batches.append(cost)
    finally:
        db.close()
    summaries = {}
    for label, selected in [('first_30', batches[:30]), ('last_30', batches[-30:])]:
        summaries[label] = dict(count=len(selected), medians={
            k:statistics.median(r[k] for r in selected)
            for k in ('worker_ms', 'after_manifest_ms', 'cycle_ms', 'live_bytes',
                      'new_bytes', 'rows', 'delta_execution_ms', 'journal_ms')
        }) if selected else dict(count=0)
    return dict(scope=__doc__, published_batches=len(batches), summaries=summaries, batches=batches)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with args.output.open('x') as stream:
        json.dump(run(args.state), stream, indent=2)
        stream.write('\n')
