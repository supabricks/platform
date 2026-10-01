#!/usr/bin/env python3
"""Validate completed controls and quantify observer cost without dropping outliers."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import statistics
import sys

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--repo',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();root=Path(__file__).resolve().parent;folder=root/'controls'
sys.path.insert(0,str(a.repo.resolve()/'e2e/native/performance'))
from compare import expected
from comparison import load_trial
for line in (folder/'SHA256SUMS').read_text().splitlines():
    digest,name=line.split(maxsplit=1);path=(folder/name).resolve()
    assert path.is_relative_to(folder.resolve()) and hashlib.sha256(path.read_bytes()).hexdigest()==digest,name
record=json.loads((folder/'experiment.json').read_text())
assert record['state']=='complete' and record['config']['activation_control']
assert len(record['attempts'])==len(record['pairs'])==6
samples=[]
for path in sorted((folder/'host').glob('*.jsonl.gz')):
    samples.extend(json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines())
rows=[];paired=[]
for pair in record['pairs']:
    outcomes={}
    for arm,receipt in pair['results'].items():
        expect=expected(record['config'],arm,pair['pair'])
        assert expect['parameters']['profile']==(arm=='candidate')
        metrics=load_trial(folder/receipt['directory'],expect)
        assert metrics==receipt['metrics']
        assert metrics['status']=='measured' and metrics['within_5s_p95'] and metrics['offered_load_met']
        window=[s for s in samples if receipt['started_at_ms']<=s['at_ms']<=receipt['ended_at_ms']]
        assert window and not any(s['active_builds'] for s in window)
        trial=json.loads(next((folder/receipt['directory']).glob('*-cpu*/trial.json')).read_text())
        rows.append(dict(cpus=pair['pair']['cpus'],repeat=pair['pair']['repeat'],arm=arm,
            directory=receipt['directory'],source_rows_s=metrics['source_rows_s'],lag_p95_ms=metrics['lag_p95_ms'],
            cpu_cores=metrics['cpu_cores'],peak_memory_bytes=metrics['peak_memory_bytes'],
            peak_backlog_bytes=trial['peak_backlog_bytes'],drain_seconds=trial['drain_seconds'],
            memory_current_before=int(trial['cgroup_before']['memory.current']),
            memory_current_after=int(trial['cgroup_after']['memory.current']),
            minimum_free_gib=min(s['free_bytes'] for s in window)/2**30))
        outcomes[arm]=metrics
    paired.append(dict(cpus=pair['pair']['cpus'],repeat=pair['pair']['repeat'],percent={
        k:100*(outcomes['candidate'][k]/outcomes['predecessor'][k]-1)
        for k in ('source_rows_s','lag_p95_ms','cpu_cores','peak_memory_bytes')}))
summary=[]
for cpu in (8,16):
    group=[r for r in rows if r['cpus']==cpu]
    summary.append(dict(cpus=cpu,trials=len(group),source_rows_s_range=[min(r['source_rows_s'] for r in group),max(r['source_rows_s'] for r in group)],
        lag_p95_ms_range=[min(r['lag_p95_ms'] for r in group),max(r['lag_p95_ms'] for r in group)],
        paired_percent_medians={k:statistics.median(r['percent'][k] for r in paired if r['cpus']==cpu) for k in paired[0]['percent']}))
result=dict(measurements='complete',accepted_trials_this_phase=12,accepted_trials_all_phases=102,
    decision='Source capacity qualified; measurement-enabling slice complete with bounded full-stack evidence and unresolved intermittent 8-core input limit.',
    runtime_change=False,blanket_8core_throughput_qualification=False,summary=summary,pairs=paired,trials=rows,
    reviewed_findings=[
        'Small repeatable throughput cost in both CPU groups remains disclosed; no capacity or freshness threshold crossings. Profiler activation is the intervention; individual probe costs are not isolated.',
        'CPU cost is about 0.08 average cores (5.1-6.3% paired median); memory paired median cost 5.3-6.6%, below the 10% resource investigation trigger.',
        '16-core profiler-off repeat 1 has a 4.71-GB peak and starts load at 4.10 GB. Other off runs peak around 1.9-2.0 GB. Preserve this whole-fixture peak, including setup/warmup; its cause is not isolated and it is not evidence that profiling reduces memory.',
        'All fresh controls exceed 1000 actual changed rows/s and pass exact final correctness, cleanup and five-second p95. They do not replace the earlier accepted 737/839-row/s pair.',
        'No source runtime fix is proven necessary by these controls; scope further durable-commit/storage attribution under issue127 before selecting an SP07 intervention.',
        'Five-minute trials do not qualify sustained maintenance/recovery (SP11), EC2 scaling or release promises (SP12).'])
a.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(summary,indent=2))
