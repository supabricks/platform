#!/usr/bin/env python3
"""PG247a: durable metric sampling, counter rates, suspend gaps and daemon restart."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import psycopg
from activity import Activity
from cell import wait

class Metrics(Activity):
    def history(self, branch, range='1h'):
        return self.request(method='api', api_version=1,
            binding=dict(project_id=self.project,worktree=str(self.work)),
            action=dict(action='metrics',command=dict(action='history',target=self.target(branch),range=range)))

    def last(self, branch):
        points=self.history(branch)['points']
        return next((p for p in reversed(points) if p['observed_at_ms'] is not None), {})

    def run(self):
        self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version=1\nid="{self.project}"\nname="metrics"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        branch=self.create('main')
        config=self.request(method='connection',project_id=self.project,id=branch['branch']['id'])
        connection=dict(host=config['host'],port=config['port'],user=config['username'],password=config['password'],dbname=config['database'],autocommit=True)
        with psycopg.connect(**connection) as idle:
            idle.execute('SELECT 1 /* metrics-must-not-store-this-text */').fetchone()
            started=int(time.time()*1000)
            def baseline():
                p=self.last(branch)
                return p if p.get('status')=='available' and p['observed_at_ms']>started else None
            first=wait(baseline,timeout=50)
            assert first['connections_idle']>=1 and first['database_size_bytes']>0,first
            with psycopg.connect(**connection) as writer:
                writer.execute('CREATE TABLE public.metrics_fixture (i int, payload text)')
                for i in range(300):
                    writer.execute('INSERT INTO public.metrics_fixture VALUES (%s, repeat(md5(%s::text), 20))',(i,i))
                for _ in range(10):
                    writer.execute('BEGIN');writer.execute('SELECT count(*) FROM public.metrics_fixture');writer.execute('ROLLBACK')
            def rates():
                p=self.last(branch)
                return p if p.get('observed_at_ms',0)>first['observed_at_ms'] and p.get('rates_status')=='available' else None
            second=wait(rates,timeout=50)
            assert second['commits_per_second']>1 and second['rollbacks_per_second']>0,second
            assert second['wal_bytes_per_second']>0,second
            assert second['cache_hit_ratio'] is None or 0<=second['cache_hit_ratio']<=1,second
        for window,count in [('1h',121),('6h',721),('24h',1441),('7d',1009)]:
            result=self.history(branch,window)
            assert len(result['points'])==count and result['storage_healthy']
            assert all(p['connections_active'] is None for p in result['points'] if p['status']=='missing')
            assert result['unavailable']['query_latency_percentiles']=='histogram_not_collected'
            assert not result['query_text_persisted'] and not result['external_telemetry']
        cli=subprocess.run([str(self.binary),'metrics','history','--branch','main','--range','6h','--project',str(self.work),'--data-dir',str(self.root)],capture_output=True,text=True,env=self.env,timeout=15)
        assert cli.returncode==0,cli.stderr
        assert json.loads(cli.stdout)['range']=='6h'
        branch=self.state(branch,'suspended')
        def suspended():
            p=self.last(branch)
            return p if p.get('status')=='suspended' else None
        gap=wait(suspended,timeout=50)
        assert gap['connections_active'] is None and gap['commits_per_second'] is None
        assert self.request(method='branch',id=branch['branch']['id'])['endpoint']['desired_state']=='suspended'
        # No API read can wake the compute; persisted pre-suspend values survive.
        self.stop();self.start()
        points=self.history(branch)['points']
        assert any(p['observed_at_ms']==first['observed_at_ms'] for p in points),points
        assert self.request(method='branch',id=branch['branch']['id'])['endpoint']['desired_state']=='suspended'
        assert b'metrics-must-not-store-this-text' not in (self.root/'metrics.sqlite3').read_bytes()
        return dict(status='PASS',checks=['real idle connection gauge and database size','positive transaction rollback and WAL rates after workload','cache ratio bounded or unavailable','four bounded history ranges','missing data remains null','CLI history','suspend gaps without wake','history survives daemon restart','no SQL text in persisted history','unsupported percentile and resource metrics explicit'],first_sample=first,workload_sample=second,suspended_sample=gap)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('binary','bundle','helpers','report'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    root=Path(tempfile.mkdtemp(prefix='pg247a-',dir='/tmp')).resolve()
    cell=Metrics(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    try:
        result=cell.run();args.report.write_text(json.dumps(result,indent=2)+'\n');print('PG247a PASS')
    except BaseException:print('Failed fixture retained at',root,flush=True);raise
    finally:
        if (root/'control.sock').exists():cell.stop()
if __name__=='__main__':main()
