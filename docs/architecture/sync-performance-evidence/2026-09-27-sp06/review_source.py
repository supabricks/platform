#!/usr/bin/env python3
"""Reproduce the specific SP06 source/observer review before higher-load trials."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--campaign',type=Path,required=True)
p.add_argument('--repo',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args();root=a.campaign.resolve();repo=a.repo.resolve()
sys.path.insert(0,str(repo/'e2e/native/performance'))
from source_analysis import analyze
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
phases=('source-screen-02','source-controls','source-envelope','historical-main','historical-controls')
identities={}
for phase in phases:
    archived=root/'archives'/phase
    for line in (archived/'SHA256SUMS').read_text().splitlines():
        digest,name=line.split(maxsplit=1);path=(archived/name).resolve()
        assert path.is_relative_to(archived.resolve()) and sha(path)==digest,name
    identities[phase]=dict(raw_experiment_sha256=sha(root/phase/'experiment.json'),archive_manifest_sha256=sha(archived/'SHA256SUMS'))
screen,controls,envelope=(analyze(root/phase) for phase in phases[:3])
assert screen['provisional_selection']==dict(clients=8,qualification_floor_rows_s=1250,preferred_headroom=True)
assert len(screen['trials'])==18 and len(controls['trials'])==12 and len(envelope['trials'])==6
selected=[t for t in screen['trials'] if t['clients']==8]+controls['trials']
minimum=min(min(t['minute_rows_s']) for t in selected)
assert all(t['clients']==8 and t['seconds']==300 and len(t['minute_rows_s'])==5 for t in selected)
assert minimum>=1250
for cpu in (8,16):
    for profile in (False,True):
        ts=[t for t in controls['trials'] if t['cpus']==cpu and t['profile']==profile]
        assert {t['repeat'] for t in ts}=={1,2,3}
for c in controls['controls']:
    # Bounds describe this reviewed evidence; they are not a relaxed new policy.
    v=c['paired_percent']
    assert -1.3<v['rows_s']['minimum']<v['rows_s']['maximum']<0
    assert 0<v['transaction_p95_ms']['median']<1
    assert 0<v['cpu_cores']['median']<6
    assert v['peak_memory_bytes']['median']<4
history=[]
for phase in phases[3:]:
    result=json.loads((root/phase/'comparison.json').read_text())
    assert result['complete'] and result['accepted_pairs']==(12 if phase=='historical-main' else 9)
    for g in result['groups']:
        row=dict(phase=phase,cpus=g['cpus'],offered_rows_s=g['rate'])
        for arm in ('predecessor','candidate'):
            metrics=g[arm]
            assert metrics['failures']==0 and metrics['measured']==metrics['freshness_passes']==metrics['trials']==3
            row[arm]=dict(source_rows_s=metrics['metrics']['source_rows_s']['median'],lag_p95_ms=metrics['metrics']['lag_p95_ms']['median'])
        row['paired_percent_medians']={k:statistics.median(x['percent'] for x in g['paired'][k]) for k in ('source_rows_s','lag_p95_ms','cpu_cores','peak_memory_bytes')}
        history.append(row)
decision=dict(decision='qualified_for_profile_trial',scope='Source input capacity only; no 1000-row/s analytical replication claim.',
    identities=identities,accepted_trials=78,source_clients=8,source_floor_rows_s=1250,
    minimum_required_minute_rows_s=minimum,minimum_margin_above_floor_percent=100*(minimum/1250-1),
    source_screen_groups=screen['groups'],source_control_pairs=controls['controls'],four_cpu_envelope=envelope['groups'],historical_comparisons=history,
    reviewed_flags=['repeatable source profiler throughput loss at both CPU sizes'],
    disposition='Retain and disclose observer cost. Activation costs about 0.5-0.6% median source throughput, 5.2-5.5% CPU and <1% transaction p95. Every required minute in both arms retains at least 33% headroom above the 1250-row/s floor. Selection remains eight clients with profiling on or off. Historical profiler controls also show a small input cost, without correctness or five-second freshness failures at achieved input. Proceed using identical diagnostic activation in both higher-load comparison arms, followed by separately required controls at that load. Do not subtract or normalize measured overhead out of results.',
    limitations=['Three-repeat screen is not statistical certainty or an EC2 scaling claim.','Observer activation is the tested intervention; individual timing/resource probe costs are not separately isolated.','Historical four-client overload runs still supply only about 725-742 rows/s; they do not qualify 1000-row/s replication.','Original nine temporary contended trials remain missing; one recovered dependency-failure trial and three fresh contended trials remain excluded and archived.'],
    next_profile=dict(id='sp06-source-qualified-8clients-v1',clients=8,cpus=[8,16],memory_gib=16,swap=False,cpu_quota=False,rows_per_table=10000,tables=2,changes_per_transaction=2,baseline_seconds=5,warmup_seconds=60,seconds=300,offered_changed_rows_s=1250,comparison_pairs=6,activation_control_pairs=6),
    runtime_revision='e10d5152f5681af7c233701e0fd75c100cbf4c6c',harness_revision='be4701cce5694ed00349ab3db9b577592965d7d7',
    measurement_change=False,runtime_change=False)
a.output.write_text(json.dumps(decision,indent=2)+'\n')
print(json.dumps({k:decision[k] for k in ('decision','accepted_trials','minimum_required_minute_rows_s','minimum_margin_above_floor_percent','next_profile')},indent=2))
