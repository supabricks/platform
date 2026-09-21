"""UC04 installed SQL, Spark Connect and Jupyter frozen catalog reads."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
from cell import wait


def run(cell, root, installed, branch, work, token, api, check):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'notebook-environments'))
    from client import Console, execute
    def cli(project,*parts):
        return json.loads(subprocess.check_output([str(cell.binary),*parts,'--project',str(project),'--data-dir',str(cell.root)],text=True,timeout=180).strip().splitlines()[-1])
    def publication(action,**fields):return cell.api('catalog_publication',command=dict(action=action,**fields))
    def status(id):return publication('status',id=id)['publication']
    def published(state='published',**kwargs):
        _,snapshot=cell.publish()
        preview=publication('preview',epoch_id=snapshot['epoch_id'])
        p=publication('publish',epoch_id=preview['epoch_id'],key=str(uuid.uuid4()),expected_preview=preview['preview_hash'],expected_source_revision=preview['source_revision'],expected_binding_revision=preview['binding_revision'])['publication']
        def done():
            v=status(p['id']);assert not v['error'],v['error'];return v if v['state']==state else None
        return wait(done,timeout=60)
    def ready(s,expected='ready'):
        result=wait(lambda:(r if (r:=cell.api('analytics_session',id=s['id']))['state'] in ('ready','failed','closed') else None),timeout=150)
        assert result['state']==expected,result
        return result
    def open_read(**fields):return ready(cell.api('analytics_open',branch='main',key=str(uuid.uuid4()),catalog=True,**fields))
    def query(s,sql):
        q=cell.api('analytics_sql',id=s['id'],sql=sql)
        r=wait(lambda:(r if (r:=cell.api('analytics_query',id=s['id'],query=q['id']))['state']!='running' else None),timeout=40)
        assert r['state']=='complete',r
        return r['rows']
    def close(s):cell.api('analytics_close',id=s['id']);ready(s,'closed')
    def provider():return cell.request(method='catalog_service',command=dict(action='status'))
    def provider_ready():return wait(lambda:(s if (s:=provider())['ready'] else None),timeout=60)
    def workspace(console,action,**fields):return console.request('workspace',dict(action='analytics',command=dict(action=action,**fields)))['value']
    cell.sql(branch,'SET ROLE supabricks_owner; CREATE TABLE uc04_values(id integer); INSERT INTO uc04_values VALUES(1)')
    p=published();channels=[]
    console=Console(work,cli,channels)
    first=ready(workspace(console,'open',target=console.target,key='uc04-console',catalog=True))
    catalog=p['namespace']['catalog'];qualified=f'`{catalog}`.public.uc04_values'
    assert first['metadata']['catalog']['publication_id']==p['id']
    assert query(first,f'SELECT count(*) FROM {qualified}')==[['1']]
    q=workspace(console,'sql',id=first['id'],sql='SELECT count(*) FROM public.uc04_values',max_rows=100,timeout_ms=10000)
    wait(lambda:(s if (s:=workspace(console,'status',id=first['id']))['query']['state']!='running' else None))
    assert workspace(console,'status',id=first['id'])['query']['rows']==[['1']]
    # Same endpoint via the independent Spark Connect client, including UC physical aliases.
    table=next(t for t in p['tables'] if t['source_name']=='uc04_values')
    physical=f"`{catalog}`.analytics.`{table['body']['name']}`"
    script=root/'uc04-spark.py';script.write_text('''import json,sys
from pyspark.sql import SparkSession
spark=SparkSession.builder.remote(sys.argv[1]).getOrCreate()
assert spark.table(sys.argv[2]).count()==1
assert json.loads(spark.sql('SELECT metadata_json FROM _supabricks.epoch').first()[0])['catalog']['publication_id']==sys.argv[3]
print('PASS')
''')
    assert subprocess.check_output([str(cell.python),str(script),first['endpoint'],physical,p['id']],text=True,timeout=45).strip()=='PASS'
    e=console.action('create',target=console.target,key='uc04-notebook',catalog=True)
    e=console.wait(console.action('start',id=e['id'],generation=0,key='uc04-start'))
    ws=console.connect(e)
    assert e['epoch']['catalog']==first['metadata']['catalog']
    execute(ws,f"assert spark.table({qualified!r}).count()==1\nassert spark.table('public.uc04_values').count()==1")
    check('uc04_console_sql_spark_connect_notebook_share_frozen_revision_and_aliases')
    cell.sql(branch,'SET ROLE supabricks_owner; INSERT INTO uc04_values VALUES(2)')
    q=published()
    assert query(first,f'SELECT count(*) FROM {qualified}')==[['1']]
    execute(ws,f"assert spark.table({qualified!r}).count()==1")
    # Restart retains the old explicit epoch even after the logical head moves.
    ws.close();e=console.restart(e);ws=console.connect(e)
    assert e['epoch']['catalog']['publication_id']==p['id']
    execute(ws,f"assert spark.table({qualified!r}).count()==1")
    ws.close();console.stop(e)
    fresh=open_read();assert fresh['catalog']['publication_id']==q['id']
    assert query(fresh,f'SELECT count(*) FROM {qualified}')==[['2']]
    close(fresh)
    check('uc04_refresh_changes_new_sessions_only_and_notebook_restart_retains_revision')
    pid=next(r['pid'] for r in cell.records() if r['role']=='unity-catalog')
    os.kill(pid,signal.SIGSTOP)
    try:
        failed=cell.api('analytics_open',branch='main',key='uc04-outage',catalog=True)
        ready(failed,'failed')
        assert query(first,f'SELECT count(*) FROM {qualified}')==[['1']]
        assert cell.sql(branch,'SELECT 42')=='42'
    finally:
        try:os.kill(pid,signal.SIGCONT)
        except ProcessLookupError:pass
    provider_ready()
    check('uc04_outage_fails_new_resolution_without_fallback_and_preserves_leased_reads')
    publication('unpublish',id=p['id'],key='uc04-retire',expected_binding_revision=q['revision'])
    time.sleep(.3);assert status(p['id'])['state']=='retiring'
    assert query(first,'SELECT count(*) FROM public.uc04_values')==[['1']]
    assert cell.api('collect_snapshots',branch='main',keep=1)['deleting']==[]
    close(first)
    wait(lambda:status(p['id'])['state']=='retired',timeout=60)
    check('uc04_retirement_waits_for_session_worker_death')
    fresh=open_read(ttl_ms=10000)
    wait(lambda:cell.api('analytics_session',id=fresh['id'])['state']=='closed',timeout=20)
    assert cell.api('analytics_session',id=fresh['id'])['error']=='expired'
    # A recreated same-name object must not become a different dataset at open.
    table=q['tables'][0];body=table['body'];name='.'.join(body[k] for k in ('catalog_name','schema_name','name'))
    endpoint=provider_ready()['endpoint']
    assert api(endpoint,token(),'tables/'+name,method='DELETE')[0]==200
    assert api(endpoint,token(),'tables',method='POST',body=body)[0]==200
    ready(cell.api('analytics_open',branch='main',key='uc04-foreign',catalog=True),'failed')
    check('uc04_expiry_closes_lease_and_recreated_uc_uuid_is_rejected')
