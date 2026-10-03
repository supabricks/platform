#!/usr/bin/env python3
"""Matched direct/owner read cost with the installed SQLite backend in both arms.

The writer lives in a child in both arms. Only SP10b reads through its private
owner thread. Opaque records isolate bounded journal reads from PostgreSQL/Delta;
these are component costs, never a pipeline capacity claim.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid


def summary(values):
    return dict(median=statistics.median(values),minimum=min(values),maximum=max(values),individual=values)


def child(analytics,root):
    sys.path.insert(0,str(analytics))
    from capture.spool import Spool
    control=next((root/'capture').glob('*/control.json'));config=json.loads(control.read_text())
    spool=Spool(control.parent/'spool',config['identity']);service=None
    try:
        spool.establish(100,{});spool.set('bootstrap',{'lsn':'0/64'})
        end=100
        for _ in range(128):
            group=[]
            for _ in range(32):group.append((end+1,end+2,b'v'*1024));end+=2
            spool.append_many(group)
        spool.verify()
        if (analytics/'capture/owner.py').exists():
            from capture.owner import Owner
            service=Owner(control,spool.backend)
        print(json.dumps(dict(status='ready',mode='owner' if service else 'direct',captured=end)),flush=True)
        for line in sys.stdin:
            command=json.loads(line)
            if command=='begin':start=time.process_time();print('{}',flush=True)
            elif command=='end':print(json.dumps(dict(cpu_seconds=time.process_time()-start)),flush=True)
            elif command=='append':
                spool.append(end+1,end+2,b'tail');spool.verify();print('{}',flush=True)
            elif command=='stop':break
    finally:
        if service:service.close()
        spool.close()


def run(args):
    sys.path.insert(0,str(args.analytics))
    from capture.spool import atomic,pg_lsn
    from incremental.storage import journal_attempt
    mode='owner' if (args.analytics/'capture/owner.py').exists() else 'direct'
    if mode=='owner':
        from capture.owner import read_range,request_for
    with tempfile.TemporaryDirectory(prefix='sb-read-',dir=args.scratch) as temp:
        root=Path(temp);capture=str(uuid.uuid4());run_id=str(uuid.uuid4())
        for name in ('tmp','capture',f'capture/{capture}','analytics','analytics/apply-work',f'analytics/apply-work/{run_id}'):
            (root/name).mkdir(mode=0o700)
        identity=dict(project_id='component',generation=capture,decoder_version=1)
        access=dict(endpoint=str(root/'tmp'/capture/'journal.sock'),policy_revision=1,source_revision=1)
        control=root/'capture'/capture/'control.json'
        atomic(control,dict(identity=identity,worker_generation=1,desired='running',journal_access=access))
        config=dict(id=run_id,attempt=1,epoch_id=str(uuid.uuid4()),identity=identity,worker_generation=1,
            bootstrap_lsn='0/64',after_lsn='0/64',target_lsn=pg_lsn(100+4096*2),deadline_ms=time.time()*1000+120000,
            journal_access=access,spool=str(control.parent/'spool/spool.sqlite3'))
        input_path=root/'analytics/apply-work'/run_id/'input.json';atomic(input_path,config)
        process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--analytics',str(args.analytics),'--child',str(root)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        def command(value):
            process.stdin.write(json.dumps(value)+'\n');process.stdin.flush()
            line=process.stdout.readline();assert line and process.poll() is None,'owner exited'
            return json.loads(line)
        def read(stats):
            deadline=time.monotonic()+3
            return read_range(request_for(config),deadline,stats) if mode=='owner' else journal_attempt(config,deadline)
        cells=[]
        try:
            ready=json.loads(process.stdout.readline());assert ready['mode']==mode and ready['captured']==8292
            for count in (32,512,4096):
                config['target_lsn']=pg_lsn(100+count*2);atomic(input_path,config)
                for _ in range(3):read({})
                samples=[];metrics=[];command('begin');cpu=time.process_time()
                for _ in range(16):
                    stats={};started=time.perf_counter();result=read(stats)
                    samples.append((time.perf_counter()-started)*1000);metrics.append(stats)
                    assert result[0]=={} and result[2]==100+count*2 and result[3]==count*1024
                    assert result[1]==[(100+(i+1)*2,b'v'*1024) for i in range(count)]
                client_cpu=time.process_time()-cpu;owner_cpu=command('end')['cpu_seconds']
                cells.append(dict(records=count,payload_bytes=count*1024,requests=16,roundtrip_ms=summary(samples),
                    client_cpu_seconds=client_cpu,owner_cpu_seconds=owner_cpu,total_cpu_seconds=client_cpu+owner_cpu,
                    transport={key:summary([m[key] for m in metrics]) for key in metrics[0]}))
            # New durable data must not move the already issued target.
            command('append');assert read({})[2]==8292
            command('begin');command('end')
            process.stdin.write('"stop"\n');process.stdin.flush();process.wait(timeout=5)
            assert process.returncode==0
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=5)
            process.stdin.close();process.stdout.close()
        report=dict(status='PASS',mode=mode,cells=cells,
            checks=['separate_writer_process','identical_durable_records','all_bounded_ranges_exact','target_pinned_across_append','owner_closed'],
            source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            scope='Installed SQLite read component; three sizes and 16 correlated requests per fresh fixture. CPU includes client validation; no pipeline throughput claim.')
    args.report.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--analytics',type=Path,required=True)
    p.add_argument('--scratch',type=Path);p.add_argument('--report',type=Path);p.add_argument('--child',type=Path)
    a=p.parse_args()
    if a.child:child(a.analytics,a.child)
    else:run(a)
