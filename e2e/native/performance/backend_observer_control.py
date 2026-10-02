#!/usr/bin/env python3
"""Full-stack observer on/off throughput control with independent final equality.

Both arms use this same workload. No latency claim is possible without the
transaction observer; final equality proves convergence, not steady freshness.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from trial import InstalledContinuous, Observer, counters, cpu_delta, healthy, load, sha


def run(args):
    root=Path(tempfile.mkdtemp(prefix='sb-observer-control-',dir=args.scratch));root.chmod(0o700)
    release=args.release.resolve();cell=InstalledContinuous(release,root);observer=None
    report=dict(status='error',observer=args.observer,release_identity=sha(release/'release.json'),
        binary_sha256=sha(release/'bin/supabricks'),affinity=sorted(os.sched_getaffinity(0)),
        cgroup_limits=counters(),parameters=dict(rate=args.rate,seconds=args.seconds,warmup=args.warmup,clients=args.clients,rows=10000),
        scope='Observer cost only; no p95 freshness qualification. Frozen-source equality checked independently in both arms.',checks=[])
    try:
        assert json.loads(subprocess.check_output([str(cell.binary),'installation','verify']))['identity']==report['release_identity']
        cell.setup_source(release/'python/analytics/python',release/'python/analytics/export.py',
            'CREATE TABLE orders(id int PRIMARY KEY,value int); CREATE TABLE payments(id int PRIMARY KEY,value int); INSERT INTO orders SELECT i,0 FROM generate_series(1,10000) i; INSERT INTO payments SELECT i,0 FROM generate_series(1,10000) i')
        _,report['baseline']=load(cell,args.rate,5,args.clients,10000)
        cell.sql(cell.parent,'UPDATE orders SET value=0; UPDATE payments SET value=0')
        p=cell.cli('sync','create','--branch','main','--mode','continuous','--key','policy');cell.policy_id=p['id'];healthy(cell)
        if args.observer=='on':
            observer=Observer(cell,p['capture_id']);observer.thread.start()
        _,report['warmup']=load(cell,args.rate,args.warmup,args.clients,10000);healthy(cell)
        before=counters();start=time.perf_counter();report['measurement_start_ms']=time.time()*1000
        _,report['source']=load(cell,args.rate,args.seconds,args.clients,10000,offset=1000000)
        report['measurement_end_ms']=time.time()*1000;after=counters()
        report['cpu']=cpu_delta(before,after,time.perf_counter()-start)
        report['cgroup_before']=before;report['cgroup_after']=after
        expected={}
        for table in ('orders','payments'):
            with cell.source() as db:expected[table]=[dict(id=i,value=v) for i,v in db.execute(f'SELECT id,value FROM {table} ORDER BY id').fetchall()]
        drain=time.monotonic();deadline=drain+120
        while time.monotonic()<deadline:
            current=cell.current()
            # Read both table versions from the same published epoch.
            if all(sorted(cell.version_rows(current,t),key=lambda r:r['id'])==expected[t] for t in expected):break
            time.sleep(.2)
        else:raise RuntimeError('final equality drain timed out')
        report['drain_seconds']=time.monotonic()-drain
        report['checks'].append('both_published_tables_equal_frozen_postgres_source')
        if observer:
            observer.finish();assert not observer.errors and not observer.runtime_error
        report['offered_load_met']=report['source']['achieved_rows_per_second']>=args.rate*.95
        report['status']='measured'
    finally:
        try:
            try:
                if observer:
                    observer.stop.set();observer.thread.join(timeout=10)
                    assert not observer.thread.is_alive()
            finally:
                if (root/'control.sock').exists():cell.stop()
            report['checks'].append('owned_runtime_stopped')
        except BaseException:
            report['status']='error';raise
        finally:
            assert sha(release/'release.json')==report['release_identity']
            args.report.write_text(json.dumps(report,indent=2)+'\n')
            if report['status']=='measured':shutil.rmtree(root)



if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('release','scratch','report'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--observer',choices=('on','off'),required=True)
    p.add_argument('--rate',type=int,default=1250);p.add_argument('--clients',type=int,default=8)
    p.add_argument('--seconds',type=int,default=300);p.add_argument('--warmup',type=int,default=60)
    run(p.parse_args())
