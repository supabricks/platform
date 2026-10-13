#!/usr/bin/env python3
"""PG247c: executor histogram, interval quantile bounds and instrumentation cost."""
import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import tempfile
import time
import psycopg
from metrics import Metrics
from cell import wait

# Independent interpretation of the shipped collector's strict upper bounds.
UPPER_MS = [0.002,0.003,0.006,0.01,0.02,0.03,0.06,0.1,0.2,0.3,0.6,1,
            2,3,6,10,20,30,60,100,200,300,600,1000,2000,3000,6000,10000,
            20000,30000,60000,100000,None]

class Latency(Metrics):
    def backend_buckets(self, admin, pid):
        rows = admin.execute('''SELECT bucket_le,value FROM neon.get_backend_perf_counters()
            AS p(procno int,pid int,metric text,bucket_le float8,value float8)
            WHERE pid=%s AND metric='query_time_seconds_bucket' ORDER BY bucket_le''',(pid,)).fetchall()
        assert len(rows)==33,rows
        return [int(value) for _,value in rows]

    def cost(self, db):
        results=[]
        for name,query,iterations in [('trivial','SELECT 1',2000),
                ('executor_loop','SELECT sum(i) FROM generate_series(1,10000) i',100)]:
            trials=[]
            # Two ABBA blocks balance drift; fixed warm-up outside each timing.
            for mode in ['off','on','on','off']*2:
                db.execute('SET neon.monitor_query_exec_time='+mode)
                for _ in range(20):db.execute(query,prepare=True).fetchone()
                start=time.perf_counter()
                for _ in range(iterations):db.execute(query,prepare=True).fetchone()
                elapsed=time.perf_counter()-start
                trials.append(dict(enabled=mode=='on',iterations=iterations,seconds=elapsed,
                                   microseconds_per_call=elapsed*1e6/iterations))
            medians={mode:statistics.median(t['microseconds_per_call'] for t in trials if t['enabled']==enabled)
                     for mode,enabled in [('off',False),('on',True)]}
            results.append(dict(workload=name,trials=trials,median_microseconds_per_call=medians,
                median_overhead_percent=100*(medians['on']/medians['off']-1)))
        return dict(method='same persistent local TCP application session; prepared statements; two ABBA blocks; client wall time includes driver and protocol; observational, no pass threshold',workloads=results)

    def run(self):
        self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version=1\nid="{self.project}"\nname="latency"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        branch=self.create('main')
        config=self.request(method='connection',project_id=self.project,id=branch['branch']['id'])
        with psycopg.connect(host='127.0.0.1',port=branch['ports']['sql'],user='cloud_admin',
                password=self.credentials(branch),dbname='postgres',autocommit=True) as admin, \
                psycopg.connect(host=config['host'],port=config['port'],user=config['username'],
                password=config['password'],dbname=config['database'],autocommit=True) as app:
            admin.execute('SET neon.monitor_query_exec_time=off')
            assert app.execute('SHOW neon.monitor_query_exec_time').fetchone()[0]=='on'
            def baseline():
                p=self.last(branch)
                if p.get('status')=='available':
                    assert p['latency']['status'] in ('no_baseline','gap_or_restart','available','no_observations'),p
                    return p
            first=wait(baseline,timeout=50)
            before=self.backend_buckets(admin,app.info.backend_pid)
            for _ in range(40):app.execute('SELECT pg_sleep(0.005)').fetchone()
            for _ in range(10):app.execute('SELECT pg_sleep(0.05)').fetchone()
            after=self.backend_buckets(admin,app.info.backend_pid)
            assert after[-1]-before[-1]==50,(before,after)
            # No sub-3ms completions; slow cohort cannot appear below 30ms.
            assert after[13]-before[13]==0 and after[17]-before[17]<=40,(before,after)
            app.execute('SET neon.monitor_query_exec_time=off')
            for _ in range(20):app.execute('SELECT 1').fetchone()
            assert self.backend_buckets(admin,app.info.backend_pid)==after
            def measured():
                p=self.last(branch)
                return p if p.get('observed_at_ms',0)>first['observed_at_ms'] and p['latency']['status']=='available' else None
            second=wait(measured,timeout=50)
            assert second['latency']['sample_count']>=50,second
            # Recompute nearest ranks independently from the exact persisted pair.
            with sqlite3.connect(f'file:{self.root}/metrics.sqlite3?mode=ro',uri=True) as db:
                records=[json.loads(r[0]) for r in db.execute('SELECT sample FROM samples ORDER BY at_ms')]
            pair=[r for r in records if r['at_ms'] in (first['observed_at_ms'],second['observed_at_ms'])]
            assert len(pair)==2,pair
            old,new=[r['raw']['latency']['cumulative'] for r in pair]
            delta=[b-a for a,b in zip(old,new)]
            assert delta[-1]==second['latency']['sample_count']
            for percentile in (50,95,99):
                rank=math.ceil(delta[-1]*percentile/100)
                index=next(i for i,n in enumerate(delta) if n>=rank)
                assert second['latency'][f'p{percentile}_ms']==[UPPER_MS[index-1] if index else 0,UPPER_MS[index]]
            report=self.history(branch)
            assert report['latency_histogram']['per_statement_percentiles'] is False
            assert all(p['latency']['p95_ms'] is None for p in report['points'] if p['status']=='missing')
            cost=self.cost(app)
            print(json.dumps(cost),flush=True)
            # A role-level inherited opt-out is explicit, not fabricated quiet data.
            admin.execute('ALTER ROLE cloud_admin SET neon.monitor_query_exec_time=off')
            try:
                disabled=wait(lambda:(p if (p:=self.last(branch)).get('latency',{}).get('status')=='histogram_disabled_for_default_session' else None),timeout=50)
                assert disabled['status']=='available' and disabled['latency']['p95_ms'] is None,disabled
            finally:admin.execute('ALTER ROLE cloud_admin RESET neon.monitor_query_exec_time')
        self.stop();self.start()
        points=self.history(branch)['points']
        assert any(p['observed_at_ms']==second['observed_at_ms'] and p['latency']==second['latency'] for p in points)
        return dict(status='PASS',checks=['instrumentation on by default','known backend completion cohort in expected buckets',
            'session opt-out does not increment histogram','API nearest-rank bounds match independently computed persisted deltas',
            'missing intervals have null quantiles','compute-wide scope explicitly excludes per-statement quantiles',
            'disabled inherited instrumentation preserves SQL gauges and reports null quantiles','history survives daemon restart',
            'paired instrumentation overhead trials recorded'],first_sample=first,workload_sample=second,
            disabled_sample=disabled,backend_cohort_cumulative_delta=[b-a for a,b in zip(before,after)],instrumentation_cost=cost)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('binary','bundle','helpers','report'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    root=Path(tempfile.mkdtemp(prefix='pg247c-',dir='/tmp')).resolve()
    cell=Latency(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    try:
        result=cell.run();args.report.write_text(json.dumps(result,indent=2)+'\n');print('PG247c PASS')
    except BaseException:print('Failed fixture retained at',root,flush=True);raise
    finally:
        if (root/'control.sock').exists():cell.stop()
if __name__=='__main__':main()
