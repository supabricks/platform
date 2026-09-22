"""UC03 durable complete-set publication against the actual installed UC/PG closure."""
import json
from pathlib import Path
import sqlite3
import os
import signal
import subprocess
import time
from urllib.parse import urlparse, unquote
from jsonschema import Draft202012Validator, FormatChecker
from cell import wait


def run(cell, root, installed, branch, work, token, api, check):
    repo=Path(__file__).resolve().parents[3]
    schema=json.loads((repo/'schemas/catalog-publication-response-v1.schema.json').read_text())
    tools=json.loads((repo/'crates/local/tests/fixtures/local-mcp-tools.json').read_text())
    embedded=next(t['outputSchema'] for t in tools if t['name']=='catalog_publication')
    validators=[Draft202012Validator(s,format_checker=FormatChecker()) for s in (schema,embedded)]
    binding=dict(project_id=cell.project,worktree=str(work))
    def request(action, **fields):
        result=cell.request(method='api',api_version=1,binding=binding,action=dict(action='catalog_publication',command=dict(action=action,**fields)))
        for validator in validators:validator.validate(result)
        return result
    def rejected(action,**fields):
        try:request(action,**fields)
        except RuntimeError:return
        raise AssertionError('stale/foreign request accepted')
    def provider():return cell.request(method='catalog_service',command=dict(action='status'))
    def ready():return wait(lambda:(s if (s:=provider())['ready'] else None),timeout=60)
    def status(id):return request('status',id=id)['publication']
    def complete(id,state='published'):
        def poll():
            p=status(id)
            assert not p['error'],p['error']
            return p if p['state']==state else None
        return wait(poll,timeout=60)
    def accept(preview,key):return request('publish',epoch_id=preview['epoch_id'],key=key,expected_preview=preview['preview_hash'],expected_source_revision=preview['source_revision'],expected_binding_revision=preview['binding_revision'])['publication']
    cell.work=work
    snapshot=cell.api('current_snapshot',branch='main')['publication']
    first=request('preview',epoch_id=snapshot['epoch_id'])
    assert first['retention']['durable'] and len(first['tables'])>=2
    assert request('resolve',branch='main')['publication'] is None
    # Hold the actual provider before publishing so registration cannot finish
    # between API/schema validation and our durable-intent observation. Crash
    # the writer at that boundary, then release UC even if the assertion fails.
    pid=next(record['pid'] for record in cell.records() if record['role']=='unity-catalog')
    os.kill(pid,signal.SIGSTOP)
    try:
        p=accept(first,'uc03-first');ids=[t['id'] for t in p['tables']]
        def intent():
            with sqlite3.connect(cell.root/'state.sqlite3') as db:
                saved=json.loads(db.execute('SELECT record_json FROM catalog_publications WHERE id=?',(p['id'],)).fetchone()[0])
            assert saved['state']=='registering', 'missed crash boundary'
            assert not saved['error'],saved['error']
            return any(t['state']=='creating' for t in saved['tables'])
        wait(intent,timeout=10)
        cell.daemons[-1].kill();cell.daemons[-1].wait(timeout=5)
    finally:
        try:os.kill(pid,signal.SIGCONT)
        except ProcessLookupError:pass
    cell.start();ready()
    p=complete(p['id']);assert [t['id'] for t in p['tables']]==ids
    assert accept(first,'uc03-first')['id']==p['id']
    rejected('publish',epoch_id=first['epoch_id'],key='uc03-first',expected_preview='0'*64,expected_source_revision=first['source_revision'],expected_binding_revision=first['binding_revision'])
    assert request('resolve',branch='main')['publication']['id']==p['id']
    endpoint=provider()['endpoint']
    cli=json.loads(subprocess.check_output([str(cell.binary),'catalog','publication','status',p['id'],'--project',str(work),'--data-dir',str(cell.root)],text=True))
    assert cli['publication']['id']==p['id']
    denied=subprocess.run([str(cell.binary),'catalog','publication','preview',p['epoch_id'],'--data-dir',str(cell.root)],cwd=root,capture_output=True,text=True)
    assert denied.returncode!=0
    meta=cell.request(method='api',api_version=1,binding=binding,action=dict(action='catalog_metadata',command=dict(action='list',branch='main')))
    meta=wait(lambda:(v if (v:=cell.request(method='api',api_version=1,binding=binding,action=dict(action='catalog_metadata',command=dict(action='poll',id=meta['id']))))['state']!='running' else None))
    assert meta['state']=='complete'
    assert {a['uc_object_id'] for a in meta['result']['assets'] if a['epoch_id']==p['epoch_id']}==set(ids)
    for t in p['tables']:
        body=t['body'];name='.'.join(body[k] for k in ('catalog_name','schema_name','name'))
        code,remote=api(endpoint,token(),'tables/'+name)
        assert code==200 and remote['table_id']==t['id'] and remote['storage_location']==body['storage_location']
    check('uc03_preview_idempotent_publish_crash_recovery_and_complete_alias')
    rejected('publish',epoch_id=first['epoch_id'],key='uc03-stale-preview',expected_preview=first['preview_hash'],expected_source_revision=first['source_revision'],expected_binding_revision=first['binding_revision'])
    lease=cell.api('pin_snapshot',id=p['epoch_id'],ttl_ms=300000)
    _,snapshot2=cell.publish()
    second=request('preview',epoch_id=snapshot2['epoch_id'])
    pid=next(record['pid'] for record in cell.records() if record['role']=='unity-catalog')
    os.kill(pid,signal.SIGSTOP)
    try:
        q=accept(second,'uc03-refresh')
        wait(lambda:status(q['id'])['error'],timeout=15)
        assert request('resolve',branch='main')['publication']['id']==p['id']
        assert cell.sql(branch,'SELECT 42')=='42'
    finally:
        try:os.kill(pid,signal.SIGCONT)
        except ProcessLookupError:pass
    ready();request('resume',id=q['id']);q=complete(q['id'])
    check('uc03_catalog_outage_preserves_old_head_and_explicit_resume_reconciles')
    assert request('resolve',branch='main')['publication']['id']==q['id']
    assert cell.api('collect_snapshots',branch='main',keep=1)['deleting']==[]
    cell.stop()
    with sqlite3.connect(cell.root/'state.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM catalog_retention').fetchone()[0]==2
    cell.start();ready()
    assert request('resolve',branch='main')['publication']['id']==q['id']
    check('uc03_refresh_retains_old_revision_across_gc_and_stopped_daemon')
    request('unpublish',id=p['id'],key='uc03-remove-first',expected_binding_revision=2)
    time.sleep(.3);assert status(p['id'])['state']=='retiring'
    cell.api('release_snapshot_lease',id=lease['id'])
    p=complete(p['id'],'retired')
    assert request('resolve',branch='main')['publication']['id']==q['id']
    for t in p['tables']:
        b=t['body'];name='.'.join(b[k] for k in ('catalog_name','schema_name','name'))
        assert api(provider()['endpoint'],token(),'tables/'+name)[0]==404
        assert Path(unquote(urlparse(b['storage_location']).path)).exists()
    assert p['epoch_id'] in cell.api('collect_snapshots',branch='main',keep=1)['deleting']
    check('uc03_unpublish_waits_for_readers_deletes_only_metadata_then_releases_gc')
    # Reusing the alias without the protocol header generates a new server UUID.
    t=q['tables'][0];b=t['body'];name='.'.join(b[k] for k in ('catalog_name','schema_name','name'));endpoint=provider()['endpoint']
    assert api(endpoint,token(),'tables/'+name,method='DELETE')[0]==200
    code,replacement=api(endpoint,token(),'tables',method='POST',body=b)
    assert code==200 and replacement['table_id']!=t['id']
    request('unpublish',id=q['id'],key='uc03-remove-replaced',expected_binding_revision=2)
    wait(lambda:status(q['id'])['error'])
    assert request('resolve',branch='main')['publication'] is None
    assert api(endpoint,token(),'tables/'+name)[1]['table_id']==replacement['table_id']
    with sqlite3.connect(cell.root/'state.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM catalog_retention WHERE publication_id=?',(q['id'],)).fetchone()[0]==1
    check('uc03_recreated_alias_blocks_cleanup_and_preserves_retention')
