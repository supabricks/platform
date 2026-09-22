#!/usr/bin/env python3
"""Exercise the named alpha.34 UC backend transition with both real installations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'e2e/native/catalog'))
from probe import CatalogCell
from cell import wait
from service import api


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser()
    for key in ['release','previous-release','report']:parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    root=Path(tempfile.mkdtemp(prefix='s9u-'))
    prefix=root/'programs';(prefix/'releases').mkdir(parents=True);(prefix/'bin').mkdir()
    expected=json.loads((ROOT/'components/execution-runtime.lock.json').read_text())['platform']
    assert digest(args.previous_release/'release.json')==expected['inventory_sha256']
    source=json.loads((args.previous_release/'release.json').read_text())
    candidate=json.loads((args.release/'release.json').read_text())
    old=prefix/'releases'/source['version'];new=prefix/'releases'/candidate['version']
    shutil.copytree(args.previous_release,old);shutil.copytree(args.release,new)
    (prefix/'current').symlink_to(Path('releases')/source['version'])
    for name in ['supabricks','psql']:(prefix/'bin'/name).symlink_to(Path('../current/bin')/name)
    data=root/'data';data.mkdir();work=root/'producer';work.mkdir()
    cell=CatalogCell(old/'bin/supabricks',old/'engine',old/'helpers',data);cell.binary=old/'bin/supabricks';cell.work=work
    (work/'supabricks.toml').write_text(f'format_version=1\nid="{cell.project}"\nname="upgrade-producer"\n')
    report=dict(status='FAIL',checks=[],binary_sha256=digest(new/'bin/supabricks'),
                release_identity=digest(new/'release.json'),predecessor_identity=expected['inventory_sha256'])
    def cli(binary,*args,at=data):
        suffix=[] if args==('installation','verify') else ['--data-dir',str(at)]
        result=subprocess.run([str(binary),*map(str,args),*suffix],capture_output=True,text=True,timeout=240)
        assert result.returncode==0,result.stderr[-1200:]
        return json.loads(result.stdout)
    def call(scope,action,**fields):return cell.request(method='api',api_version=1,binding=scope,action=dict(action=action,**fields))
    producer=dict(project_id=cell.project,worktree=str(work))
    def publication(action,**fields):return call(producer,'catalog_publication',command=dict(action=action,**fields))
    def ready():return wait(lambda:(s if (s:=cell.request(method='catalog_service',command=dict(action='status')))['ready'] else None),timeout=90)
    try:
        cli(old/'bin/supabricks','installation','verify')
        cli(new/'bin/supabricks','installation','verify')
        cell.start();initial=ready()
        cell.request(method='resolve_binding',source=dict(definition_id=cell.project,worktree=str(work)))
        branch=cell.create('main')
        cell.sql(branch,'SET ROLE supabricks_owner; CREATE TABLE preserved(id integer); INSERT INTO preserved VALUES(41),(1)')
        operation=call(producer,'catalog_metadata',command=dict(action='ensure_namespace'))
        if operation.get('state')=='running':
            wait(lambda:call(producer,'catalog_metadata',command=dict(action='poll',id=operation['id']))['state']=='complete',timeout=60)
        _,snapshot=cell.publish()
        preview=publication('preview',epoch_id=snapshot['epoch_id'])
        published=publication('publish',epoch_id=preview['epoch_id'],key=str(uuid.uuid4()),expected_preview=preview['preview_hash'],expected_source_revision=preview['source_revision'],expected_binding_revision=preview['binding_revision'])['publication']
        published=wait(lambda:(p if (p:=publication('status',id=published['id'])['publication'])['state']=='published' else None),timeout=90)
        old_token=(data/'catalog/etc/conf/token.txt').read_text().strip()
        report['checks'].append('qualified_predecessor_native_pg_and_published_uc_snapshot')
        cell.stop()
        backup=root/'before-upgrade'
        value=cli(new/'bin/supabricks','installation','upgrade','--prefix',prefix,'--previous',old,'--backup',backup)
        assert value['upgraded'] is True and (prefix/'current').resolve()==new
        assert cli(old/'bin/supabricks','backup','verify',backup)['verified'] is True
        report['checks'].append('named_uc_transition_uses_source_binary_checkpoint_and_preserves_backup')
        cell.binary=new/'bin/supabricks';cell.bundle=new/'engine';cell.helpers=new/'helpers';cell.start()
        current=ready()
        assert current['metastore_id']==initial['metastore_id']
        branch=cell.state(branch,'running')
        assert cell.sql(branch,'SELECT sum(id) FROM preserved')=='42'
        retained=publication('status',id=published['id'])['publication']
        assert [t['id'] for t in retained['tables']]==[t['id'] for t in published['tables']]
        assert api(current['endpoint'],old_token)[0]==401
        report['checks'].append('upgraded_pg_publication_and_metastore_ids_survive_with_rotated_uc_credentials')
        cell.stop()
        # The untouched backup still opens only with its source release, including
        # the old H2 runtime, independently of the newly activated candidate.
        restored=root/'r';cli(old/'bin/supabricks','backup','restore',backup,at=restored)
        cell.root=restored;cell.binary=old/'bin/supabricks';cell.bundle=old/'engine';cell.helpers=old/'helpers';cell.start()
        assert ready()['metastore_id']==initial['metastore_id']
        branch=cell.state(branch,'running')
        assert cell.sql(branch,'SELECT sum(id) FROM preserved')=='42'
        report['checks'].append('pre_upgrade_backup_restores_with_its_original_catalog_binary')
        report['status']='PASS'
    finally:
        if cell.daemons and cell.daemons[-1].poll() is None:cell.stop()
        args.report.write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':main()
