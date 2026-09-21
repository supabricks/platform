"""UC05: destination mapping, portable requirements and leased cross-project reads."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
from jsonschema import Draft202012Validator, FormatChecker
from cell import wait


def run(cell, root, installed, branch, work, token, api, check):
    repo=Path(__file__).resolve().parents[3]
    sys.path.insert(0,str(repo/'e2e/native/notebook-environments'))
    from client import Console, execute
    producer=dict(project_id=cell.project,worktree=str(work))
    def call(scope,action,**fields):return cell.request(method='api',api_version=1,binding=scope,action=dict(action=action,**fields))
    def package(scope,action,**fields):return call(scope,'project_apply',command=dict(action=action,**fields))
    def dataset(scope,action,**fields):return call(scope,'catalog_datasets',command=dict(action=action,**fields))
    def publication(action,**fields):return call(producer,'catalog_publication',command=dict(action=action,**fields))
    def target(p):return dict(deployment_id=p['deployment_id'],provider_id=p['namespace']['provider_id'],publication_id=p['id'])
    def rejected(fn):
        try:fn()
        except RuntimeError:return
        raise AssertionError('request unexpectedly accepted')
    def published():
        _,snapshot=cell.publish();v=publication('preview',epoch_id=snapshot['epoch_id'])
        p=publication('publish',epoch_id=v['epoch_id'],key=str(uuid.uuid4()),expected_preview=v['preview_hash'],expected_source_revision=v['source_revision'],expected_binding_revision=v['binding_revision'])['publication']
        def done():
            p1=publication('status',id=p['id'])['publication'];assert not p1['error'],p1['error'];return p1 if p1['state']=='published' else None
        return wait(done,timeout=60)
    plan_validator=Draft202012Validator(json.loads((repo/'schemas/project-plan-v1.schema.json').read_text()),format_checker=FormatChecker())
    def plan(scope,mappings=None):
        p=package(scope,'plan',options=dict(datasets=mappings or {}));plan_validator.validate(p);return p
    def apply(scope,p,key=None):
        key=key or str(uuid.uuid4());o=package(scope,'apply',plan=p,key=key)
        assert package(scope,'apply',plan=p,key=key)['id']==o['id']
        value=wait(lambda:(r if (r:=package(scope,'status',id=o['id']))['state'] in ('succeeded','failed','cancelled') else None),timeout=150)
        assert value['state']=='succeeded',value.get('error')
        return value
    def open_read(scope):
        s=call(scope,'analytics_open',branch='main',catalog=True,key=str(uuid.uuid4()),ttl_ms=600000)
        s=wait(lambda:(r if (r:=call(scope,'analytics_session',id=s['id']))['state'] in ('ready','failed','closed') else None),timeout=150)
        assert s['state']=='ready',s.get('error');return s
    def query(scope,s,sql):
        q=call(scope,'analytics_sql',id=s['id'],sql=sql)
        r=wait(lambda:(r if (r:=call(scope,'analytics_query',id=s['id'],query=q['id']))['state']!='running' else None),timeout=40)
        assert r['state']=='complete',r
        return r['rows']
    def close(scope,s):
        call(scope,'analytics_close',id=s['id']);wait(lambda:call(scope,'analytics_session',id=s['id'])['state']=='closed')
    def cli(project,*parts):return json.loads(subprocess.check_output([str(cell.binary),*parts,'--project',str(project),'--data-dir',str(cell.root)],text=True,timeout=180).strip().splitlines()[-1])
    cell.sql(branch,'SET ROLE supabricks_owner; CREATE TABLE uc05_shared(id integer); INSERT INTO uc05_shared VALUES(1)')
    p=published();scopes=[]
    for name in ('uc05-b','uc05-c'):
        path=root/name;path.mkdir();definition=str(uuid.uuid4())
        (path/'supabricks.toml').write_text(f'''format_version=2
id="{definition}"
name="{name}"
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
requirement="sales.orders.v1"
[resources.dataset.sales.provenance]
deployment_id="{p['deployment_id']}"
provider_id="{p['namespace']['provider_id']}"
publication_id="{p['id']}"
''')
        ctx=cell.request(method='project',source=dict(definition_id=definition,worktree=str(path)),command=dict(action='create',key=name,target=None))
        scope=dict(project_id=ctx['runtime_project_id'],worktree=str(path));scopes.append(scope)
        unresolved=plan(scope);assert next(s for s in unresolved['steps'] if s['logical']=='dataset.sales')['action']=='unresolved'
        rejected(lambda:package(scope,'apply',plan=unresolved,key='unresolved'))
        apply(scope,plan(scope,{'dataset.sales':target(p)}))
    b,c=scopes
    first=open_read(b);other=open_read(c)
    for scope,s in [(b,first),(c,other)]:
        assert query(scope,s,'SELECT count(*) FROM dataset_sales.public.uc05_shared')==[['1']]
        assert s['metadata']['datasets'][0]['publication_id']==p['id']
        assert s['metadata']['datasets'][0]['owner_deployment_id']==p['deployment_id']
        config=json.loads((cell.root/'session-work'/s['id']/'input.json').read_text())
        assert config['descriptor']['manifest']['tables']==[], 'consumer unexpectedly copied producer tables'
        assert config['datasets'][0]['descriptor']['manifest_sha256']==p['manifest_hash']
    assert dataset(producer,'references',target=target(p))['references']==dict(apply=0,binding=2,session=2)
    check('uc05_two_projects_share_one_publication_without_copies_or_ownership_transfer')
    close(c,other)
    cell.sql(branch,"SET ROLE supabricks_owner; ALTER TABLE uc05_shared ADD COLUMN note text; INSERT INTO uc05_shared VALUES(2,'new')")
    q=published()
    discovered=dataset(b,'updates',logical='dataset.sales')
    assert discovered['schema_changed'] and discovered['available']['target']==target(q) and not discovered['adopted']
    assert query(b,first,'SELECT count(*) FROM dataset_sales.public.uc05_shared')==[['1']]
    # Bind both revisions of the same producer, under separate per-session catalog names.
    path=Path(b['worktree']);manifest=path/'supabricks.toml'
    manifest.write_text(manifest.read_text()+'''\n[resources.dataset.old]
kind="catalog_dataset"
requirement="sales.orders.previous"
''')
    update=plan(b,{'dataset.sales':target(q),'dataset.old':target(p)})
    changed=next(s for s in update['steps'] if s['logical']=='dataset.sales')
    assert changed['action']=='update_binding' and changed['initialization']['schema_changed']
    apply(b,update)
    fresh=open_read(b)
    assert query(b,fresh,'SELECT count(*) FROM dataset_sales.public.uc05_shared')==[['2']]
    assert query(b,fresh,'SELECT count(*) FROM dataset_sales.public.uc05_shared n JOIN dataset_old.public.uc05_shared o ON n.id=o.id')==[['1']]
    assert query(b,first,'SELECT count(*) FROM dataset_sales.public.uc05_shared')==[['1']]
    assert {d['publication_id'] for d in fresh['metadata']['datasets']}=={p['id'],q['id']}
    assert fresh['metadata']['shared_source_transaction'] is False
    close(b,fresh)
    channels=[];console=Console(path,cli,channels)
    e=console.action('create',target=console.target,key='uc05-notebook',catalog=True)
    e=console.wait(console.action('start',id=e['id'],generation=0,key='start'))
    ws=console.connect(e)
    execute(ws,"assert spark.table('dataset_sales.public.uc05_shared').count()==2\nassert spark.table('dataset_old.public.uc05_shared').count()==1")
    assert {d['publication_id'] for d in e['epoch']['datasets']}=={p['id'],q['id']}
    ws.close();console.stop(e)
    check('uc05_reviewed_schema_diff_updates_new_readers_and_notebook_joins_record_each_revision')
    # Pure package operations remain usable with UC unreachable. Imported IDs are only provenance.
    pid=next(r['pid'] for r in cell.records() if r['role']=='unity-catalog');os.kill(pid,signal.SIGSTOP)
    archive=root/'uc05.sbproj';destination=root/'uc05-imported'
    try:
        report=cli(path,'project','pack','--output',str(archive))
        Draft202012Validator(json.loads((repo/'schemas/project-package-report-v1.schema.json').read_text()),format_checker=FormatChecker()).validate(report)
        subprocess.run([str(cell.binary),'project','verify',str(archive)],check=True,capture_output=True,timeout=20)
        subprocess.run([str(cell.binary),'project','unpack',str(archive),'--destination',str(destination)],check=True,capture_output=True,timeout=20)
    finally:
        try:os.kill(pid,signal.SIGCONT)
        except ProcessLookupError:pass
    wait(lambda:cell.request(method='catalog_service',command=dict(action='status'))['ready'],timeout=60)
    definition=report['inspection']['definition']['id']
    ctx=cell.request(method='project',source=dict(definition_id=definition,worktree=str(destination)),command=dict(action='create',key='imported',target=None))
    imported=dict(project_id=ctx['runtime_project_id'],worktree=str(destination))
    unresolved=plan(imported)
    assert len([s for s in unresolved['steps'] if s['action']=='unresolved'])==2
    rejected(lambda:package(imported,'apply',plan=unresolved,key='no-authority'))
    check('uc05_offline_pack_verify_import_preserve_requirements_without_destination_authority')
    publication('unpublish',id=p['id'],key='uc05-withdraw',expected_binding_revision=q['revision'])
    assert publication('status',id=p['id'])['publication']['state']=='retiring'
    rejected(lambda:call(b,'analytics_open',branch='main',catalog=True,key='withdrawn'))
    assert query(b,first,'SELECT count(*) FROM dataset_sales.public.uc05_shared')==[['1']]
    for scope in [b,c]:
        manifest=Path(scope['worktree'])/'supabricks.toml'
        manifest.write_text(manifest.read_text().split('[resources.dataset.sales]')[0])
        removal=plan(scope);assert any(s['action']=='unbind' for s in removal['steps'])
        apply(scope,removal)
        assert dataset(scope,'list')['bindings']==[]
    refs=dataset(producer,'references',target=target(p))['references'];assert refs==dict(apply=0,binding=0,session=1),refs
    assert query(b,first,'SELECT count(*) FROM dataset_sales.public.uc05_shared')==[['1']]
    close(b,first)
    wait(lambda:publication('status',id=p['id'])['publication']['state']=='retired',timeout=60)
    assert call(producer,'get_snapshot',id=p['epoch_id'])['state']=='available'
    assert cell.sql(branch,'SELECT count(*) FROM uc05_shared')=='2'
    check('uc05_consumer_decommission_releases_bindings_after_review_without_deleting_producer_data')

    # An object replaced after planning must fail activation, leaving no new ownership.
    manifest=Path(c['worktree'])/'supabricks.toml'
    manifest.write_text(manifest.read_text()+'\n[resources.dataset.sales]\nkind="catalog_dataset"\nrequirement="sales.orders.v1"\n')
    candidate=plan(c,{'dataset.sales':target(q)})
    endpoint=cell.request(method='catalog_service',command=dict(action='status'))['endpoint']
    body=q['tables'][0]['body'];name='.'.join(body[k] for k in ('catalog_name','schema_name','name'))
    assert api(endpoint,token(),'tables/'+name,method='DELETE')[0]==200
    assert api(endpoint,token(),'tables',method='POST',body=body)[0]==200
    rejected_apply=package(c,'apply',plan=candidate,key='uc05-replaced-object')
    failed=wait(lambda:(v if (v:=package(c,'status',id=rejected_apply['id']))['state'] in ('succeeded','failed') else None),timeout=60)
    assert failed['state']=='failed' and dataset(c,'list')['bindings']==[]
    assert dataset(producer,'references',target=target(q))['references']['apply']==0
    check('uc05_remote_uuid_replacement_after_plan_fails_activation_and_releases_pending_pins')
