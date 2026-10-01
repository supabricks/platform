#!/usr/bin/env python3
"""Recompute all higher-load outcomes, preserving the accepted input shortfall."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import statistics
import sys

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
a=parser.parse_args()
root=Path(__file__).resolve().parent
sys.path.insert(0,str(a.repo.resolve()/'e2e/native/performance'))
from compare import expected
from comparison import load_trial

def verify(folder):
    for line in (folder/'SHA256SUMS').read_text().splitlines():
        digest,name=line.split(maxsplit=1);p=(folder/name).resolve()
        assert p.is_relative_to(folder.resolve())
        assert hashlib.sha256(p.read_bytes()).hexdigest()==digest,name

def host(folder):
    samples=[];partial=[]
    for p in sorted((folder/'host').glob('*.jsonl.gz')):
        for i,line in enumerate(gzip.decompress(p.read_bytes()).splitlines(),1):
            try:samples.append(json.loads(line))
            except ValueError:partial.append(dict(file=p.name,line=i,bytes=len(line)))
    return samples,partial

main=root/'qualified-main';failed=root/'qualified-controls-interrupted'
verify(main);verify(failed)
record=json.loads((main/'experiment.json').read_text())
assert record['state']=='complete' and len(record['pairs'])==6
samples,partial=host(main);assert not partial
rows=[]
for attempt in record['attempts']:
    for arm,receipt in attempt['results'].items():
        folder=main/receipt['directory']
        metrics=load_trial(folder,expected(record['config'],arm,attempt['pair']))
        assert metrics==receipt['metrics']
        trial=json.loads(next(folder.glob('*-cpu*/trial.json')).read_text())
        window=[s for s in samples if receipt['started_at_ms']<=s['at_ms']<=receipt['ended_at_ms']]
        assert window
        rows.append(dict(directory=receipt['directory'],accepted=attempt['accepted'],arm=arm,
            cpus=attempt['pair']['cpus'],repeat=attempt['pair']['repeat'],
            source_rows_s=metrics['source_rows_s'],lag_p95_ms=metrics['lag_p95_ms'],
            input_pass=metrics['offered_load_met'],freshness_pass=metrics['within_5s_p95'],
            status=metrics['status'],baseline_rows_s=metrics['baseline_rows_s'],
            source_commit_ms=trial['source']['source_sql_ms']['commit'],
            capture_commit_mean_ms=metrics['capture_commit_ms'],cpu_cores=metrics['cpu_cores'],
            minimum_free_gib=min(s['free_bytes'] for s in window)/2**30,
            io_some_avg10_median=statistics.median(float(s['pressure']['io'].split()[1].split('=')[1]) for s in window),
            detected_build_samples=sum(bool(s['active_builds']) for s in window)))
accepted=[r for r in rows if r['accepted']]
assert len(rows)==16 and len(accepted)==12
assert all(r['status']=='measured' and r['freshness_pass'] for r in accepted)
assert sum(r['input_pass'] for r in accepted)==10
summary=[]
for cpu in (8,16):
    group=[r for r in accepted if r['cpus']==cpu]
    summary.append(dict(cpus=cpu,trials=len(group),input_passes=sum(r['input_pass'] for r in group),
        source_rows_s_min=min(r['source_rows_s'] for r in group),source_rows_s_max=max(r['source_rows_s'] for r in group),
        lag_p95_ms_min=min(r['lag_p95_ms'] for r in group),lag_p95_ms_max=max(r['lag_p95_ms'] for r in group)))
broken=json.loads((failed/'experiment.json').read_text());hs,partial=host(failed)
assert broken['state']=='running_trial' and broken['pairs']==[]
assert len(partial)==1 and len(hs)==120
result=dict(status='main_complete_controls_pending',runtime_change=False,performance_qualification=False,
    acceptance='All originally accepted trials retained. Two input misses are not reclassified as contended or replaced.',
    summary=summary,trials=rows,
    controls_failure=dict(state=broken['state'],accepted_pairs=0,valid_host_samples=len(hs),
        partial_host_records=partial,first_free_gib=hs[0]['free_bytes']/2**30,
        last_free_gib=hs[-1]['free_bytes']/2**30,original_cleanup_receipt_present=False),
    interpretation='Elevated source and capture COMMIT times coincide with the accepted 8-core shortfall. Low CPU use and a slow pre-sync source baseline point to commit service time as the immediate limit. Host I/O pressure does not identify a causal competing process; later ENOSPC is not proof of the earlier cause.',
    remaining=['New on/off profiler controls after disk recovery','Review activation costs and retained input misses','Final slice disposition; no blanket 8-core target qualification'])
a.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(dict(summary=summary,controls_failure=result['controls_failure']),indent=2))
