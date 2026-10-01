#!/usr/bin/env python3
"""Attribute retained commit stalls; correlation is not a root-cause intervention."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path


def read(p):return json.loads(gzip.decompress(p.read_bytes()) if p.suffix=='.gz' else p.read_bytes())


def summarize(trial,profile):
    start,end=trial['measurement_start_ms'],trial['measurement_end_ms']
    rows=[r for r in profile['observations'] if start<=r['at_ms']<end]
    assert rows and not profile['monitor_errors']
    waits=Counter()
    for row in rows:
        for w in row['pg_waits']:
            if w['backend_type']=='client backend' and w['state']=='active':
                waits[(w['wait_type'] or 'running')+'/'+(w['wait'] or 'running')]+=w['count']
    total=sum(waits.values())
    storage=[r for r in rows if 'storage_metrics' in r];assert len(storage)>=2
    a,b=storage[0],storage[-1];seconds=(b['at_ms']-a['at_ms'])/1000
    first,last=(r['storage_metrics']['safekeeper'] for r in (a,b))
    def delta(k):
        value=last[k]-first[k];assert value>=0,k;return value
    calls=delta('safekeeper_flush_wal_seconds_count');duration=delta('safekeeper_flush_wal_seconds_sum');assert calls>0
    pg={k:b['pg_wal'][k]-a['pg_wal'][k] for k in a['pg_wal']};assert min(pg.values())>=0
    return dict(source_commit_ms=trial['source']['source_sql_ms']['commit'],
        active_wait_share_percent={k:100*v/total for k,v in sorted(waits.items())},active_wait_samples=total,
        covered_seconds=seconds,safekeeper_flush_mean_ms=1000*duration/calls,safekeeper_flush_calls_s=calls/seconds,
        pg_wal_delta=pg,pg_wal_timing_available=profile['pg_settings']['track_wal_io_timing']=='on',
        scope='Sampled active waits and async flush wall time include scheduling; percentages are not exact transaction time. Disabled PG WAL timing is unavailable, not zero cost.')


def analyze(folder):
    for line in (folder/'SHA256SUMS').read_text().splitlines():
        digest,name=line.split(maxsplit=1);path=(folder/name).resolve()
        assert path.is_relative_to(folder.resolve()) and hashlib.sha256(path.read_bytes()).hexdigest()==digest,name
    record=read(folder/'experiment.json');assert record['state']=='complete'
    results=[]
    for attempt in record['attempts']:
        for arm,receipt in attempt['results'].items():
            root=folder/receipt['directory'];trial_root=next(root.glob('*-cpu*-rate*-r*'))
            if not (trial_root/'profile.json.gz').exists():continue
            trial=read(trial_root/'trial.json');profile=read(trial_root/'profile.json.gz')
            results.append(dict(directory=receipt['directory'],accepted=attempt['accepted'],cpus=attempt['pair']['cpus'],
                repeat=attempt['pair']['repeat'],arm=arm,source_rows_s=receipt['metrics']['source_rows_s'],
                capture_commit_mean_ms=receipt['metrics']['capture_commit_ms'],**summarize(trial,profile)))
    return dict(source_manifest_sha256=hashlib.sha256((folder/'SHA256SUMS').read_bytes()).hexdigest(),trials=results)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('archives',nargs='+',type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();a.output.write_text(json.dumps({f.name:analyze(f) for f in a.archives},indent=2)+'\n')
