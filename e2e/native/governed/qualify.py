#!/usr/bin/env python3
"""UC09.5: authenticated requests against the pinned native PG17 cell."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'catalog'))
from probe import CatalogCell
from cell import wait
import psycopg


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary',type=Path,required=True)
    p.add_argument('--release',type=Path,required=True)
    p.add_argument('--report',type=Path)
    p.add_argument('--exact-installed',action='store_true')
    args=p.parse_args()
    os.umask(0o077)
    root=Path(tempfile.mkdtemp(prefix='s5-' if args.exact_installed else 'sb-uc095-'))
    print('Private diagnostics: '+str(root),flush=True)
    cellroot=root/'cell';cellroot.mkdir()
    cell=CatalogCell(args.binary.resolve(),args.release.resolve()/'engine',args.release.resolve()/'helpers',cellroot)
    if args.exact_installed:cell.binary=args.binary.resolve()
    cell.work=root/'project';cell.work.mkdir()
    (cell.work/'supabricks.toml').write_text(f'format_version=1\nid="{cell.project}"\nname="governed"\n')
    checks=[]
    measurements={}
    try:
        cell.start()
        binding=cell.request(method='resolve_binding',source=dict(definition_id=cell.project,worktree=str(cell.work)))
        with sqlite3.connect(cellroot/'state.sqlite3') as db:
            deployment=db.execute('SELECT id FROM deployments').fetchone()[0]
        branch=cell.create('main');bid=branch['branch']['id']
        cell.sql(branch,'CREATE TABLE example(id int); INSERT INTO example VALUES (42)')
        def admin(**command):return cell.request(method='authorization_admin',command=command)
        def policy():return admin(action='policy',deployment=deployment)['policy_revision']
        def identity(**command):return cell.request(method='identity_admin',command=command)
        actor=identity(action='service',label='alice')['principal_id']
        token=identity(action='issue_service',principal=actor,scopes=['identity:self','project:control'],ttl_seconds=3600)['token']
        admin(action='set_role',deployment=deployment,subject=dict(kind='principal',id=actor),role='administrator',expected_policy=policy(),key=str(uuid.uuid4()))
        bob=identity(action='service',label='bob')['principal_id']
        bobtoken=identity(action='issue_service',principal=bob,scopes=['identity:self','project:control'],ttl_seconds=3600)['token']
        admin(action='set_role',deployment=deployment,subject=dict(kind='principal',id=bob),role='viewer',expected_policy=policy(),key=str(uuid.uuid4()))
        admin(action='set_data_grant',deployment=deployment,branch=bid,subject=dict(kind='principal',id=bob),capability='read',present=True,expected_policy=policy(),key=str(uuid.uuid4()))
        def request(as_token=token,**command):
            return cell.request(method='authorized',envelope=dict(api_version=1,token=as_token,channel='service',csrf=None,command=dict(action='data',deployment=deployment,command=command)))
        def sql(text,cap='read',target=None):
            return request(action='sql',branch=target or bid,capability=cap,sql=text,expected_policy=policy(),key=str(uuid.uuid4()))
        def deny(action):
            try:action()
            except RuntimeError:return
            raise AssertionError('action unexpectedly allowed')
        def grant(cap,present=True,target=None):
            return admin(action='set_data_grant',deployment=deployment,branch=target or bid,subject=dict(kind='principal',id=actor),capability=cap,present=present,expected_policy=policy(),key=str(uuid.uuid4()))
        deny(lambda:sql('SELECT * FROM example'))
        checks.append('project_admin_does_not_imply_pg_read')
        grant('read')
        result=sql('SELECT * FROM example')
        assert result['result']['rows']==[dict(id=42)],result
        deny(lambda:request(as_token=bobtoken,action='status',operation=result['id']))
        bob_result=request(as_token=bobtoken,action='sql',branch=bid,capability='read',sql='SELECT * FROM example',expected_policy=policy(),key=str(uuid.uuid4()))
        assert bob_result['result']['rows']==[dict(id=42)]
        checks.append('restricted_pg_positive_control_and_actor_private_results')
        for text in ["SELECT pg_read_file('/etc/passwd')","SELECT set_config('role','cloud_admin',false)","SELECT set_config('role','supabricks_owner',false)"]:
            deny(lambda:sql(text))
        deny(lambda:sql('INSERT INTO example VALUES(43)','write'))
        checks.append('read_denies_control_files_owner_and_write')
        grant('write')
        assert sql('INSERT INTO example VALUES(43)','write')['result']['affected']==1
        deny(lambda:sql('CREATE TABLE extra(id int)','ddl'))
        grant('ddl')
        sql('CREATE TABLE extra(id int)','ddl')
        checks.append('separate_pg_write_and_ddl')
        for text,cap in [('COMMIT','write'),('INSERT INTO example VALUES(44); COMMIT','write'),("COPY example TO PROGRAM 'true'",'ddl'),('CREATE ROLE stolen SUPERUSER','ddl')]:
            deny(lambda:sql(text,cap))
        deny(lambda:sql('SELECT 1) q WHERE false UNION SELECT NULL --'))
        assert sql('SELECT count(*) AS n FROM example')['result']['rows']==[dict(n=2)]
        assert cell.sql(branch,"SELECT count(*) FROM pg_roles WHERE rolname LIKE 'sbg_%'")=='0'
        checks.append('single_statement_transaction_and_role_cleanup')
        cell.sql(branch,'ALTER TABLE example ENABLE ROW LEVEL SECURITY')
        deny(lambda:sql('SELECT * FROM example'))
        grant('copy_source')
        deny(lambda:request(action='export',branch=bid,expected_policy=policy(),key=str(uuid.uuid4())))
        cell.sql(branch,'ALTER TABLE example DISABLE ROW LEVEL SECURITY')
        checks.append('rls_source_is_not_whole_branch')
        # A grant changed while a write is executing must roll back the SQL.
        sql('SELECT count(*) AS n FROM example')
        last_success=time.monotonic()
        failures=[]
        def changing_write():
            try:sql('INSERT INTO example SELECT 99 FROM pg_sleep(1.5)','write')
            except RuntimeError:failures.append(True)
        worker=threading.Thread(target=changing_write);worker.start()
        wait(lambda:cell.sql(branch,"SELECT count(*) FROM pg_stat_activity WHERE usename LIKE 'sbg_%' AND state='active'")=='1',10)
        grant('write',False);acknowledged=time.monotonic();worker.join(15)
        wait(lambda:cell.sql(branch,"SELECT count(*) FROM pg_stat_activity WHERE usename LIKE 'sbg_%'")=='0',15)
        measurements['platform_revoke']=dict(last_success_monotonic=last_success,acknowledged_deny_monotonic=acknowledged,closed_monotonic=time.monotonic(),bound_seconds=60)
        assert measurements['platform_revoke']['closed_monotonic']-acknowledged<60
        assert failures==[True] and not worker.is_alive()
        assert cell.sql(branch,'SELECT count(*) FROM example WHERE id=99')=='0'
        grant('write')
        started=time.monotonic();deny(lambda:sql('SELECT pg_sleep(60)'))
        assert time.monotonic()-started<15
        deny(lambda:sql("SELECT set_config('statement_timeout','0',false)"))
        assert cell.sql(branch,"SELECT count(*) FROM pg_stat_activity WHERE usename LIKE 'sbg_%'")=='0'
        checks.append('revocation_rolls_back_write_and_timeouts_close_sessions')

        # Whole-source copy is required even for a selected .sbdata table set.
        selection=dict(version=1,tables=[dict(schema='public',name='example')])
        grant('copy_source',False)
        deny(lambda:request(action='export_data',branch=bid,selection=selection,expected_policy=policy(),key=str(uuid.uuid4())))
        grant('copy_source')
        exported=request(action='export_data',branch=bid,selection=selection,expected_policy=policy(),key=str(uuid.uuid4()))
        archive_hex=exported['result']['archive_hex']
        archive=json.loads(bytes.fromhex(archive_hex))
        assert 'grants' not in archive['content'] and 'principals' not in archive['content']
        destination=cell.create('destination');did=destination['branch']['id']
        def ingest(payload=archive_hex):return request(action='import',branch=did,archive_hex=payload,expected_policy=policy(),key=str(uuid.uuid4()))
        deny(ingest);grant('receive',target=did);deny(ingest);grant('ddl',target=did)
        assert len(ingest()['result']['tables'])==1
        grant('read',target=did)
        assert sql('SELECT count(*) AS n FROM example',target=did)['result']['rows']==[dict(n=2)]
        deny(ingest)
        def encode(value):
            value['content_sha256']=hashlib.sha256(json.dumps(value['content'],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode().hex()
        bad=copy.deepcopy(archive)
        new=copy.deepcopy(bad['content']['tables'][0]);new['table']['name']='must_rollback'
        bad['content']['tables'].insert(0,new)
        deny(lambda:ingest(encode(bad)))
        assert cell.sql(destination,"SELECT to_regclass('public.must_rollback') IS NULL")=='t'
        bad=copy.deepcopy(archive);bad['content']['grants']=[dict(principal=actor,role='administrator')]
        deny(lambda:ingest(encode(bad)))
        checks.append('sbdata_copy_requires_source_and_destination_authority_and_is_atomic')
        race=copy.deepcopy(archive);race['content']['tables'][0]['table']['name']='revoked_import'
        rejected=[]
        def racing_import():
            try:ingest(encode(race))
            except RuntimeError:rejected.append(True)
        with psycopg.connect(host='127.0.0.1',port=destination['ports']['sql'],dbname='postgres',user='cloud_admin',password=cell.credentials(destination)) as held:
            held.execute('LOCK TABLE public.example IN ACCESS SHARE MODE')
            worker=threading.Thread(target=racing_import);worker.start()
            wait(lambda:cell.sql(destination,"SELECT count(*) FROM pg_stat_activity WHERE application_name='supabricks-governed' AND wait_event_type='Lock'")=='1',10)
            grant('receive',False,target=did)
        worker.join(15)
        assert rejected==[True] and not worker.is_alive()
        assert cell.sql(destination,"SELECT to_regclass('public.revoked_import') IS NULL")=='t'
        grant('receive',target=did)
        checks.append('destination_grant_race_rolls_back_import')

        # Clone copies data, but source credentials and data grants are not copied.
        grant('receive');grant('share')
        source_password='qualification-source-only'
        cell.sql(branch,"CREATE ROLE source_login LOGIN PASSWORD '"+source_password+"'; GRANT CONNECT ON DATABASE postgres TO source_login")
        result=request(action='clone',branch=bid,name='clone',expected_policy=policy(),key=str(uuid.uuid4()))['result']
        cell.operation(dict(id=result['clone_id']))
        clone=cell.request(method='branch',id=result['branch_id'])
        assert cell.sql(clone,'SELECT count(*) FROM example')=='2'
        for user,password in [('source_login',source_password),('cloud_admin',cell.credentials(branch))]:
            try:
                with psycopg.connect(host='127.0.0.1',port=clone['ports']['sql'],dbname='postgres',user=user,password=password,connect_timeout=2):pass
            except psycopg.OperationalError as error:assert 'password authentication failed' in str(error)
            else:raise AssertionError('copied credential authenticated')
        deny(lambda:sql('SELECT * FROM example',target=result['branch_id']))
        grant('read',target=result['branch_id'])
        assert sql('SELECT count(*) AS n FROM example',target=result['branch_id'])['result']['rows']==[dict(n=2)]
        checks.append('clone_rotates_source_credentials_and_does_not_inherit_grants')

        # The real frozen exporter and publication keep the source policy fence.
        cell.python=args.release.resolve()/'python/analytics/python'
        if not args.exact_installed:cell.configure(args.release.resolve()/'python/analytics/export.py')
        exported=request(action='export',branch=bid,expected_policy=policy(),key=str(uuid.uuid4()))['result']['export_id']
        record=cell.api('get_export',id=exported)
        cell.terminal(record)
        grant('share',False)
        deny(lambda:request(action='publish',export=exported,expected_policy=policy()))
        deny(lambda:cell.api('publish_export',id=exported))
        assert cell.api('list_snapshots',branch='main',limit=10)['snapshots']==[]
        grant('share')
        exported=request(action='export',branch=bid,expected_policy=policy(),key=str(uuid.uuid4()))['result']['export_id']
        record=cell.terminal(cell.api('get_export',id=exported))
        request(action='publish',export=exported,expected_policy=policy())
        cell.publication(record)
        checks.append('frozen_export_publication_requires_fresh_copy_and_sharing_grants')
        cancelled=request(action='export',branch=bid,expected_policy=policy(),key=str(uuid.uuid4()))['result']['export_id']
        grant('copy_source',False)
        cell.terminal(cell.api('get_export',id=cancelled),expected='failed')
        assert cell.api('current_snapshot',branch='main')['publication']['export_id']==exported
        checks.append('grant_race_cleans_export_without_replacing_active_revision')

        # Kill the writer during an uncommitted write. PG rolls back, and the
        # durable key records interruption instead of replaying the statement.
        crash_key=str(uuid.uuid4())
        def crash_write():
            try:request(action='sql',branch=bid,capability='write',sql='INSERT INTO example SELECT 101 FROM pg_sleep(1.5)',expected_policy=policy(),key=crash_key)
            except Exception:pass
        worker=threading.Thread(target=crash_write);worker.start()
        wait(lambda:cell.sql(branch,"SELECT count(*) FROM pg_stat_activity WHERE usename LIKE 'sbg_%' AND state='active'")=='1',10)
        cell.daemons[-1].kill();cell.daemons[-1].wait(timeout=10);worker.join(10)
        cell.start()
        wait(lambda:cell.sql(branch,'SELECT count(*) FROM example WHERE id=101')=='0',60)
        found=request(action='find',key=crash_key)
        assert found['state']=='interrupted' and found['result'] is None,found
        assert cell.sql(branch,"SELECT count(*) FROM pg_stat_activity WHERE usename LIKE 'sbg_%'")=='0'
        checks.append('writer_crash_rolls_back_and_never_replays_sql')

        # Restore a real stopped PG cell from an earlier allow state after the
        # source principal was revoked. No token, grant or copied password revives.
        before_password=cell.credentials(branch)
        before_storage=hashlib.sha256((cell.root/'storage.pk8').read_bytes()).hexdigest()
        before_s3=cell.config['s3_secret']
        realm=identity(action='status')['realm_id']
        cell.stop()
        backup=root/'governed-backup'
        def backup_command(action,path,data):
            process=subprocess.run([str(cell.binary),'backup',action,str(path),'--data-dir',str(data)],capture_output=True,text=True,timeout=120)
            assert process.returncode==0,process.stderr
            return json.loads(process.stdout)
        backup_command('create',backup,cellroot)
        cell.start()
        identity(action='disable',principal=actor,disabled=True)
        cell.stop()
        restored=root/'restored'
        assert backup_command('restore',backup,restored)['governed_closed'] is True
        cell.root=restored
        cell.start()
        state=identity(action='restore_status')
        assert state['closed'] is True
        deny(lambda:sql('SELECT * FROM example'))
        assert admin(action='policy',deployment=deployment)['data_grants']==[]
        assert cell.credentials(branch)!=before_password
        assert hashlib.sha256((cell.root/'storage.pk8').read_bytes()).hexdigest()!=before_storage
        assert cell.config['s3_secret']!=before_s3
        wait(lambda:cell.sql(branch,'SELECT count(*) FROM example')=='2',60)
        try:
            with psycopg.connect(host='127.0.0.1',port=branch['ports']['sql'],dbname='postgres',user='cloud_admin',password=before_password,connect_timeout=2):pass
        except psycopg.OperationalError as error:assert 'password authentication failed' in str(error)
        else:raise AssertionError('backup control password revived')
        deny(lambda:identity(action='restore_reconcile',restore_id=state['restore_id'],realm_id=str(uuid.uuid4())))
        identity(action='restore_reconcile',restore_id=state['restore_id'],realm_id=realm)
        deny(lambda:sql('SELECT * FROM example'))
        identity(action='disable',principal=actor,disabled=False)
        fresh=identity(action='issue_service',principal=actor,scopes=['identity:self','project:control'],ttl_seconds=300)['token']
        admin(action='set_role',deployment=deployment,subject=dict(kind='principal',id=actor),role='viewer',expected_policy=policy(),key=str(uuid.uuid4()))
        grant('read')
        value=request(as_token=fresh,action='sql',branch=bid,capability='read',sql='SELECT count(*) AS n FROM example',expected_policy=policy(),key=str(uuid.uuid4()))
        assert value['result']['rows']==[dict(n=2)]
        audit=identity(action='audit_export',after=0)
        serialized=json.dumps(audit)
        for secret in (token,bobtoken,fresh,before_password,'SELECT count(*) AS n FROM example'):
            assert secret not in serialized
        assert any(e['event']['action']=='access.denied' for e in audit['events'])
        identity(action='audit_acknowledge',after=0,through=audit['through'],sha256=audit['sha256'])
        checks.append('governed_backup_restore_closes_historical_grants_and_rotates_pg_and_sessions')
        checks.append('bounded_audit_export_contains_correlated_metadata_without_credentials_or_sql')

        # Inject a real SQLite audit append failure into the running installed
        # daemon; the identity mutation must roll back with its audit transaction.
        with sqlite3.connect(cell.root/'state.sqlite3') as db:
            db.execute("CREATE TRIGGER qualification_audit_failure BEFORE INSERT ON identity_audit BEGIN SELECT RAISE(ABORT,'qualification audit failure'); END")
        deny(lambda:identity(action='service',label='must-not-exist'))
        with sqlite3.connect(cell.root/'state.sqlite3') as db:
            assert db.execute("SELECT count(*) FROM identity_principals WHERE label='must-not-exist'").fetchone()[0]==0
            db.execute('DROP TRIGGER qualification_audit_failure')
            remaining=10000-db.execute("SELECT count(*) FROM security_audit").fetchone()[0]
            db.executemany("INSERT INTO security_audit(at_ms,event) VALUES(?,'{}')", ((i,) for i in range(remaining)))
        deny(lambda:request(as_token=fresh,action='sql',branch=bid,capability='read',sql='SELECT 1',expected_policy=policy(),key=str(uuid.uuid4())))
        cell.stop();cell.start()
        deny(lambda:request(as_token=fresh,action='sql',branch=bid,capability='read',sql='SELECT 1',expected_policy=policy(),key=str(uuid.uuid4())))
        exported=0
        while True:
            page=identity(action='audit_export',after=0)
            if not page['events']:break
            exported+=len(page['events'])
            identity(action='audit_acknowledge',after=0,through=page['through'],sha256=page['sha256'])
        assert exported>=10000
        checks.append('audit_append_failure_rolls_back_identity_mutation')
        checks.append('audit_capacity_closes_admission_across_restart_and_remains_exportable')

    finally:
        if cell.daemons and cell.daemons[-1].poll() is None:cell.stop()

    report=dict(status='PASS',checks=checks,measurements=measurements,binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),shared_ingress=False)
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)

if __name__=='__main__':main()
