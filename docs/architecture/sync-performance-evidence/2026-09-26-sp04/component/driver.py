#!/usr/bin/env python3
"""Matched SP04 read-only plans; fixtures built before quiet admission."""
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')
def inventory(root):return {str(p.relative_to(root)):sha(p) for p in sorted(root.rglob('*')) if p.is_file()}


def prepare(output,repo,package):
    sys.path[:0]=[str(package/'python/analytics'),str(repo/'python/analytics')]
    import pyarrow as pa
    from deltalake import write_deltalake
    from test_incremental import PROFILE,tx,change
    output.mkdir(mode=0o700,parents=True);(output/'sync-profile').mkdir();(output/'sync-profile/enabled').touch()
    fixtures={}
    for age in ('fresh','aged'):
        root=output/age;root.mkdir(mode=0o700);tables=[]
        schema=pa.schema([pa.field('id',pa.int32(),nullable=False),pa.field('amount',pa.decimal128(38,8)),pa.field('note',pa.string())])
        for oid in (42,43):
            size=10000 if age=='fresh' else 100
            for start in range(0,10000,size):
                table=pa.Table.from_pylist([dict(id=i,amount=None,note='original') for i in range(start,start+size)],schema=schema)
                write_deltalake(str(root/'tables'/str(oid)),table,mode='append',configuration={'delta.dataSkippingNumIndexedCols':'0'})
            tables.append(dict(oid=oid,path='tables/'+str(oid),version=10000//size-1))
        fixtures[age]=dict(root=str(root),tables=tables,files=inventory(root))
    save(output/'fixtures.json',fixtures)
    for age in fixtures:
        for keys in (1,1024):
            payload=tx(280,300,*[change(b'U',oid,new=[i,None,'updated']) for oid in (42,43) for i in range(keys)])
            (output/f'{age}-{keys}.payload').write_bytes(payload)


def worker(package,repo,control,result):
    sys.path[:0]=[str(package/'python/analytics'),str(repo/'python/analytics')]
    from test_incremental import PROFILE
    import incremental_worker as worker
    import worker_profile as profiler
    fixture=json.loads((control.parent/'fixtures.json').read_text());cfg=json.loads(control.read_text());fixture=fixture[cfg['age']]
    root=Path(fixture['root']);before=inventory(root);assert before==fixture['files']
    sys.argv=['incremental_worker.py',str(control)]
    profiler.install(vars(worker),'incremental')
    config=dict(id='matched-plan',identity=dict(generation='matched-generation',decoder_version=1),after_lsn='0/C8',target_lsn='0/12C',deadline_ms=time.time()*1000+60000)
    previous=dict(epoch_id='matched-epoch',manifest=dict(tables=fixture['tables']))
    payload=(control.parent/f"{cfg['age']}-{cfg['keys']}.payload").read_bytes();journal=(PROFILE,[(300,payload)],300,len(payload))
    started=time.perf_counter();cpu=time.process_time()
    with profiler.Span('apply.component'):
        if cfg['arm']=='candidate':
            from incremental.planning import mutation_lease
            with mutation_lease(root) as lease:plan=worker.plan(config,root,previous,journal,lease)
        else:plan=worker.plan(config,root,previous,journal)
    elapsed=time.perf_counter()-started;cpu=time.process_time()-cpu
    profiler.STOP.set();profiler.flush(final=True);profiler.ENABLED=False
    assert inventory(root)==before
    assert len(plan['tables'])==2 and all(len(t['rows'])==cfg['keys'] for t in plan['tables'])
    for table in plan['tables']:
        assert all(row==[i,[i,None,'updated']] for i,row in enumerate(table['rows']))
    checks=profiler.METRICS.get('apply.planning_check' if cfg['arm']=='candidate' else 'apply.boundary',{}).get('calls',0)
    save(result,dict(status='passed',**cfg,files=len(before),plan_sha256=hashlib.sha256(worker.canonical(plan)).hexdigest(),
        fixture_sha256=hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest(),scan_batches=checks-(2 if cfg['arm']=='candidate' else 0),
        wall_seconds=elapsed,cpu_seconds=cpu,metrics=profiler.METRICS,work=profiler.WORK))


def run(output,repo,predecessor,candidate):
    from host_monitor import HostMonitor
    from matrix import topology,affinity
    packages=dict(predecessor=predecessor,candidate=candidate)
    pairs=[dict(age=age,keys=keys,repeat=repeat) for age in ('fresh','aged') for keys in (1,1024) for repeat in range(1,4)]
    rng=random.Random(20260926);rng.shuffle(pairs)
    receipts=[];monitor=HostMonitor(output).start()
    cpus=','.join(map(str,affinity(topology(),8)))
    try:
        for index,pair in enumerate(pairs,1):
            for attempt in range(1,4):
                quiet=monitor.wait_quiet();order=['predecessor','candidate'];rng.shuffle(order)
                receipt=dict(pair=pair,index=index,attempt=attempt,quiet=quiet,arms={});started=time.time()*1000
                for arm in order:
                    label=f'{index:02}-attempt{attempt}-{arm}';control=output/(label+'-control.json');result=output/(label+'.json')
                    save(control,dict(pair,arm=arm));package=packages[arm]
                    process=subprocess.run(['taskset','-c',cpus,str(package/'python/analytics/python'),str(Path(__file__).resolve()),'worker',str(package),str(repo),str(control),str(result)],capture_output=True,text=True,timeout=120)
                    receipt['arms'][arm]=dict(exit_code=process.returncode,result=result.name,stderr=process.stderr[-4000:])
                    assert process.returncode==0,receipt['arms'][arm]
                time.sleep(5.2);ended=time.time()*1000
                a,b=[json.loads((output/receipt['arms'][arm]['result']).read_text()) for arm in ('predecessor','candidate')]
                assert a['plan_sha256']==b['plan_sha256'] and a['fixture_sha256']==b['fixture_sha256'] and a['scan_batches']==b['scan_batches']
                receipt.update(started_at_ms=started,ended_at_ms=ended,overlap=monitor.overlap(started,ended))
                receipt['accepted']=not receipt['overlap'];receipts.append(receipt);save(output/'screen.json',dict(pairs=receipts,cpus=cpus))
                print(json.dumps(dict(index=index,attempt=attempt,accepted=receipt['accepted'],age=pair['age'],keys=pair['keys'])),flush=True)
                if receipt['accepted']:break
            else:raise RuntimeError('three contended pairs')
    finally:monitor.close()

if __name__=='__main__':
    os.umask(0o077)
    mode=sys.argv[1];args=list(map(lambda s:Path(s).resolve(),sys.argv[2:]))
    dict(prepare=prepare,worker=worker,run=run)[mode](*args)
