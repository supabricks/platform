#!/usr/bin/env python3
"""SY06 governed modes and immutable epoch views on disposable native workers."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import uuid
from cell import wait
from snapshot_policies import Policies


class Surfaces(Policies):
    def run(self, python, worker):
        self.python = python
        self.work = self.root / 'work'
        self.work.mkdir()
        (self.work / 'supabricks.toml').write_text(
            f'format_version=1\nid="{self.project}"\nname="sync-surfaces"\n')
        self.start()
        self.request(method='resolve_binding', source=dict(definition_id=self.project, worktree=str(self.work)))
        parent = self.create('main'); branch = parent['branch']['id']
        self.configure(worker)
        self.sql(parent, 'CREATE TABLE orders(id int PRIMARY KEY); INSERT INTO orders VALUES(1)')
        with sqlite3.connect(self.root / 'state.sqlite3') as db:
            deployment = db.execute('SELECT id FROM deployments').fetchone()[0]
        identity = lambda **c: self.request(method='identity_admin', command=c)
        admin = lambda **c: self.request(method='authorization_admin', command=c)
        revision = lambda: admin(action='policy', deployment=deployment)['policy_revision']
        manager = identity(action='service', label='sync-manager')['principal_id']
        service = identity(action='service', label='sync-executor')['principal_id']
        other = identity(action='service', label='other-reader')['principal_id']
        token_for = lambda p: identity(action='issue_service', principal=p, scopes=['identity:self', 'project:control'], ttl_seconds=3600)['token']
        token = token_for(manager); other_token = token_for(other)
        for who in (manager, service, other):
            admin(action='set_role', deployment=deployment, subject=dict(kind='principal', id=who), role='viewer', expected_policy=revision(), key=str(uuid.uuid4()))
        def grant(who, cap, present=True):
            admin(action='set_data_grant', deployment=deployment, branch=branch, subject=dict(kind='principal', id=who), capability=cap, present=present, expected_policy=revision(), key=str(uuid.uuid4()))
        def governed(command, actor_token=None, principal=None, target=deployment):
            return self.request(method='authorized', envelope=dict(api_version=1, token=actor_token or token, channel='service', csrf=None,
                command=dict(action='workspace', command=dict(action='sync', deployment=target, request=dict(command=command, expected_policy=revision(), service_principal=principal)))))
        def denied(call):
            try: call()
            except RuntimeError: return
            raise AssertionError('unauthorized action succeeded')
        create = dict(kind='create', branch=branch, key='create', config=dict(mode='snapshot', strategy='full', schedule=None))
        denied(lambda: governed(create, principal=service))
        for cap in ('read', 'manage_sync', 'read_sync'): grant(manager, cap)
        denied(lambda: governed(create, principal=service))
        for cap in ('read', 'execute_sync'): grant(service, cap)
        inspection = governed(dict(kind='inspect', branch=branch))
        assert inspection['source_qualification'] == 'not_probed'
        assert self.sync('list') == []
        p = governed(create, principal=service)
        assert governed(create, principal=service)['id'] == p['id']
        assert governed(dict(kind='list'), actor_token=other_token) == []
        denied(lambda: governed(dict(kind='get', id=p['id']), actor_token=other_token))
        denied(lambda: governed(dict(kind='get', id=p['id']), target=str(uuid.uuid4())))
        self.check('read_only_inspection_separate_source_manage_executor_and_result_grants_no_leakage')
        first = governed(dict(kind='run_now', id=p['id'], expected_revision=p['revision'], key='first'))
        identity(action='revoke', principal=manager)
        denied(lambda: governed(dict(kind='get', id=p['id'])))
        first = self.finished(first)
        reader = self.opened(epoch=first['epoch_id'], ttl_ms=600000)
        assert self.query(reader, 'SELECT count(*) FROM public.orders')['rows'] == [['1']]
        self.check('native_background_snapshot_finishes_after_manager_session_revocation')
        token = token_for(manager)
        self.sql(parent, 'INSERT INTO orders VALUES(2)')
        second = self.finished(governed(dict(kind='run_now', id=p['id'], expected_revision=1, key='second')))
        assert second['epoch_id'] != first['epoch_id']
        assert self.query(reader, 'SELECT count(*) FROM public.orders')['rows'] == [['1']]
        self.close(reader)
        newer = self.opened(epoch=second['epoch_id'], ttl_ms=600000)
        assert self.query(newer, 'SELECT count(*) FROM public.orders')['rows'] == [['2']]
        self.close(newer)
        self.check('new_epochs_do_not_retarget_existing_sail_readers')
        identity(action='revoke', principal=service)
        denied(lambda: governed(dict(kind='run_now', id=p['id'], expected_revision=1, key='second')))
        denied(lambda: self.api('get_snapshot', id=first['epoch_id']))
        denied(lambda: self.opened(epoch=second['epoch_id'], ttl_ms=600000))
        assert governed(dict(kind='get', id=p['id']))['authority_status'] == 'revoked_or_unavailable'
        self.check('service_revocation_denies_replayed_admission_and_retained_epoch_new_readers')
        governed(dict(kind='delete', id=p['id'], expected_revision=governed(dict(kind='get',id=p['id']))['revision'], key='delete'))
        # A new scoped service authority cannot bypass the frozen whole-branch profile.
        self.sql(parent, 'ALTER TABLE orders ENABLE ROW LEVEL SECURITY')
        p = governed(dict(create, key='rls'), principal=service)
        run = governed(dict(kind='run_now', id=p['id'], expected_revision=1, key='rls-run'))
        ended = wait(lambda: (r if (r := self.sync('run', id=run['id']))['state'] in ('failed', 'cancelled', 'succeeded') else False), timeout=180)
        assert ended['state'] != 'succeeded', ended
        governed(dict(kind='delete', id=p['id'], expected_revision=governed(dict(kind='get',id=p['id']))['revision'], key='delete-rls'))
        self.check('native_rls_source_never_publishes_through_service_authority')
        self.sql(parent, 'ALTER TABLE orders DISABLE ROW LEVEL SECURITY')
        self.sql(parent, 'CREATE FUNCTION public.unsupported_sync_function() RETURNS int LANGUAGE sql AS \'SELECT 1\'')
        rejected = governed(dict(create, key='unsupported-capture', config=dict(mode='continuous', strategy='incremental', schedule=None)), principal=service)
        blocked = wait(lambda:(v if (v:=self.api('managed_capture',command=dict(kind='status',id=rejected['capture_id'])))['desired']=='fenced' else False),timeout=60)
        assert blocked['error']=='unsupported_governed_source_profile' and blocked['start_lsn'] is None and blocked['bootstrap_id'] is None,blocked
        assert self.sql(parent,"SELECT count(*) FROM pg_publication WHERE pubname LIKE 'sbcap_%'")=='0'
        governed(dict(kind='delete',id=rejected['id'],expected_revision=governed(dict(kind='get',id=rejected['id']))['revision'],key='delete-unsupported'))
        wait(lambda:self.api('managed_capture',command=dict(kind='status',id=rejected['capture_id']))['state']=='deleted')
        self.sql(parent,'DROP FUNCTION public.unsupported_sync_function()')
        self.check('unsupported_governed_source_is_refused_before_capture_resources_or_bootstrap')
        p = governed(dict(create, key='incremental', config=dict(mode='continuous', strategy='incremental', schedule=None)), principal=service)
        def healthy():
            current = governed(dict(kind='get', id=p['id']))
            assert current['state'] != 'blocked', current
            return current if current.get('continuous_status', {}).get('state') == 'healthy' else False
        current = wait(healthy, timeout=180)
        epoch = current['last_epoch_id']
        capture = self.api('managed_capture', command=dict(kind='status', id=current['capture_id']))
        assert capture['identity']['service_authority']['principal_id'] == service
        reader = self.opened(epoch=epoch, ttl_ms=600000)
        self.sql(parent, 'INSERT INTO orders VALUES(3)')
        identity(action='revoke', principal=manager)
        wait(lambda: (v if (v:=self.sync('get',id=p['id']))['last_epoch_id'] != epoch else False),timeout=120)
        token = token_for(manager)
        current = wait(healthy,timeout=120)
        assert self.query(reader,'SELECT count(*) FROM public.orders')['rows'] == [['2']]
        self.close(reader)
        self.check('governed_continuous_service_identity_survives_manager_logout_and_keeps_old_readers_pinned')
        # Review and sharing use the same result authority for incremental artifacts.
        run = next(r for r in self.sync('runs', id=p['id']) if r['epoch_id']==current['last_epoch_id'])
        result = self.request(method='authorized', envelope=dict(api_version=1,token=token,channel='service',csrf=None,
            command=dict(action='workspace',command=dict(action='snapshot',deployment=deployment,export=run['published_artifact_id']))))
        assert result['publication']['epoch_id'] == current['last_epoch_id']
        self.check('incremental_result_review_uses_the_scoped_service_artifact')
        if self.catalog_runtime:
            self.request(method='catalog_service',command=dict(action='configure',provider=dict(mode='local',runtime=str(self.catalog_runtime))))
            wait(lambda:self.request(method='catalog_service',command=dict(action='status'))['ready'],timeout=90)
            meta=self.api('catalog_metadata',command=dict(action='ensure_namespace'))
            if meta.get('state')=='running':
                meta=wait(lambda:(v if (v:=self.api('catalog_metadata',command=dict(action='poll',id=meta['id'])))['state']!='running' else False),timeout=90)
                assert meta['state']=='complete',meta
            def publication(action,**fields): return self.api('catalog_publication',command=dict(action=action,**fields))
            def share(epoch,key):
                review=publication('preview',epoch_id=epoch)
                assert review['retention']['copies'] is True
                candidate=publication('publish',epoch_id=epoch,key=key,expected_preview=review['preview_hash'],expected_source_revision=review['source_revision'],expected_binding_revision=review['binding_revision'])['publication']
                def ready():
                    v=publication('status',id=candidate['id'])['publication']; assert not v['error'],v
                    return v if v['state']=='published' else False
                return wait(ready,timeout=90)
            shared=share(current['last_epoch_id'],'shared-first')
            catalog_reader=self.opened(catalog=True,ttl_ms=600000)
            assert self.query(catalog_reader,'SELECT count(*) FROM public.orders')['rows']==[['3']]
            from urllib.parse import urlparse,unquote
            location=unquote(urlparse(shared['tables'][0]['body']['storage_location']).path)
            self.sql(parent,'DELETE FROM orders WHERE id=1; INSERT INTO orders VALUES(4),(5)')
            wait(lambda: healthy() and self.sync('get',id=p['id'])['last_epoch_id'] != shared['epoch_id'],timeout=120)
            current=wait(healthy,timeout=120)
            assert self.query(catalog_reader,'SELECT count(*) FROM public.orders')['rows']==[['3']]
            self.close(catalog_reader)
            newer=share(current['last_epoch_id'],'shared-second')
            new_reader=self.opened(catalog=True,ttl_ms=600000)
            assert self.query(new_reader,'SELECT count(*) FROM public.orders')['rows']==[['4']]
            self.close(new_reader)
            proof="from deltalake import DeltaTable;import sys;import pyarrow.fs as f;p=sys.argv[1];assert sorted(DeltaTable(p).to_pyarrow_table(filesystem=f.SubTreeFileSystem(p,f.LocalFileSystem())).column('id').to_pylist())==[1,2,3]"
            subprocess.run([str(self.python),'-c',proof,location],check=True,timeout=30)
            self.check('real_uc_incremental_publications_are_immutable_version_zero_views_with_pinned_sail_readers')
            # Bind the older immutable publication in a different project. Neither
            # new producer heads nor notebook restarts may change that binding.
            consumer = self.root / 'consumer'; consumer.mkdir()
            definition = str(uuid.uuid4())
            (consumer / 'supabricks.toml').write_text(f'''format_version=2
id="{definition}"
name="sync-consumer"
[package]
version="0.1.0"
include=[]
notebook_outputs="strip"
[targets.local]
mode="development"
default=true
[resources.database.main]
kind="postgres_database"
lifecycle="retain"
[resources.dataset.orders]
kind="catalog_dataset"
requirement="orders.v1"
''')
            ctx = self.request(method='project',source=dict(definition_id=definition,worktree=str(consumer)),command=dict(action='create',key='consumer',target=None))
            scope = dict(project_id=ctx['runtime_project_id'],worktree=str(consumer))
            def call(action,**fields): return self.request(method='api',api_version=1,binding=scope,action=dict(action=action,**fields))
            target = dict(deployment_id=shared['deployment_id'],provider_id=shared['namespace']['provider_id'],publication_id=shared['id'])
            plan = call('project_apply',command=dict(action='plan',options=dict(datasets={'dataset.orders':target})))
            op = call('project_apply',command=dict(action='apply',plan=plan,key='bind'))
            op = wait(lambda:(v if (v:=call('project_apply',command=dict(action='status',id=op['id'])))['state'] in ('succeeded','failed') else False),timeout=150)
            assert op['state']=='succeeded',op
            session = call('analytics_open',branch='main',catalog=True,key='bound',ttl_ms=600000)
            session = wait(lambda:(v if (v:=call('analytics_session',id=session['id']))['state'] in ('ready','failed') else False),timeout=150)
            assert session['state']=='ready',session
            q = call('analytics_sql',id=session['id'],sql='SELECT count(*) FROM dataset_orders.public.orders')
            q = wait(lambda:(v if (v:=call('analytics_query',id=session['id'],query=q['id']))['state']!='running' else False),timeout=45)
            assert q['state']=='complete' and q['rows']==[['3']],q
            call('analytics_close',id=session['id'])
            wait(lambda:call('analytics_session',id=session['id'])['state']=='closed')
            sys.path.insert(0,str(Path(__file__).resolve().parent/'notebook-environments'))
            from client import Console,execute
            def cli(project,*parts):
                return json.loads(subprocess.check_output([str(self.binary),*parts,'--project',str(project),'--data-dir',str(self.root)],text=True,timeout=180).strip().splitlines()[-1])
            # Use the verified baseline's offline notebook environment closure.
            # The new view deliberately retains its supported frozen-v1 reader contract.
            notebook_worker=self.helpers.parent/'python/analytics/export.py'
            self.api('configure_analytics',python=str(notebook_worker.parent/'python'),worker=str(notebook_worker))
            console=Console(consumer,cli,[])
            e=console.action('create',target=console.target,key='bound-notebook',catalog=True)
            e=console.wait(console.action('start',id=e['id'],generation=0,key='start'))
            ws=console.connect(e)
            execute(ws,"assert spark.table('dataset_orders.public.orders').count()==3")
            assert e['epoch']['datasets'][0]['publication_id']==shared['id']
            ws.close(); e=console.restart(e); ws=console.connect(e)
            execute(ws,"assert spark.table('dataset_orders.public.orders').count()==3")
            ws.close();console.stop(e)
            self.configure(worker)
            self.check('cross_project_dataset_binding_and_restarted_notebook_keep_selected_incremental_epoch')
            # A stopped checkpoint must relocate immutable views alongside original
            # descriptors, and every restored catalog location must use the new root.
            with tempfile.TemporaryDirectory(prefix='sb-sy06-backup-') as temporary:
                backup=Path(temporary).resolve()/'backup'; restored=Path(temporary).resolve()/'restored'
                def backup_cli(*parts,at=None):
                    subprocess.run([str(self.binary),*map(str,parts),'--data-dir',str(at or self.root)],check=True,stdout=subprocess.DEVNULL,timeout=180)
                backup_cli('backup','create',backup)
                backup_cli('backup','verify',backup)
                backup_cli('backup','restore',backup,at=restored)
                assert not (restored/'restore-incomplete').exists()
                assert json.loads((restored/'catalog-restore.json').read_text())['state']=='reconciled'
                with sqlite3.connect(restored/'state.sqlite3') as db:
                    records=[json.loads(row[0]) for row in db.execute('SELECT record_json FROM catalog_publications')]
                for record in records:
                    for table in record['tables']:
                        path=Path(unquote(urlparse(table['body']['storage_location']).path))
                        assert path.is_relative_to(restored) and path.parent.name=='shared',path
            self.start()
            self.check('stopped_backup_restore_reconciles_incremental_catalog_view_locations')
        identity(action='revoke',principal=service)
        denied(lambda:self.opened(epoch=current['last_epoch_id'],ttl_ms=600000))
        wait(lambda:self.api('managed_capture',command=dict(kind='status',id=current['capture_id']))['desired']=='fenced',timeout=60)
        self.check('service_revocation_fences_incremental_capture_and_retained_epoch_new_readers')
        # Recovery must retain protected metadata without reopening it or failing daemon startup.
        self.stop(); self.start()
        denied(lambda:self.api('get_snapshot',id=current['last_epoch_id']))
        self.check('restart_with_revoked_service_epochs_keeps_access_closed')
        governed(dict(kind='delete',id=p['id'],expected_revision=governed(dict(kind='get',id=p['id']))['revision'],key='delete-incremental'))
        audit = identity(action='audit_export', after=0)
        assert any(e['event'].get('stream') == 'sync' for e in audit['events'])
        self.check('admission_and_run_transition_audit_contains_identifiers_without_rows_or_tokens')
        self.stop()


def main():
    parser = argparse.ArgumentParser()
    for name in ('binary', 'bundle', 'helpers', 'python', 'worker', 'report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--catalog-runtime',type=Path)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True,exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='sb-sy06-',dir='/tmp')).resolve(); root.chmod(0o700)
    cell = Surfaces(args.binary.resolve(), args.bundle.resolve(), args.helpers.resolve(), root)
    cell.catalog_runtime=args.catalog_runtime.resolve() if args.catalog_runtime else None
    def sha(path):
        with path.open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()
    report = dict(status='FAIL', checks=cell.checks, scope='Disposable source qualification; not an installed release.',
        binary_sha256=sha(args.binary), engine_manifest_sha256=sha(args.bundle/'manifest.json'),
        capture_source_sha256=sha(args.worker.parent/'capture/source.py'))
    try:
        cell.run(args.python.absolute(), args.worker.resolve()); report['status'] = 'PASS'
    finally:
        if (root / 'control.sock').exists():
            try: cell.stop()
            except Exception: report.update(status='FAIL', cleanup='failed')
        if report['status'] != 'PASS': report['state_dir'] = str(root)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        if report['status'] == 'PASS' and 'cleanup' not in report: shutil.rmtree(root)
    print(json.dumps(report, indent=2)); assert report['status'] == 'PASS'

if __name__ == '__main__': main()
