"""NE03 real installed kernels, public console authentication and offline environments."""
import argparse
import hashlib
import http.cookiejar
import json
import os
import psutil
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import uuid
import websocket


def main(args):
    release=args.release.resolve(); binary=release/'bin/supabricks'
    root=Path(tempfile.mkdtemp(prefix='sb-ne03-',dir='/tmp')).resolve();root.chmod(0o700)
    data=root/'data'; report={'status':'running','checks':[], 'release_sha256':hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),
        'network_evidence':os.environ.get('SB_NE02_NETWORK_EVIDENCE','local run; external networking not isolated')}
    projects=[]; channels=[]
    def cli(project,*parts):
        value=subprocess.run([str(binary),*parts,'--project',str(project),'--data-dir',str(data)],capture_output=True,text=True,timeout=180)
        if value.returncode:
            (root/'private-cli.log').write_text(value.stdout+value.stderr);raise RuntimeError('NE03 CLI failed: '+parts[0])
        return json.loads(value.stdout.strip().splitlines()[-1])
    def check(name):
        report['checks'].append(name); args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n');print(name,flush=True)
    class Console:
        def __init__(self,project):
            self.created={}
            self.project=project;url=cli(project,'console','--no-open')['url'];self.origin=url.split('/#')[0].rstrip('/')
            self.jar=http.cookiejar.CookieJar();self.http=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
            self.csrf=self.request('session',{'token':url.split('#launch=')[1]})['csrf']
            overview=self.request('overview');b=next(b for b in overview['branches'] if b['name']=='main')
            self.target={'branch':b['id'],'revision':b['revision']}
        def request(self,path,value=None):
            headers={'Origin':self.origin,'X-Supabricks-Console':'1','Content-Type':'application/json'}
            if hasattr(self,'csrf'):headers['X-Supabricks-CSRF']=self.csrf
            req=urllib.request.Request(self.origin+'/api/'+path,headers=headers,data=None if value is None else json.dumps(value).encode())
            try:
                with self.http.open(req,timeout=30) as result:return json.load(result)
            except urllib.error.HTTPError as error:
                # Preserve the bounded API diagnostic; a bare 503 hides whether
                # startup hit a transport deadline or the daemon rejected it.
                detail=error.read(8192).decode('utf-8',errors='replace')
                raise RuntimeError(f'console {path}: HTTP {error.code}: {detail}') from None
        def action(self,action,**fields):return self.request('workspace',{'action':'notebook','command':{'action':action,**fields}})['value']
        def wait(self,e,state='ready'):
            deadline=time.monotonic()+150
            while time.monotonic()<deadline:
                e=next(item for item in self.action('list') if item['id']==e['id'])
                if e['state']==state:return e
                if e['state'] in ['failed','lost','expired']:raise AssertionError('notebook '+e['state']+': '+str(e.get('error')))
                time.sleep(.15)
            raise TimeoutError('kernel readiness')
        def start(self,environment=None,epoch=None):
            request=dict(key=str(uuid.uuid4()),target=self.target,environment=environment,epoch=epoch)
            e=self.action('create',**request);self.created[e['id']]=request
            e=self.action('start',id=e['id'],generation=0,key=str(uuid.uuid4()))
            return self.wait(e)
        def stop(self,e):
            return self.wait(self.action('shutdown',id=e['id'],generation=e['generation'],key=str(uuid.uuid4())),'stopped')
        def restart(self,e,environment=None):
            fields={} if environment is None else {'environment':environment}
            return self.wait(self.action('restart' if environment is None else 'adopt_environment',id=e['id'],generation=e['generation'],key=str(uuid.uuid4()),**fields))
        def connect(self,e):
            ticket=self.request('notebooks/ticket',{'id':e['id'],'generation':e['generation']})
            ws=websocket.create_connection(self.origin.replace('http:','ws:')+f"/api/notebooks/{e['id']}/{e['generation']}/channels",origin=self.origin,
                cookie='; '.join(c.name+'='+c.value for c in self.jar),subprotocols=[ticket['protocol'],ticket['authorization_protocol']],timeout=30)
            channels.append(ws);return ws
    def execute(ws,code):
        mid=str(uuid.uuid4());parts=[b'shell',json.dumps({'msg_id':mid,'username':'qualification','session':str(uuid.uuid4()),'msg_type':'execute_request','version':'5.3'}).encode(),b'{}',b'{}',json.dumps(dict(code=code,silent=False,store_history=True,user_expressions={},allow_stdin=False,stop_on_error=True)).encode()]
        offsets=[8*(len(parts)+2)]
        for p in parts:offsets.append(offsets[-1]+len(p))
        ws.send_binary(struct.pack('<'+'Q'*(len(offsets)+1),len(offsets),*offsets)+b''.join(parts))
        reply=None;idle=False;outputs=[]
        while not (reply is not None and idle):
            raw=ws.recv();assert isinstance(raw,bytes)
            count=struct.unpack_from('<Q',raw)[0];assert count<=16
            offsets=struct.unpack_from('<'+'Q'*count,raw,8)
            parts=[raw[a:b] for a,b in zip(offsets,offsets[1:])]
            header,parent,content=(json.loads(parts[i]) for i in [1,2,4])
            if parent.get('msg_id')!=mid:continue
            if header['msg_type']=='execute_reply':reply=content
            elif header['msg_type']=='status' and content['execution_state']=='idle':idle=True
            elif header['msg_type']=='stream':outputs.append(content['text'])
        assert reply['status']=='ok',reply
        return ''.join(outputs)
    def records(table):
        assert table in ['environment_generations','environment_leases']
        with sqlite3.connect(f'file:{data}/state.sqlite3?mode=ro',uri=True) as db:
            return list(db.execute('SELECT * FROM '+table))
    def prepare(project,label):
        destination=project/'notebooks/environment';destination.mkdir(parents=True,exist_ok=True)
        for name in ['pyproject.toml','uv.lock']:shutil.copy2(release/'python/notebooks/environments'/label/name,destination/name)
        cli(project,'env','prepare','--wait')
        return cli(project,'env','status')['active_generation']
    try:
        for name in ['a','b']:
            p=root/name;p.mkdir();projects.append(p);cli(p,'init','ne03-'+name)
        cli(projects[0],'up')
        for p in projects:
            cli(p,'database','create','main','--wait');cli(p,'sql','--branch','main','--write','--sql','CREATE TABLE public.orders(id int)');cli(p,'sql','--branch','main','--write','--sql','INSERT INTO public.orders VALUES(1)')
        a=Console(projects[0]);b=Console(projects[1])
        # Old documents/default start need no manual env initialization.
        first=a.start();ws=a.connect(first)
        assert first['environment'] and execute(ws,"import sys,site\nassert sys.prefix!=sys.base_prefix and not site.ENABLE_USER_SITE\nassert spark.table('public.orders').count()==1\nassert supabricks_environment['id']=="+repr(first['environment']['id'])+"\nprint('ISOLATED')").strip()=='ISOLATED'
        ws.close();a.stop(first);check('default_console_start_creates_offline_isolated_kernel')
        old_id=prepare(projects[0],'fixture-a');other_id=prepare(projects[1],'fixture-b')
        old=a.start(old_id);other=b.start(other_id);old_ws=a.connect(old);other_ws=b.connect(other)
        execute(old_ws,"import humanize,xxhash\nassert humanize.__version__=='4.13.0'\nassert xxhash.xxh32('x').intdigest()>0\ncounter=7")
        execute(other_ws,"import humanize\nassert humanize.__version__=='4.14.0'\nassert spark.table('public.orders').count()==1")
        assert len(records('environment_leases'))==2
        check('two_projects_run_different_pure_and_native_packages_concurrently')
        other_ws.close();b.stop(other)
        new_id=prepare(projects[0],'fixture-b')
        # The old lease keeps its materialization alive while new kernels use new dependencies.
        cli(projects[0],'env','gc');assert any(r[0]==old_id for r in records('environment_generations') if r[3]=='ready')
        old=a.action('status',id=old['id'],generation=old['generation']);assert old['environment_preparation_needed']
        fresh=a.start(new_id,old['epoch_id']);fresh_ws=a.connect(fresh)
        execute(old_ws,"assert humanize.__version__=='4.13.0' and counter==7")
        execute(fresh_ws,"import humanize\nassert humanize.__version__=='4.14.0'")
        check('same_project_old_and_new_kernels_keep_leased_environments_and_snapshot')
        old_ws.close();restarted=a.restart(old);old_ws=a.connect(restarted)
        assert restarted['environment']==old['environment'] and restarted['epoch_id']==old['epoch_id']
        execute(old_ws,"import humanize\nassert humanize.__version__=='4.13.0'\nassert 'counter' not in globals()")
        old_ws.close();adopted=a.restart(restarted,new_id);old_ws=a.connect(adopted)
        assert adopted['environment']['id']==new_id and adopted['epoch_id']==old['epoch_id']
        assert a.action('create',**a.created[old['id']])['environment']==adopted['environment']
        execute(old_ws,"import humanize\nassert humanize.__version__=='4.14.0'\nassert 'counter' not in globals()")
        check('restart_retains_both_identities_and_explicit_adoption_retains_epoch_without_replay')
        def provenance(e):return {k:e[k] for k in ['branch_id','epoch_id','environment']}
        document=dict(nbformat=4,nbformat_minor=5,metadata={'supabricks':{'binding':provenance(adopted)}},cells=[dict(id=str(i),cell_type='code',metadata={'supabricks_outputs':provenance(e)},source='print(1)',execution_count=1,outputs=[dict(output_type='stream',name='stdout',text='1\n')]) for i,e in enumerate([old,adopted])])
        a.request('notebooks/contents',dict(action='save',path='mixed.ipynb',document=document,expected_revision=None))
        assert a.request('notebooks/contents',dict(action='get',path='mixed.ipynb'))['value']['document']==document
        check('mixed_environment_output_provenance_survives_save_and_reopen')
        old_ws.close();a.stop(adopted);fresh_ws.close();a.stop(fresh)
        cli(projects[0],'sql','--branch','main','--write','--sql','INSERT INTO public.orders VALUES(2)')
        cli(projects[0],'analytics','refresh','--branch','main','--wait')
        latest=a.start(new_id);latest_ws=a.connect(latest)
        assert latest['epoch_id']!=adopted['epoch_id'] and latest['environment']==adopted['environment']
        execute(latest_ws,"assert spark.table('public.orders').count()==2")
        latest_ws.close();a.stop(latest)
        check('latest_snapshot_selection_retains_environment_and_observes_new_data')
        assert len(records('environment_leases'))==0
        # Drift must fail before admission/launch, leaving no lease behind.
        generation=next(json.loads(r[4]) for r in records('environment_generations') if r[0]==new_id)
        marker=Path(generation['path'])/'drift.py';marker.write_text('changed')
        e=a.action('create',key=str(uuid.uuid4()),target=a.target,environment=new_id)
        e=a.action('start',id=e['id'],generation=0,key=str(uuid.uuid4()))
        deadline=time.monotonic()+20
        while e['state'] in ['starting','stopping'] and time.monotonic()<deadline:
            time.sleep(.1);e=a.action('status',id=e['id'],generation=e['generation'])
        assert e['state']=='lost' and 'drifted' in e['error'];assert len(records('environment_leases'))==0
        marker.unlink();check('drifted_environment_is_rejected_before_python_and_releases_all_leases')
        service=subprocess.check_output([str(release/'python/runtime/bin/python3.12'),'-I','-B','-c',"import importlib.util;assert importlib.util.find_spec('humanize') is None;import jupyter_server,pysail"],text=True)
        check('service_runtime_dependencies_remain_unchanged')
        live=a.start(new_id);live_ws=a.connect(live);execute(live_ws,'counter=99')
        assert len(records('environment_leases'))==1
        daemons=[]
        for process in psutil.process_iter(['cmdline','uids']):
            argv=process.info['cmdline'] or []
            if 'daemon' in argv and str(data) in argv and process.info['uids'].effective==os.getuid():daemons.append(process)
        assert len(daemons)==1
        daemons[0].kill()
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            try:
                if not daemons[0].is_running() or daemons[0].status()==psutil.STATUS_ZOMBIE:break
            except psutil.NoSuchProcess:break
            time.sleep(.05)
        else:raise TimeoutError('owned daemon did not stop')
        cli(projects[0],'up')
        assert len(records('environment_leases'))==0
        a=Console(projects[0]);assert a.action('list')==[]
        recovered=a.start(new_id);recovered_ws=a.connect(recovered)
        execute(recovered_ws,"assert 'counter' not in globals()\nassert spark.table('public.orders').count()==2")
        recovered_ws.close();a.stop(recovered)
        check('daemon_crash_reaps_kernels_before_releasing_leases_and_never_replays_cells')
        report['status']='passed'
    finally:
        for ws in channels:ws.close()
        if projects:
            try:cli(projects[0],'down')
            except Exception:report['cleanup_failed']=True;report['status']='failed'
        if report['status']!='passed':report['status']='failed'
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({'status':report['status'],'private_workspace':str(root)}))
        if report['status']=='passed':shutil.rmtree(root)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--release',type=Path,required=True);parser.add_argument('--report',type=Path,required=True);main(parser.parse_args())
