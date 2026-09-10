#!/usr/bin/env python3
"""Real I01 service qualification in a verified disposable root; synthetic data only."""
import http.client
import socket
import tomllib
from urllib.parse import urlsplit
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import psutil
import psycopg
import subprocess
import tempfile
import time


def qualify(args):
    workspace=Path(tempfile.mkdtemp(prefix='sb-i01-',dir='/tmp')).resolve()
    data=workspace/'data';project=workspace/"app ' with spaces";project.mkdir()
    binary=str(args.binary.resolve())
    env=dict(os.environ)
    for name in list(env):
        if name.upper().endswith('_PROXY') or name.startswith(('PG','AWS_','PC_','OTEL_','SUPABRICKS_')): env.pop(name)
    pressure_dir=os.environ.get('SUPABRICKS_INGEST_PRESSURE_DIR')
    checks=[]
    paused=[]
    report=dict(status='failed',checks=checks,workspace=str(workspace),network_evidence=os.environ.get('SUPABRICKS_INGEST_NETWORK_EVIDENCE','local source run; no network isolation'))
    def cli(*argv,ok=True):
        p=subprocess.run([binary,*map(str,argv),'--data-dir',str(data),'--project',str(project)],env=env,capture_output=True,text=True,timeout=700)
        value=json.loads(p.stdout) if p.stdout.strip() else None
        if ok and p.returncode: raise AssertionError('command failed: '+str(argv[:2])+' '+json.dumps(value or p.stderr[-1500:]))
        if not ok: assert p.returncode!=0,'command unexpectedly succeeded'
        return value
    def check(name): checks.append(name);print('[I01] '+name,flush=True)
    def wait(fn, seconds=180):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            value=fn()
            if value:return value
            time.sleep(0.005)
        raise AssertionError('condition timed out')
    def terminal(id):
        def poll():
            j=cli('ingest','status',id)
            return j if j['state'] in ('succeeded','failed','cancelled') else None
        return wait(poll,650)
    def sql(statement):return cli('sql','--branch','main','--sql',statement)['rows']
    def owned(role):
        assert data.parent==workspace and workspace.name.startswith('sb-i01-')
        if role=='daemon':
            matches=[p for p in psutil.process_iter(['cmdline']) if 'daemon' in (p.info['cmdline'] or []) and str(data) in (p.info['cmdline'] or [])]
            assert len(matches)==1
            proc=matches[0]
        else:
            with sqlite3.connect(f'file:{data}/state.sqlite3?mode=ro',uri=True) as db:
                row=db.execute('SELECT record_json FROM native_processes WHERE role=?',(role,)).fetchone()
            if not row:return None
            record=json.loads(row[0]);proc=psutil.Process(record['pid'])
        assert proc.uids().effective==os.geteuid() and str(data) in ' '.join(proc.cmdline())
        return proc
    def mapping_for(path, **kwargs):
        result=cli('ingest','inspect',path,*kwargs.get('options',[]))
        dest=workspace/(path.name+'.json');dest.write_text(json.dumps(result['inspection']['mapping']))
        return result['source']['id'],dest
    def load(source,mapping,table,waited=True,ok=True):
        return cli('ingest','load','--source',source,'--schema-file',mapping,'--branch','main','--table',table,'--key',table,*(['--wait'] if waited else []),ok=ok)
    try:
        if not args.bundle:
            verified=subprocess.run([binary,'installation','verify'],capture_output=True,text=True,env=env,check=True,timeout=90)
            report['release']=json.loads(verified.stdout)
        cli('init','ingestion')
        extra=['--bundle',args.bundle,'--helpers',args.helpers] if args.bundle else []
        cli('up',*extra)
        if args.python:cli('analytics','configure','--python',args.python,'--worker',args.worker)
        cli('database','create','main','--wait')
        source=workspace/'quoted.csv'
        source.write_text('id,zip,note,amount\n9007199254740993,001,"line\nnext",12345678901234567890.1234567890\n2,002,"",\n')
        inspected=cli('ingest','inspect',source,'--null-strings','[""]')
        assert inspected['source']['state']=='staged',inspected
        mapping=inspected['inspection']['mapping']
        mapping['columns'][0].update(data_type=dict(kind='bigint'),nullable=False)
        mapping['columns'][2]['name']='odd" column'
        mapping['columns'][3]['data_type']=dict(kind='decimal',precision=30,scale=10)
        approved=workspace/'approved.json';approved.write_text(json.dumps(mapping))
        job=cli('ingest','load','--source',inspected['source']['id'],'--branch','main','--table','imported','--schema-file',approved,'--key','first','--wait')
        assert job['state']=='succeeded' and job['committed_rows']==2,job
        rows=cli('sql','--branch','main','--sql','SELECT id,zip,"odd"" column",amount FROM imported ORDER BY id')['rows']
        assert rows==[['2','002','',None],['9007199254740993','001','line\nnext','12345678901234567890.1234567890']],rows
        check('approved CSV mapping preserves bigint, decimal, leading zeros, multiline values, quoted identifiers and empty/null')
        assert cli('ingest','status',job['id'])['id']==job['id']
        assert any(j['id']==job['id'] for j in cli('ingest','list','--branch','main'))
        repeated=cli('ingest','load','--source',inspected['source']['id'],'--branch','main','--table','imported','--schema-file',approved,'--key','first','--wait')
        assert repeated['id']==job['id']
        cli('ingest','load','--source',inspected['source']['id'],'--branch','main','--table','different','--schema-file',approved,'--key','first',ok=False)
        check('durable status/list and same-key replay return the original job; changed input conflicts')
        cli('branch','create','child','--from','main','--wait')
        cli('sql','--branch','child','--sql','UPDATE imported SET zip=\'changed\'','--write')
        assert cli('sql','--branch','main','--sql','SELECT zip FROM imported ORDER BY id')['rows']==[['002'],['001']]
        cli('sql','--branch','main','--sql','ALTER TABLE imported RENAME COLUMN "odd"" column" TO note','--write')
        cli('analytics','refresh','--branch','main','--wait')
        assert cli('analytics','sql','--branch','main','--sql','SELECT count(*) FROM public.imported')['rows']==[['2']]
        check('imported table participates in physical branching and real analytical export with receipt schema excluded')
        tsv=workspace/'headerless.tsv';tsv.write_text('001\t"line\nnext"\n002\t""\n')
        sid,mp=mapping_for(tsv,options=['--no-header'])
        tsv.unlink()  # staged source is independent of the user's device file
        assert load(sid,mp,'tsv_rows')['committed_rows']==2
        assert sql('SELECT * FROM tsv_rows ORDER BY column_1')==[['001','line\nnext'],['002','']]
        check('headerless TSV survives source removal with exact multiline and empty values')
        device=workspace/'device.csv';device.write_text('code\n001\n')
        sid,mp=mapping_for(device)
        file_args=['ingest','load',device,'--schema-file',mp,'--branch','main','--table','device_rows','--key','device-key','--wait']
        first=cli(*file_args)
        assert cli(*file_args)['id']==first['id']
        device.write_text('code\n002\n');cli(*file_args,ok=False)
        cli('ingest','dispose',sid)
        assert sql('SELECT * FROM device_rows')==[['001']]
        check('FILE command replay rehashes the device file and conflicts on changed bytes')

        bad=workspace/'late.csv';bad.write_text('id,payload\n'+('1,valid\n'*160000)+'2,too,many\n')
        sid,mp=mapping_for(bad)
        failed=load(sid,mp,'malformed_late',ok=False)
        assert failed['state']=='failed' and failed['retryable'] and failed['copied_rows']>0,failed
        assert sql("SELECT to_regclass('malformed_late')")==[[None]]
        check('malformed CSV after a valid preview and partial COPY rolls back the entire new table')
        cli('ingest','dispose',sid)
        bad.write_text('amount\n1.00\n1000.00\n')
        sid,mp=mapping_for(bad);m=json.loads(mp.read_text());m['columns'][0]['data_type']=dict(kind='decimal',precision=5,scale=2);mp.write_text(json.dumps(m))
        failed=load(sid,mp,'decimal_overflow',ok=False)
        assert failed['state']=='failed' and sql("SELECT to_regclass('decimal_overflow')")==[[None]]
        cli('ingest','dispose',sid)
        check('decimal overflow fails atomically without rounding or a partial table')
        small=workspace/'small.csv';small.write_text('value\n001\n')
        sid,mp=mapping_for(small)
        failed=load(sid,mp,'imported',ok=False)
        assert failed['state']=='failed' and not failed['retryable'] and failed['error']=='target_exists',failed
        assert sql('SELECT count(*) FROM imported')==[['2']]
        cli('ingest','dispose',sid)
        check('existing destination is preserved and rejected without leaving a pending commit')
        oversize=workspace/'oversize.csv'
        with oversize.open('wb') as out:out.truncate(100*1024**2+1)
        rejected=cli('ingest','inspect',oversize,ok=False)
        assert rejected['error']=='source_limit',rejected
        oversize.unlink()
        check('sources above the 100 MiB admission limit are rejected')

        # Exact source limit, generated in small chunks without inflating harness RSS.
        large=workspace/'limit.csv';header=b'id,payload\n';row=b'1,'+b'x'*1021+b'\n'
        size=100*1024**2
        count,remainder=divmod(size-len(header),len(row))
        with large.open('wb') as out:
            out.write(header)
            for _ in range(count):out.write(row)
            if remainder:out.write(b'1,'+b'x'*(remainder-3)+b'\n')
        count+=bool(remainder)
        assert large.stat().st_size==size
        sid,mp=mapping_for(large)
        started=load(sid,mp,'killed_copy',waited=False)
        def copying():
            progress=data/'tmp'/('ingest-'+started['id'])/'progress.json'
            if not progress.exists():return None
            value=json.loads(progress.read_text())
            return owned('ingest-'+started['id']) if value.get('copied_rows',0)>=1024 else None
        proc=wait(copying);proc.send_signal(signal.SIGSTOP);paused.append(proc)
        cli('branch','suspend','main',ok=False)
        cli('branch','delete','main','--force',ok=False)
        # Only one active load can consume the cell's ingestion budget.
        load(sid,mp,'concurrent_copy',waited=False,ok=False)
        assert sql("SELECT to_regclass('killed_copy')")==[[None]]
        cli('branch','create','during-copy','--from','main','--wait')
        assert cli('sql','--branch','during-copy','--sql',"SELECT to_regclass('killed_copy')")['rows']==[[None]]
        cli('analytics','refresh','--branch','main','--wait')
        assert cli('analytics','sql','--branch','main','--sql','SELECT count(*) FROM public.imported')['rows']==[['2']]
        check('physical fork and analytical export during an uncommitted COPY expose a consistent committed snapshot')
        proc.kill();paused.remove(proc)
        failed=terminal(started['id'])
        assert failed['state']=='failed' and failed['retryable'] and sql("SELECT to_regclass('killed_copy')")==[[None]],failed
        check('worker death during COPY proves rollback; suspend/delete and concurrent import are rejected while owned')
        completed=cli('ingest','retry',started['id'],'--wait')
        assert completed['state']=='succeeded' and completed['attempt']==2 and completed['committed_rows']==count,completed
        assert sql('SELECT count(*) FROM killed_copy')==[[str(count)]]
        metrics=completed['metrics'];assert 0<metrics['peak_rss_bytes']<=512*1024**2 and metrics['decoded_bytes']<=512*1024**2,metrics
        report['qualification']=dict(source_bytes=size,rows=count,**metrics)
        check('explicit retry uses the same job exactly once; full 100 MiB source stays within sampled 512 MiB RSS and decoded limits')

        sid,mp=mapping_for(large)
        started=load(sid,mp,'committed_before_record',waited=False)
        proc=wait(copying)
        connection=cli('connect','main')['uri']
        worker_input=json.loads((data/'tmp'/('ingest-'+started['id'])/'input.json').read_text())
        observer=psycopg.connect(connection,port=worker_input['connection']['port'],connect_timeout=5,autocommit=True)
        daemon=owned('daemon');daemon.send_signal(signal.SIGSTOP);paused.append(daemon)
        def committed():
            return observer.execute('SELECT committed_rows FROM _supabricks.ingest_receipts WHERE job_id=%s',(started['id'],)).fetchone()
        assert wait(committed,180)==(count,)
        with sqlite3.connect(f'file:{data}/state.sqlite3?mode=ro',uri=True) as db:
            assert db.execute('SELECT state,committed_rows FROM ingest_jobs WHERE id=?',(started['id'],)).fetchone()==('loading',None)
        observer.close()
        daemon.kill();paused.remove(daemon)
        cli('up')
        recovered=terminal(started['id'])
        assert recovered['state']=='succeeded' and recovered['attempt']==1 and recovered['committed_rows']==count,recovered
        assert sql('SELECT count(*) FROM committed_before_record')==[[str(count)]]
        check('daemon death after PostgreSQL commit but before SQLite success reconciles receipt without replay')

        # Real MCP stdio reads the identical project-bound durable job.
        requests=[dict(jsonrpc='2.0',id=1,method='initialize',params=dict(protocolVersion='2025-06-18',clientInfo=dict(name='ingest-qualification',version='1'),capabilities={})),
                  dict(jsonrpc='2.0',method='notifications/initialized'),
                  dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(name='ingest_status',arguments=dict(id=recovered['id']))),
                  dict(jsonrpc='2.0',id=3,method='tools/call',params=dict(name='ingest_list',arguments=dict(branch='main')))]
        mcp=subprocess.run([binary,'mcp','--data-dir',str(data),'--project',str(project)],input=''.join(json.dumps(r)+'\n' for r in requests),capture_output=True,text=True,env=env,timeout=30)
        responses=[json.loads(line) for line in mcp.stdout.splitlines()]
        value=next(r for r in responses if r['id']==2)['result']['structuredContent']
        assert value['id']==recovered['id'] and value['state']=='succeeded' and 'worker' not in value
        assert any(j['id']==recovered['id'] for j in next(r for r in responses if r['id']==3)['result']['structuredContent']['jobs'])
        check('MCP status/list and CLI share the same durable receipt-backed job without exposing process credentials')

        sid,mp=mapping_for(large);started=load(sid,mp,'cancelled_copy',waited=False);wait(copying)
        cancelled=cli('ingest','cancel',started['id'],'--wait')
        assert cancelled['state']=='cancelled' and sql("SELECT to_regclass('cancelled_copy')")==[[None]],cancelled
        check('cancellation fences COPY before reporting rollback and releasing staged data')
        sid,mp=mapping_for(large);started=load(sid,mp,'shutdown_copy',waited=False);wait(copying)
        cli('down');cli('up')
        stopped=terminal(started['id'])
        assert stopped['state']=='cancelled' and sql("SELECT to_regclass('shutdown_copy')")==[[None]],stopped
        check('daemon shutdown fences and resolves active COPY before stopping PostgreSQL')
        sid,mp=mapping_for(large)
        started=cli('ingest','load','--source',sid,'--schema-file',mp,'--branch','child','--table','ttl_copy','--key','ttl-copy')
        proc=wait(copying);proc.send_signal(signal.SIGSTOP);paused.append(proc)
        cli('branch','ttl','child','--expires-at-ms',str(int(time.time()*1000)+1000),'--wait')
        time.sleep(2)
        assert cli('ingest','status',started['id'])['state']=='loading'
        cli('connect','child',ok=False)
        proc.send_signal(signal.SIGCONT);paused.remove(proc)
        expired=terminal(started['id'])
        assert expired['state']=='succeeded' and expired['committed_rows']==count,expired
        check('TTL rejects new application access while owned COPY and receipt recovery complete before deletion')
        if pressure_dir:
            pressure=Path(tempfile.mkdtemp(prefix='sb-i01-pressure-',dir=pressure_dir))
            pressure_data=pressure/'data';pressure_project=workspace/'pressure-project';pressure_project.mkdir()
            def pressure_cli(*argv,ok=True):
                out=subprocess.run([binary,*map(str,argv),'--data-dir',str(pressure_data),'--project',str(pressure_project)],capture_output=True,text=True,env=env,timeout=60)
                value=json.loads(out.stdout) if out.stdout.strip() else None
                assert (out.returncode==0)==ok,(argv,value,out.stderr[-500:])
                return value
            pressure_cli('init','pressure')
            daemon=subprocess.Popen([binary,'daemon','--data-dir',str(pressure_data)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                wait(lambda:(pressure_data/'control.sock').exists(),30)
                if args.python:pressure_cli('analytics','configure','--python',args.python,'--worker',args.worker)
                # Open the real authenticated bridge against this metadata-only
                # cell, before disk pressure. No native compute is needed to upload.
                config=tomllib.loads((pressure_project/'supabricks.toml').read_text())
                def control(request):
                    with socket.socket(socket.AF_UNIX) as sock:
                        sock.settimeout(5);sock.connect(str(pressure_data/'control.sock'))
                        sock.sendall(json.dumps(dict(version=1,request=request)).encode()+b'\n')
                        value=json.loads(sock.makefile('rb').readline())
                        assert 'error' not in value,value
                        return value['result']
                assets=Path(binary).parent.parent/'share/console'
                if not assets.is_dir():assets=Path(__file__).resolve().parents[2]/'console/dist'
                def open_console():
                    value=control(dict(method='console_open',binding=dict(project_id=config['id'],worktree=str(pressure_project)),assets=str(assets)))
                    return value if value['state']=='ready' else None
                console=wait(open_console,20)
                url=urlsplit(console['url']);origin=f'http://127.0.0.1:{url.port}'
                headers={'Origin':origin,'X-Supabricks-Console':'1','Content-Type':'application/json'}
                def browser_http(path,payload):
                    conn=http.client.HTTPConnection('127.0.0.1',url.port,timeout=10)
                    conn.request('POST',path,body=json.dumps(payload),headers=headers)
                    response=conn.getresponse();value=json.loads(response.read());cookie=response.getheader('Set-Cookie');code=response.status;conn.close()
                    return code,value,cookie
                code,session,cookie=browser_http('/api/session',dict(token=url.fragment.removeprefix('launch=')))
                assert code==200,session
                headers.update({'Cookie':cookie.split(';')[0],'X-Supabricks-CSRF':session['csrf']})
                def browser(command):return browser_http('/api/workspace',dict(action='ingest',command=command))[:2]
                disk=os.statvfs(pressure)
                free=disk.f_bavail*disk.f_frsize
                assert 8*1024**2<free<160*1024**2,'pressure fixture must be a separate bounded volume'
                with (pressure/'synthetic-fill').open('wb') as out:
                    for _ in range((free-8*1024**2)//1048576):out.write(b'x'*1048576)
                    out.flush();os.fsync(out.fileno())
                code,rejection=browser(dict(action='begin',name='browser.csv',bytes=100))
                assert code==409 and 'disk_reserve' in rejection['error']['message'],rejection
                rejected=pressure_cli('ingest','inspect',small,ok=False)
                assert rejected['source']['state']=='interrupted' and rejected['error']=='disk_reserve',rejected
                assert not list((pressure_data/'ingest/sources').glob('*.source'))
                (pressure/'synthetic-fill').unlink()
                pressure_cli('ingest','dispose',rejected['source']['id'])
                recovered=pressure_cli('ingest','inspect',small)
                assert recovered['source']['state']=='staged',recovered
                check('real bounded volume pressure refuses staging before partial publication and recovers after space is freed')
                code,slot=browser(dict(action='begin',name='expired.csv',bytes=8));assert code==200,slot
                source=slot['value']['source']['id']
                # Test-only expiry injection in an isolated catalog. Production
                # continues to have exactly one metadata writer.
                with closing(sqlite3.connect(pressure_data/'state.sqlite3')) as db:
                    with db:
                        db.execute('UPDATE ingest_sources SET expires_at_ms=0 WHERE id=?',(source,))
                code,rejection=browser(dict(action='source',source=source))
                assert code==409 and 'expired' in rejection['error']['message'],rejection
                check('real browser upload admission rejects disk exhaustion and expired staging on the bounded volume')

            finally:
                (pressure/'synthetic-fill').unlink(missing_ok=True)
                pressure_cli('down');daemon.wait(timeout=30)
                shutil.rmtree(pressure)
        report['status']='passed'
    except BaseException as error:
        report['error']=str(error)
        try:
            report['daemon_status']=cli('status')
            report['worker_outcomes']={str(p.relative_to(data)): {k:v for k,v in json.loads(p.read_text()).items() if k!='value'} for p in (data/'tmp').glob('ingest-*/result.json')}
            with sqlite3.connect(f'file:{data}/state.sqlite3?mode=ro',uri=True) as db:report['generation']=db.execute('SELECT generation FROM owner').fetchone()[0]
        except BaseException:pass
        raise
    finally:
        for proc in paused:
            try:proc.send_signal(signal.SIGCONT)
            except psutil.NoSuchProcess:pass
        try: cli('down')
        except BaseException: report['cleanup_failed']=True
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='passed' and not report.get('cleanup_failed'):shutil.rmtree(workspace)
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary',type=Path,required=True)
    for name in ['bundle','helpers','python','worker']:p.add_argument('--'+name,type=Path)
    p.add_argument('--report',type=Path,required=True)
    print(json.dumps(qualify(p.parse_args()),indent=2))
