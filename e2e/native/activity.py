#!/usr/bin/env python3
"""PG248a focused fixture: live activity, blocking and guarded session signals."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import uuid
import psycopg
from cell import Cell, wait

class Activity(Cell):
    def api(self, command, binding=None):
        return self.request(method='api',api_version=1,binding=binding or dict(project_id=self.project,worktree=str(self.work)),action=dict(action='activity',command=command))
    def target(self, branch):
        record=self.request(method='branch',id=branch['branch']['id'])
        return dict(branch=record['branch']['id'],revision=record['revision'])
    def finish(self, job):
        def done():
            value=self.api(dict(action='poll',id=job['id']))
            return value if value['state']!='running' else None
        return wait(done,timeout=10)
    def snapshot(self,branch,text=False):
        job=self.finish(self.api(dict(action='list',target=self.target(branch),include_query_text=text)))
        assert job['state']=='succeeded',job
        return job
    def refused(self,action,reason):
        try:action()
        except RuntimeError as error:assert reason in str(error),str(error)
        else:raise AssertionError('request unexpectedly accepted')
    def signal(self,branch,snapshot,pid,action):
        return self.api(dict(action=action,target=self.target(branch),snapshot=snapshot['id'],pid=pid))
    def run(self):
        self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version=1\nid="{self.project}"\nname="activity"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        branch=self.create('main')
        config=self.request(method='connection',project_id=self.project,id=branch['branch']['id'])
        connect=lambda:psycopg.connect(host=config['host'],port=config['port'],user=config['username'],password=config['password'],dbname=config['database'],autocommit=True,options='-c statement_timeout=15000')
        errors=[]
        def execute(db,sql):
            try:db.execute(sql)
            except psycopg.Error as error:errors.append(error.sqlstate)
        # Two application sessions create a real relation-lock wait.
        with connect() as holder,connect() as blocked:
            holder.execute('CREATE TABLE public.activity_fixture(id int)')
            holder.execute('BEGIN; LOCK TABLE public.activity_fixture IN ACCESS EXCLUSIVE MODE')
            pid=blocked.info.backend_pid
            worker=threading.Thread(target=execute,args=(blocked,'SELECT * FROM public.activity_fixture /* private-fixture-marker */'));worker.start()
            wait(lambda:self.sql(branch,f"SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid={pid}")=='t')
            snapshot=self.snapshot(branch)
            rows=snapshot['result']['sessions'];row=next(r for r in rows if r['pid']==pid)
            assert row['can_cancel'] and row['can_terminate'] and row['query_text'] is None
            assert holder.info.backend_pid in row['blocking_pids']
            assert any(l['pid']==pid and l['relation']=='activity_fixture' for l in snapshot['result']['waiting_locks'])
            raw=self.snapshot(branch,True)
            assert 'private-fixture-marker' in next(r for r in raw['result']['sessions'] if r['pid']==pid)['query_text']
            cancel=self.signal(branch,snapshot,pid,'cancel');receipt=self.finish(cancel)
            assert receipt['state']=='succeeded' and receipt['result']['signal_sent'],receipt
            assert self.signal(branch,snapshot,pid,'cancel')['id']==cancel['id']
            worker.join(timeout=5);assert not worker.is_alive() and '57014' in errors
            assert blocked.execute('SELECT 42').fetchone()[0]==42
            self.refused(lambda:self.signal(branch,snapshot,pid,'terminate'),'already signalled')
            # A different old snapshot cannot cancel the next query on the same PID.
            stale=self.finish(self.signal(branch,raw,pid,'cancel'))
            assert stale['state']=='failed' and 'changed since snapshot' in str(stale),stale
            holder.execute('ROLLBACK')
            current=self.snapshot(branch)
            terminated=self.finish(self.signal(branch,current,pid,'terminate'))
            assert terminated['state']=='succeeded' and terminated['result']['signal_sent'],terminated
            wait(lambda:self.sql(branch,f'SELECT count(*) FROM pg_stat_activity WHERE pid={pid}')=='0')
        # Internal administrator sessions are visible only as protected metadata.
        admin=psycopg.connect(host='127.0.0.1',port=branch['ports']['sql'],user='cloud_admin',password=self.credentials(branch),dbname='postgres',autocommit=True)
        try:
            admin.execute('SELECT 1 /* never-expose-internal */')
            current=self.snapshot(branch,True)
            row=next(r for r in current['result']['sessions'] if r['pid']==admin.info.backend_pid)
            assert not row['can_cancel'] and not row['can_terminate'] and row['query_text'] is None
            self.refused(lambda:self.signal(branch,current,admin.info.backend_pid,'terminate'),'protected')
        finally:admin.close()
        stale_target=self.target(branch);stale_target['revision']-=1
        self.refused(lambda:self.api(dict(action='list',target=stale_target)), 'Branch changed')
        foreign=self.root/'foreign';foreign.mkdir();foreign_id=str(uuid.uuid4())
        (foreign/'supabricks.toml').write_text(f'format_version=1\nid="{foreign_id}"\nname="foreign"\n')
        self.request(method='resolve_binding',source=dict(definition_id=foreign_id,worktree=str(foreign)))
        other=dict(project_id=foreign_id,worktree=str(foreign))
        self.refused(lambda:self.api(dict(action='poll',id=current['id']),other),'not_found')
        self.refused(lambda:self.api(dict(action='list',target=self.target(branch)),other),'not_found')
        # CLI uses the same asynchronous API and does not expose query text by default.
        result=subprocess.run([str(self.binary),'activity','list','--branch','main','--project',str(self.work),'--data-dir',str(self.root)],capture_output=True,text=True,timeout=15,env=self.env)
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['state']=='succeeded'
        branch=self.state(branch,'suspended')
        self.refused(lambda:self.api(dict(action='list',target=self.target(branch))),'running, ready')
        branch=self.state(branch,'running');wait(lambda:self.sql(branch,'SELECT 1')=='1')
        old=self.snapshot(branch)
        self.stop();self.start()
        self.refused(lambda:self.api(dict(action='poll',id=old['id'])),'not_found')
        self.checks=['real blocked session and lock object','text hidden by default and explicit bounded opt-in','cancel keeps connection alive','signal retry returns same receipt','stale query snapshot refused','terminate closes selected application session','internal session protected and text hidden','stale branch revision refused','foreign list and receipt refused','CLI polls correctly','suspended compute not awakened','restart invalidates activity snapshots']
        return dict(status='PASS',checks=self.checks,query_text_retained=False)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('binary','bundle','helpers','report'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    root=Path(tempfile.mkdtemp(prefix='pg248a-',dir='/tmp')).resolve()
    cell=Activity(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    try:
        result=cell.run();args.report.write_text(json.dumps(result,indent=2)+'\n');print('PG248a PASS')
    except BaseException:print('Failed fixture retained at',root,flush=True);raise
    finally:
        if (root/'control.sock').exists():cell.stop()
if __name__=='__main__':main()
