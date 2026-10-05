#!/usr/bin/env python3
"""Read-only receipt review. Run with qualification venv; see README for inputs.

Uses each campaign's frozen validator, verifies source/evidence digests, then
reconstructs metrics. Does not launch measurements or edit original evidence.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def verify(folder, hashes):
    for name, digest in hashes.items():
        p = (folder / name).resolve()
        require(p.is_relative_to(folder.resolve()), 'escaping evidence path')
        require(sha(p) == digest, 'changed evidence: ' + str(p))


def describe(values):
    return dict(median=statistics.median(values), minimum=min(values),
                maximum=max(values), individual=values)


def clean(value):
    require(value['exit_code'] == value['remaining_descendants'] == value['leaked_descendants'] == 0
            and not value['timed_out'], 'failed cleanup')


def manifest(root):
    if (root/'SHA256SUMS').exists():
        verify(root, {name: digest for digest, name in
                     (line.split('  ', 1) for line in (root/'SHA256SUMS').read_text().splitlines())})


def summarize(rows, grouping):
    cells = []
    for key in sorted({tuple(r[k] for k in grouping) for r in rows}):
        selected = [r for r in rows if tuple(r[k] for k in grouping) == key and r['accepted']]
        if not selected:
            continue
        cell = dict(zip(grouping, key)); cell['arms'] = {}; cell['paired_percent'] = {}
        for arm in sorted({r['arm'] for r in selected}):
            armrows = [r for r in selected if r['arm'] == arm]
            metrics = {k: describe([r['values'][k] for r in armrows])
                       for k in armrows[0]['values']
                       if all(isinstance(r['values'].get(k), (int, float)) for r in armrows)}
            cell['arms'][arm] = dict(trials=len(armrows), metrics=metrics,
                measured=sum(r.get('status') == 'measured' for r in armrows),
                fresh=sum(r.get('fresh') is True for r in armrows),
                input_met=sum(r.get('input_met') is True for r in armrows))
        arms = ('predecessor', 'candidate') if 'candidate' in cell['arms'] else ('off', 'on')
        if all(a in cell['arms'] for a in arms):
            pairs = {}
            for r in selected:
                pairs.setdefault(r['pair_id'], {})[r['arm']] = r['values']
            require(all(set(p) == set(arms) for p in pairs.values()), 'incomplete paired cell')
            for k in cell['arms'][arms[0]]['metrics'].keys() & cell['arms'][arms[1]]['metrics'].keys():
                if all(p[arms[0]][k] > 0 for p in pairs.values()):
                    cell['paired_percent'][k] = describe([100*(p[arms[1]][k]/p[arms[0]][k]-1) for p in pairs.values()])
        cells.append(cell)
    return cells


def phase(root):
    from compare import expected
    from comparison import load_trial
    record = read(root/'experiment.json')
    manifest(root)
    require(record['state'] == 'complete', 'incomplete comparison')
    require(len(record['pairs']) == len(record['config']['order']), 'missing planned pairs')
    require(record['pairs'] == [a for a in record['attempts'] if a['accepted']], 'accepted pair set differs')
    # Check the frozen validator and workload files against the recorded identity.
    for arm in record['config']['arms'].values():
        if (root/'analysis-source.json.gz').exists():
            sources = json.loads(gzip.decompress((root/'analysis-source.json.gz').read_bytes()))
            for name, source in sources.items():
                require(hashlib.sha256(source.encode()).hexdigest() ==
                        arm['harness_identity']['files']['e2e/native/performance/'+name],
                        'archived validator source differs: '+name)
        else:
            verify(Path(arm['harness']), arm['harness_identity']['files'])
    rows = []
    for attempt in record['attempts']:
        for arm, receipt in attempt['results'].items():
            folder = root/receipt['directory']
            verify(folder, receipt['evidence_sha256'])
            metrics = load_trial(folder, expected(record['config'], arm, attempt['pair']))
            require(metrics == receipt['metrics'], 'recomputed metrics differ')
            if attempt['accepted']:
                require(not receipt['overlap_samples'], 'accepted host overlap')
                require(receipt['quiet']['quiet_seconds'] >= record['config']['quiet_seconds'], 'quiet admission missing')
            trial = read(next(folder.glob('*-cpu*/trial.json')))
            values = {k:v for k,v in metrics.items() if isinstance(v,(int,float)) and not isinstance(v,bool)}
            values.update({k+'_p95_ms':v['p95'] for k,v in trial.get('stages_ms',{}).items()})
            rows.append(dict(directory=receipt['directory'], arm=arm, accepted=attempt['accepted'],
                pair_id=[attempt['index'], attempt['attempt']], **attempt['pair'],
                status=metrics['status'], fresh=metrics.get('within_5s_p95'),
                input_met=metrics.get('offered_load_met'), values=values,
                rocksdb_write_batch=metrics.get('rocksdb_write_batch'),
                evidence_sha256=receipt['evidence_sha256']))
    # A tuple is used only for grouping, and serializes to a JSON array.
    for row in rows: row['pair_id'] = tuple(row['pair_id'])
    return dict(experiment_sha256=sha(root/'experiment.json'), accepted_pairs=len(record['pairs']),
                attempts=len(record['attempts']), trials=rows, cells=summarize(rows, ('cpus','rate')))


def fixtures(root, name, count):
    receipts = read(root/name/'receipts.json'); rows = []
    require(sum(bool(r['accepted']) for r in receipts) == count, 'fixture count differs')
    for index,r in enumerate(receipts):
        folder = root/name/r['directory']; verify(folder,r['sha256']); clean(read(folder/'cleanup.json'))
        d = read(folder/'result.json'); require(d['status'] in ('PASS','measured'), 'failed fixture')
        if r['accepted']:
            require(not r['overlaps'] and r['quiet']['quiet_seconds']>=300, 'invalid fixture host admission')
        row = dict(directory=r['directory'], accepted=r['accepted'], arm=r['step']['arm'],
                   cpus=r['step']['cpus'], pair_id=index//2, evidence_sha256=r['sha256'])
        if name == 'backend-component':
            require({c['records'] for c in d['cells']}=={32,512,4096}, 'component sizes differ')
            require('all_bounded_ranges_exact' in d['checks'] and 'target_pinned_across_append' in d['checks'], 'component correctness missing')
            row['mode']=d['mode']; row['values']={}
            for c in d['cells']:
                require(c['requests']==16 and c['payload_bytes']==c['records']*1024, 'component workload differs')
                row['values'][str(c['records'])+'_roundtrip_ms']=c['roundtrip_ms']['median']
                row['values'][str(c['records'])+'_cpu_seconds']=c['total_cpu_seconds']
        elif name == 'lifecycle':
            require(all(c['status']=='PASS' for c in d['checks']), 'lifecycle check failed')
            m=d['metrics']['dispatch_component']
            row['values']=dict(idle_cpu_cores=m['idle_cpu_cores'],status_p95_ms=m['status_latency_ms']['p95'])
        else:
            require(d['offered_load_met'] and 'both_published_tables_equal_frozen_postgres_source' in d['checks'], 'observer correctness/load missing')
            row['arm']=d['observer']
            row['values']=dict(source_rows_s=d['source']['achieved_rows_per_second'],cpu_cores=d['cpu']['average_cpu_cores'],drain_seconds=d['drain_seconds'])
        rows.append(row)
    return dict(receipts_sha256=sha(root/name/'receipts.json'),trials=rows,cells=summarize(rows,('cpus',)))


def sustained(root):
    manifest(root)
    receipt=read(root/'receipt.json');verify(root,receipt['sha256']);clean(read(root/'cleanup.json'))
    require(not receipt['overlaps'] and receipt['quiet']['quiet_seconds']>=300,'invalid soak admission')
    d=read(root/'result.json');p=d['parameters'];source=d['source'];lag=d['stages_ms']['commit_to_publication']
    require(d['status']=='measured' and p['seconds']>=1800 and not p['screen'],'not a sustained result')
    require(set(['both_published_tables_equal_frozen_postgres_source','owned_runtime_stopped'])<=set(d['checks']),'soak correctness missing')
    require(d['observed_transactions']>=source['completed_transactions'],'missing transactions')
    require(abs(source['achieved_rows_per_second']-2*source['completed_transactions']/source['elapsed_seconds'])<.1,'source rate differs')
    require(d['within_5s_p95']==(lag['p95']<=5000) and d['offered_load_met']==(source['achieved_rows_per_second']>=.95*p['rate']),'soak gate differs')
    history=read(root/'history.json');require(history['foreign_keys']=='passed','foreign keys failed')
    require(history['counts']['published']==d['observed_publications'],'publication counts differ')
    require(history['counts']['published']>1024 and
            all(history['counts'][k]<=768 for k in ('incremental_runs','incremental_requests','sync_runs')),
            'history rotation evidence missing')
    reopened=read(root/'reopen.json')
    require(reopened and all(r['captured']>=r['prefix_lsn'] and r['retained_bytes']>=0 for r in reopened),
            'invalid retained-chain reopen receipt')
    samples=[json.loads(line) for line in (root/'resources.jsonl').read_text().splitlines()]
    window=[s for s in samples if d['measurement_start_ms']<=s['at_ms']<=d['measurement_end_ms']]
    require(window and all(a['at_ms']<=b['at_ms'] for a,b in zip(samples,samples[1:])),'invalid resources')
    roles={};previous={};cpu={};writes={};denials=0
    for sample in window:
        rss={}
        for proc in sample['processes']:
            if proc.get('error'): denials+=1;continue
            role=proc['role'];rss[role]=rss.get(role,0)+proc['rss_bytes']
            key=(proc['pid'],proc['created']);old=previous.get(key)
            if old:
                require(proc['cpu_seconds']>=old['cpu_seconds'] and proc['write_bytes']>=old['write_bytes'],'resource counter regressed')
                cpu[role]=cpu.get(role,0)+proc['cpu_seconds']-old['cpu_seconds']
                writes[role]=writes.get(role,0)+proc['write_bytes']-old['write_bytes']
            previous[key]=proc
        for role,value in rss.items():roles[role]=max(roles.get(role,0),value)
    backlog=d['backlog_series'];groups=[s['capture_groups'] for s in backlog if s.get('capture_groups')]
    resources=dict(samples=len(samples),measurement_samples=len(window),inspection_denials=denials,
        largest_sample_gap_ms=max(b['at_ms']-a['at_ms'] for a,b in zip(samples,samples[1:])),
        sampled_peak_spool_bytes=max(s['spool_bytes'] for s in window),
        sampled_peak_spool_allocated_bytes=max(s['spool_allocated_bytes'] for s in window),
        role_peak_rss_bytes=roles,role_observed_cpu_seconds=cpu,role_observed_write_bytes=writes,
        limitation='One-second samples can miss short-lived processes and resource peaks; write_bytes is kernel accounting, not device write amplification.')
    return dict(status='verified',receipt_sha256=sha(root/'receipt.json'),evidence_sha256=receipt['sha256'],
        parameters=p,release_identity=d['release_identity'],binary_sha256=d['binary_sha256'],
        affinity=d['affinity'],memory_max=d['cgroup_limits']['memory.max'],source=source,lag_ms=lag,
        cpu=d['cpu'],peak_memory_bytes=d['peak_memory_bytes'],peak_backlog_bytes=d['peak_backlog_bytes'],
        drain_seconds=d['drain_seconds'],within_5s_p95=d['within_5s_p95'],offered_load_met=d['offered_load_met'],
        publications=d['observed_publications'],history=history,reopen=read(root/'reopen.json'),resources=resources,
        capture_backpressure_events=max(s.get('capture_journal',{}).get('backpressure_events',0) for s in backlog),
        final_capture_groups=groups[-1] if groups else None)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=('campaign','phase','sustained'));p.add_argument('input',type=Path)
    p.add_argument('output',type=Path);p.add_argument('--harness',type=Path)
    p.add_argument('--archive',action='store_true',help='use verified frozen analysis source from structured export')
    a=p.parse_args()
    manifest(a.input)
    temporary=None
    if a.archive and a.mode!='sustained':
        source_root=a.input/'historical-main' if a.mode=='campaign' else a.input
        temporary=tempfile.TemporaryDirectory(prefix='sp10c-review-')
        source_dir=Path(temporary.name)/'e2e/native/performance';source_dir.mkdir(parents=True)
        sources=json.loads(gzip.decompress((source_root/'analysis-source.json.gz').read_bytes()))
        identities=read(source_root/'experiment.json')['config']['arms']
        for name,source in sources.items():
            require(Path(name).name==name and name.endswith('.py'),'unsafe source name')
            for arm in identities.values():
                require(hashlib.sha256(source.encode()).hexdigest()==arm['harness_identity']['files']['e2e/native/performance/'+name], 'source digest differs')
            (source_dir/name).write_text(source)
        a.harness=Path(temporary.name)
    if a.harness:sys.path.insert(0,str(a.harness.resolve()/'e2e/native/performance'))
    if a.mode=='sustained':result=sustained(a.input)
    elif a.mode=='phase':result=phase(a.input)
    else:
        result=dict(phases={},fixtures={})
        for name in ('historical-main','qualified-main','historical-profiler-controls','qualified-profiler-controls'):
            result['phases'][name]=phase(a.input/name);print(name,'verified',flush=True)
        for name,count in [('backend-component',6),('lifecycle',6),('predecessor-observer-controls',12),('candidate-observer-controls',12)]:
            result['fixtures'][name]=fixtures(a.input,name,count);print(name,'verified',flush=True)
    result['review_script_sha256']=sha(Path(__file__))
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(a.output,'verified',flush=True)
    if temporary:temporary.cleanup()

if __name__=='__main__':main()
