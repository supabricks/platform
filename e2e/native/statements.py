#!/usr/bin/env python3
"""PG248b focused statement counters, ranking, scope and reset qualification."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import psycopg
from activity import Activity

class Statements(Activity):
    def stats(self,branch,**options):
        job=self.finish(self.api(dict(action='statements',target=self.target(branch),**options)))
        assert job['state']=='succeeded',job
        return job['result']
    def extension(self,branch,enabled):
        return self.request(method='api',api_version=1,binding=dict(project_id=self.project,worktree=str(self.work)),action=dict(action='extensions',command=dict(action='set',target=self.target(branch),name='pg_stat_statements',enabled=enabled)))
    def run(self):
        self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version=1\nid="{self.project}"\nname="statements"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        branch=self.create('main')
        missing=self.stats(branch)
        assert missing['available'] is False and missing['reason']=='extension_not_enabled',missing
        assert self.sql(branch,"SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'")=='0'
        if self.allow_legacy_engine:
            manifest=json.loads((self.bundle/'manifest.json').read_text())
            if manifest['neon_commit']=='1c6fa095261112aae239beef5a221b484703d49a':
                assert not (self.bundle/'pg_install/v17/share/postgresql/extension/pg_stat_statements.control').exists()
                return dict(status='PASS',profile='legacy-unavailable-only',checks=['legacy engine reports extension unavailable','no automatic extension DDL'],full_statement_qualification=False)
        # Existing user-visible names do not authorize privileged extension calls.
        self.sql(branch,'CREATE EXTENSION pg_stat_statements SCHEMA public')
        assert self.stats(branch)['reason']=='extension_outside_managed_schema'
        self.sql(branch,'DROP EXTENSION pg_stat_statements')
        self.extension(branch,True)
        self.refused(lambda:self.api(dict(action='statements',target=self.target(branch),limit=0)),'between 1 and 100')
        config=self.request(method='connection',project_id=self.project,id=branch['branch']['id'])
        with psycopg.connect(host=config['host'],port=config['port'],user=config['username'],password=config['password'],dbname=config['database'],autocommit=True) as app:
            self.sql(branch,'SELECT supabricks_extensions.pg_stat_statements_reset()')
            for _ in range(4):app.execute('SELECT pg_sleep(0.01)').fetchone()
            for _ in range(2):app.execute('SELECT sum(i) FROM generate_series(1,5) AS s(i)').fetchone()
            app.execute('SELECT i FROM generate_series(1,3) AS s(i)').fetchall()
            # Same query under the private role and another DB must not enter shares.
            self.sql(branch,'SELECT pg_sleep(0.01)')
            self.sql(branch,'CREATE DATABASE activity_other OWNER supabricks_owner')
            with psycopg.connect(host='127.0.0.1',port=branch['ports']['sql'],user=config['username'],password=config['password'],dbname='activity_other',autocommit=True) as other:
                other.execute('SELECT pg_sleep(0.01)').fetchone()
            hidden=self.stats(branch)
            assert hidden['available'] and hidden['eligible_statement_count']==3,hidden
            assert all(r['query_text'] is None for r in hidden['statements'])
            assert hidden['p95_available'] is False and all(r['p95_exec_time_ms'] is None for r in hidden['statements'])
            full=self.stats(branch,include_query_text=True,order_by='calls')
            rows=full['statements'];assert [r['calls'] for r in rows]==[4,2,1],rows
            assert [r['rows'] for r in rows]==[4,2,3],rows
            assert 'pg_sleep' in rows[0]['query_text'] and '$1' in rows[0]['query_text']
            assert rows[0]['mean_exec_time_ms']>0 and rows[0]['total_exec_time_ms']>0
            assert abs(sum(r['share_of_total_exec_time_pct'] for r in rows)-100)<0.000001
            one=self.stats(branch,limit=1,order_by='total_time')
            assert len(one['statements'])==1 and one['truncated'] and one['eligible_statement_count']==3
            assert one['total_exec_time_ms']==full['total_exec_time_ms']
            assert one['statements'][0]['share_of_total_exec_time_pct']<100
            by_rows=self.stats(branch,order_by='rows');assert by_rows['statements'][0]['rows']==4
            by_mean=self.stats(branch,order_by='mean_time');assert by_mean['statements'][0]['query_id']==rows[0]['query_id']
            result=subprocess.run([str(self.binary),'activity','statements','--branch','main','--limit','1','--order-by','calls','--project',str(self.work),'--data-dir',str(self.root)],capture_output=True,text=True,timeout=15,env=self.env)
            assert result.returncode==0,result.stderr
            assert json.loads(result.stdout)['result']['statements'][0]['calls']==4
            # Targeted reset is visible through per-entry stats_since even when
            # the global epoch remains unchanged.
            first=rows[0]
            self.sql(branch,f"SELECT supabricks_extensions.pg_stat_statements_reset({int(first['user_oid'])}::oid,{int(first['database_oid'])}::oid,{int(first['query_id'])}::bigint)")
            app.execute('SELECT pg_sleep(0.01)').fetchone()
            reset=self.stats(branch,include_query_text=True)
            entry=next(r for r in reset['statements'] if r['query_id']==first['query_id'])
            assert entry['calls']==1 and entry['stats_since']!=first['stats_since'],entry
            self.sql(branch,'SELECT supabricks_extensions.pg_stat_statements_reset()')
            empty=self.stats(branch)
            assert empty['available'] and empty['eligible_statement_count']==0 and empty['statements']==[] and empty['total_exec_time_ms']==0,empty
            assert empty['epoch']['stats_reset']!=full['epoch']['stats_reset']
        self.extension(branch,False)
        assert self.stats(branch)['reason']=='extension_not_enabled'
        return dict(status='PASS',checks=['disabled extension explicit; no automatic DDL','unmanaged extension refused','limit validation','known calls and returned-row counts','representative text opt-in; default hidden','internal roles and other databases excluded','top-N share uses full eligible denominator','all four sorting modes','CLI statements command','targeted reset exposes new per-entry start','global reset exposes epoch and empty counters','p95 explicitly unavailable','extension disable respected'],query_text_retained=False)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('binary','bundle','helpers','report'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument("--allow-legacy-engine",action="store_true",help="Only the exact previous engine pin may run absence checks instead of full statement checks")
    args=parser.parse_args();os.umask(0o077)
    root=Path(tempfile.mkdtemp(prefix='pg248b-',dir='/tmp')).resolve()
    cell=Statements(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    cell.allow_legacy_engine=args.allow_legacy_engine
    try:
        result=cell.run();args.report.write_text(json.dumps(result,indent=2)+'\n');print('PG248b PASS')
    except BaseException:print('Failed fixture retained at',root,flush=True);raise
    finally:
        if (root/'control.sock').exists():cell.stop()
if __name__=='__main__':main()
