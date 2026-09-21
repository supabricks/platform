#!/usr/bin/env python3
"""UC07: stopped/moved catalog recovery over real H2, PostgreSQL and Sail."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from service import installed_fixture, api
from probe import CatalogCell, sha
from cell import wait


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['release', 'binary', 'uc-runtime', 'report']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--disk-full',action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='sb-uc07-', dir='/tmp')).resolve()
    root.chmod(0o700)
    print('UC07 fixture:', root, flush=True)
    prefix=root/'install';(prefix/'releases').mkdir(parents=True);(prefix/'bin').mkdir()
    old_version=json.loads((args.release/'release.json').read_text())['version']
    installed = prefix/'releases'/old_version
    installed_fixture(args.release.resolve(), args.binary.resolve(), args.uc_runtime.resolve(), installed)
    (prefix/'current').symlink_to(Path('releases')/old_version)
    for name in ['supabricks','psql']:(prefix/'bin'/name).symlink_to(Path('../current/bin')/name)
    data = root / 'data'
    data.mkdir(mode=0o700)
    cell = CatalogCell(installed / 'bin/supabricks', installed / 'engine', installed / 'helpers', data)
    cell.binary = installed / 'bin/supabricks'
    work = root / 'producer'
    work.mkdir()
    (work / 'supabricks.toml').write_text(f'format_version=1\nid="{cell.project}"\nname="uc07"\n')
    cell.work = work
    report = dict(status='FAIL', checks=[], binary_sha256=sha(args.binary),
        network_evidence=os.environ.get('SB_UC00_NETWORK_EVIDENCE', 'local development; host network not isolated'))
    running = False
    def check(name):
        report['checks'].append(name)
        print('PASS', name, flush=True)
    def cli(*parts, at=None, success=True):
        p = subprocess.run([str(cell.binary), *map(str, parts), '--data-dir', str(at or cell.root)],
            capture_output=True, text=True, timeout=180)
        assert p.returncode == 0 if success else p.returncode != 0, p.stderr[-1500:]
        return json.loads(p.stdout) if success else p.stderr
    def ready():
        return wait(lambda: (s if (s := cell.request(method='catalog_service', command=dict(action='status')))['ready'] else None), timeout=65)
    def call(scope, action, **fields):
        return cell.request(method='api', api_version=1, binding=scope, action=dict(action=action, **fields))
    producer = dict(project_id=cell.project, worktree=str(work))
    def metadata(action, **fields):
        v = call(producer, 'catalog_metadata', command=dict(action=action, **fields))
        if v.get('state') == 'running':
            v = wait(lambda: (x if (x := call(producer, 'catalog_metadata', command=dict(action='poll', id=v['id'])))['state'] != 'running' else None), timeout=30)
            assert v['state'] == 'complete', v
        return v
    def query(scope):
        session = call(scope, 'analytics_open', branch='main', key=str(uuid.uuid4()), catalog=True)
        session = wait(lambda: (s if (s := call(scope, 'analytics_session', id=session['id']))['state'] in ('ready','failed') else None), timeout=120)
        assert session['state'] == 'ready', session.get('error')
        q = call(scope, 'analytics_sql', id=session['id'], sql='SELECT sum(id) FROM dataset_sales.public.sales')
        result = wait(lambda: (r if (r := call(scope, 'analytics_query', id=session['id'], query=q['id']))['state'] != 'running' else None), timeout=40)
        assert result['rows'] == [['3']], result
        call(scope, 'analytics_close', id=session['id'])
        wait(lambda: call(scope,'analytics_session',id=session['id'])['state']=='closed')
    try:
        cell.start(); running = True
        initial = ready()
        cell.request(method='resolve_binding', source=dict(definition_id=cell.project, worktree=str(work)))
        branch = cell.create('main')
        cell.sql(branch, 'SET ROLE supabricks_owner; CREATE TABLE sales(id integer); INSERT INTO sales VALUES(1),(2)')
        metadata('ensure_namespace')
        _, snapshot = cell.publish()
        def publication(action, **fields):
            return call(producer,'catalog_publication',command=dict(action=action,**fields))
        preview = publication('preview',epoch_id=snapshot['epoch_id'])
        pub = publication('publish',epoch_id=preview['epoch_id'],key=str(uuid.uuid4()),expected_preview=preview['preview_hash'],expected_source_revision=preview['source_revision'],expected_binding_revision=preview['binding_revision'])['publication']
        pub = wait(lambda: (p if (p := publication('status',id=pub['id'])['publication'])['state']=='published' else None),timeout=60)
        definition=str(uuid.uuid4()); consumer=data/'console-created-projects'/str(uuid.uuid4());consumer.mkdir(parents=True)
        (consumer/'supabricks.toml').write_text(f'''format_version=2
id="{definition}"
name="consumer"
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
[resources.dataset.sales]
kind="catalog_dataset"
requirement="sales.v1"
''')
        ctx=cell.request(method='project',source=dict(definition_id=definition,worktree=str(consumer)),command=dict(action='create',key='consumer',target=None))
        scope=dict(project_id=ctx['runtime_project_id'],worktree=str(consumer))
        target=dict(deployment_id=pub['deployment_id'],provider_id=pub['namespace']['provider_id'],publication_id=pub['id'])
        plan=call(scope,'project_apply',command=dict(action='plan',options=dict(datasets={'dataset.sales':target})))
        op=call(scope,'project_apply',command=dict(action='apply',plan=plan,key='bind'))
        op=wait(lambda:(o if (o:=call(scope,'project_apply',command=dict(action='status',id=op['id'])))['state'] in ('succeeded','failed') else None),timeout=150)
        assert op['state']=='succeeded',op.get('error')
        query(scope)
        old_token=(data/'catalog/etc/conf/token.txt').read_text().strip()
        check('two_project_bound_read_before_checkpoint')
        # The production command coordinates readers, PG, UC and the daemon.
        backup=root/'backup';cli('backup','create',backup);running=False
        cli('backup','verify',backup)
        manifest=json.loads((backup/'backup.json').read_text())
        assert 'catalog/etc/db/h2db.mv.db' in manifest['files']
        assert 'catalog-format.json' in manifest['files']
        assert 'catalog/launch.json' not in manifest['files']
        with sqlite3.connect(f'file:{backup}/data/state.sqlite3?immutable=1',uri=True) as db:
            assert db.execute('SELECT count(*) FROM catalog_publication_refs WHERE reference_key LIKE \'binding:%\'').fetchone()[0]==1
        check('stopped_checkpoint_includes_backend_journal_bindings_and_retention')
        interrupted=root/'interrupted'
        process=subprocess.Popen([str(cell.binary),'backup','restore',str(backup),'--data-dir',str(interrupted)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        wait(lambda:(interrupted/'restore-incomplete').exists(),timeout=30)
        process.kill();process.wait(timeout=10)
        cli('up',at=interrupted,success=False)
        cli('backup','verify',backup)
        check('interrupted_restore_remains_guarded_and_original_backup_verifies')
        # The source directory is absent while verifying all restored locations.
        held=root/'source-held';data.rename(held)
        restored=root/'restored root';cli('backup','restore',backup,at=restored)
        assert not (restored/'restore-incomplete').exists()
        assert json.loads((restored/'catalog-restore.json').read_text())['state']=='reconciled'
        scope['worktree']=str(restored/consumer.relative_to(data))
        cell.root=restored;cell.start();running=True
        health=ready();assert health['metastore_id']==initial['metastore_id']
        assert api(health['endpoint'],old_token)[0]==401
        current=publication('status',id=pub['id'])['publication']
        assert [t['id'] for t in current['tables']]==[t['id'] for t in pub['tables']]
        assert all('/restored%20root/' in t['body']['storage_location'] for t in current['tables'])
        assert cell.sql(branch,'SELECT sum(id) FROM sales')=='3'
        query(scope)
        check('moved_root_preserves_ids_and_bindings_relocates_files_and_rotates_credentials')
        assert call(producer,'collect_snapshots',branch='main',keep=1)['deleting']==[]
        check('restored_binding_keeps_snapshot_retained')
        cell.stop();running=False
        # Corruption present before backup cannot be blessed by checksumming it.
        h2=restored/'catalog/etc/db/h2db.mv.db';saved=root/'h2.saved';shutil.copy2(h2,saved)
        h2.write_bytes(b'corrupt H2 fixture')
        cli('backup','create',root/'corrupt-backup',success=False)
        assert not (root/'corrupt-backup/backup.json').exists()
        shutil.copy2(saved,h2)
        check('corrupt_h2_refuses_checkpoint_without_publishing_backup')
        format_path=restored/'catalog-format.json';original=format_path.read_bytes()
        value=json.loads(original);value['backend_schema']=999;format_path.write_text(json.dumps(value))
        cli('backup','create',root/'wrong-format',success=False)
        format_path.write_bytes(original)
        check('unsupported_backend_contract_refuses_implicit_migration')
        # Unavailable external catalogs are recorded as external references only.
        cell.start();running=True;ready()
        token=root/'external-token';token.write_text('uc07-unavailable-external-token');token.chmod(0o600)
        cli('catalog','service','configure','external','--endpoint','https://127.0.0.1:1','--token-file',token,'--metastore-id',initial['metastore_id'])
        external_backup=root/'external-backup';cli('backup','create',external_backup);running=False
        external_restore=root/'external';cli('backup','restore',external_backup,at=external_restore)
        assert (external_restore/'catalog-external-restore.json').exists()
        cell.root=external_restore;cell.start();running=True
        status=cell.request(method='catalog_service',command=dict(action='status'))
        assert status['state']=='failed' and status['error']=='external_restore_requires_explicit_rebind'
        wait(lambda:cell.sql(branch,'SELECT count(*) FROM sales')=='2')
        check('missing_external_provider_requires_explicit_rebind_and_preserves_postgres')
        cell.stop();running=False
        candidate=prefix/'releases/v0.1.0-alpha.33'
        shutil.copytree(installed,candidate,copy_function=os.link)
        release=json.loads((candidate/'release.json').read_text());release['version']='v0.1.0-alpha.33'
        (candidate/'release.json').unlink();(candidate/'release.json').write_text(json.dumps(release,indent=2)+'\n')
        cell.binary=candidate/'bin/supabricks'
        upgrade_backup=root/'upgrade-backup'
        def upgrade():return cli('installation','upgrade','--prefix',prefix,'--previous',installed,'--backup',upgrade_backup)
        upgrade()
        completed=json.loads((cell.root/'last-upgrade.json').read_text())
        saved=json.loads((upgrade_backup/'backup.json').read_text())
        assert completed['catalog_state_sha256']
        journal=dict(version=1,previous=str(installed),prefix=str(prefix),backup=str(upgrade_backup),
            **{'from':completed['from'],'to':completed['to']},
            database_sha256=saved['files']['state.sqlite3']['sha256'],catalog_state_sha256=completed['catalog_state_sha256'])
        for phase in ['prepared','runtime_rebound','current_activated']:
            (cell.root/'upgrade.json').write_text(json.dumps(journal));(cell.root/'upgrade.json').chmod(0o600)
            if phase=='prepared':shutil.copy2(upgrade_backup/'data/runtime.json',cell.root/'runtime.json')
            if phase!='current_activated':
                (prefix/'current').unlink();(prefix/'current').symlink_to(Path('releases')/old_version)
            cli('up',success=False)
            upgrade()
            assert not (cell.root/'upgrade.json').exists()
        cell.binary=installed/'bin/supabricks'
        cli('installation','upgrade','--prefix',prefix,'--previous',candidate,'--backup',root/'downgrade',success=False)
        cell.binary=candidate/'bin/supabricks';cell.bundle=candidate/'engine';cell.helpers=candidate/'helpers'
        cell.start();running=True
        cli('catalog','service','configure','local');ready()
        scope['worktree']=str(cell.root/consumer.relative_to(data))
        query(scope)
        check('catalog_upgrade_checkpoint_resumes_each_activation_boundary_and_refuses_downgrade')
        if args.disk_full:
            assert os.uname().sysname=='Linux'
            volume=root/'full-volume';volume.mkdir()
            subprocess.run(['sudo','-n','mount','-t','tmpfs','-o',f'size=16m,uid={os.getuid()},gid={os.getgid()},mode=0700','tmpfs',str(volume)],check=True)
            try:
                error=cli('backup','create',volume/'no-space',success=False);running=False
                assert 'space' in error.lower(),error
                assert not (volume/'no-space/backup.json').exists()
                cli('backup','verify',upgrade_backup)
                cell.start();running=True;ready();query(scope)
                check('bounded_filesystem_enospc_publishes_no_backup_and_preserves_source')
            finally:
                subprocess.run(['sudo','-n','umount',str(volume)],check=True)
        else:
            report['disk_exhaustion']='not run locally; Linux CI uses a disposable 16 MiB tmpfs'
        report['status']='PASS'
    except Exception as e:
        report['error']=str(e)[:2000]
        raise
    finally:
        if running:
            cell.stop()
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report),flush=True)


if __name__=='__main__':
    main()
