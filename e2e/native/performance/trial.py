#!/usr/bin/env python3
"""One disposable installed sync scaling trial. No release gates are changed."""
import argparse
import bisect
from contextlib import closing
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from installed_sync import InstalledContinuous,sha
from continuous import percentiles,host_counters,host_delta
from cell import lsn


def counters():
    root=Path('/sys/fs/cgroup');out={}
    for name in ('cpu.stat','memory.events','io.stat','cpu.pressure','io.pressure','memory.pressure'):
        p=root/name
        if p.exists():out[name]=p.read_text()
    for name in ('cpu.max','cpuset.cpus.effective','memory.max','memory.swap.max','memory.current','memory.peak'):
        p=root/name
        if p.exists():out[name]=p.read_text().strip()
    return out


def cpu_delta(before,after,elapsed):
    a={k:int(v) for k,v in (line.split() for line in before['cpu.stat'].splitlines())}
    b={k:int(v) for k,v in (line.split() for line in after['cpu.stat'].splitlines())}
    d={k:b[k]-v for k,v in a.items()}
    d['average_cpu_cores']=round(d['usage_usec']/1e6/elapsed,3)
    return d


def load(cell,rate,seconds,clients,rows,offset=0):
    """Paced independent connections; report unsent work instead of hiding saturation."""
    start=time.perf_counter()+.25;total=int(rate*seconds/2)
    samples=[];errors=[];lock=threading.Lock()
    def writer(client):
        local=[]
        try:
            with cell.source() as db:
                db.execute("SET statement_timeout='10s'")
                for index in range(client,total,clients):
                    due=start+index*2/rate
                    delay=due-time.perf_counter()
                    if delay>0:time.sleep(delay)
                    if time.perf_counter()>=start+seconds:break
                    # Clients own disjoint keys, so source lock contention cannot
                    # masquerade as sync capacity. Both tables change atomically.
                    key=1+client+clients*((index//clients)%(rows//clients))
                    value=offset+index+1
                    submitted=time.perf_counter()
                    result=cell.transaction(db,key,value)
                    result['late_ms']=max(0,(submitted-due)*1000)
                    local.append(result)
        except Exception as error:errors.append(type(error).__name__)
        with lock:samples.extend(local)
    threads=[threading.Thread(target=writer,args=(client,)) for client in range(clients)]
    for t in threads:t.start()
    for t in threads:t.join()
    elapsed=time.perf_counter()-start
    if errors:raise RuntimeError('source writer failures: '+','.join(errors))
    return sorted(samples,key=lambda x:x['ack_ms']),dict(target_rows_per_second=rate,
        target_transactions=total,completed_transactions=len(samples),unsent_transactions=total-len(samples),
        achieved_rows_per_second=round(2*len(samples)/elapsed,3),elapsed_seconds=round(elapsed,3),
        transaction_ms=percentiles([s['latency_ms'] for s in samples]),
        submission_lateness_ms=percentiles([s['late_ms'] for s in samples]))


class Observer:
    def __init__(self,cell,capture):
        self.cell=cell;self.spool=cell.root/'capture'/capture/'spool/spool.sqlite3'
        self.stop=threading.Event();self.ends={};self.captured={};self.publications=[]
        self.series=[];self.errors=[];self.busy_samples=0;self.seq=0;self.ordinal=0;self.runtime_error=None
        self.thread=threading.Thread(target=self.run)
    def poll(self):
        # Read short SQLite snapshots, never retain a lease that prevents pruning.
        with closing(sqlite3.connect(f'file:{self.spool}?mode=ro',uri=True,timeout=.05)) as db:
            rows=db.execute('SELECT seq,end_lsn,substr(payload,22,4) FROM transactions WHERE seq>? ORDER BY seq',(self.seq,)).fetchall()
        stamp=time.time()*1000
        for seq,end,xid in rows:
            self.seq=seq;key=struct.unpack('!I',xid)[0]
            self.ends[key]=int(end,16);self.captured.setdefault(key,stamp)
        with closing(sqlite3.connect(f'file:{self.cell.root}/state.sqlite3?mode=ro',uri=True,timeout=.05)) as db:
            rows=db.execute("SELECT ordinal,published_at_ms,json_extract(descriptor,'$.manifest.source.lsn'),export_id,json_extract(descriptor,'$.prepared_at_ms') FROM publications WHERE state='published' AND ordinal>? ORDER BY ordinal",(self.ordinal,)).fetchall()
            policy=db.execute('SELECT record FROM sync_policies WHERE id=?',(self.cell.policy_id,)).fetchone()
            if policy:self.runtime_error=json.loads(policy[0]).get('error')
        for ordinal,at,end,run,prepared in rows:
            self.ordinal=ordinal;self.publications.append(dict(at=at,end=lsn(end),run=run,prepared=prepared))
        # Status file avoids issuing benchmark monitoring RPCs to the single writer.
        status=json.loads((self.spool.parent.parent/'status.json').read_text())
        progress=status.get('progress') or {}
        self.series.append(dict(at_ms=stamp,backlog_bytes=progress.get('backlog_bytes'),
            captured_lsn=status.get('captured_lsn'),published_lsn=progress.get('published_lsn'),
            memory_bytes=int((Path('/sys/fs/cgroup')/'memory.current').read_text())))
    def run(self):
        while not self.stop.is_set():
            try:self.poll()
            except sqlite3.OperationalError as e:
                if getattr(e,'sqlite_errorcode',None) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                    self.busy_samples+=1
                else:
                    print('observer error:',repr(e),flush=True);self.errors.append(type(e).__name__);return
            except Exception as e:
                print('observer error:',repr(e),flush=True);self.errors.append(type(e).__name__);return
            self.stop.wait(.1)
    def finish(self):
        self.stop.set();self.thread.join(timeout=10)
        assert not self.thread.is_alive(),'observer did not stop'
        # A final busy sample is retried only after load has stopped. Missing
        # commit markers still invalidate the trial during attribution.
        for attempt in range(20):
            try:self.poll();break
            except sqlite3.OperationalError as e:
                if getattr(e,'sqlite_errorcode',None) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):raise
                self.busy_samples+=1;time.sleep(.1)
        else:raise RuntimeError('observer remained busy after workload')
        assert not self.errors,self.errors


def attribute(samples,observer,runs):
    cuts=[p['end'] for p in observer.publications]
    assert cuts==sorted(cuts),'published cursor regressed'
    stages={name:[] for name in ('commit_to_publication','commit_to_admission','admission_to_worker_start',
        'worker_start_to_prepared','prepared_to_publication','capture_observed_upper_bound')}
    for s in samples:
        assert s['xid'] in observer.ends,'observer missed a pruned transaction marker; trial invalid'
        at=bisect.bisect_left(cuts,observer.ends[s['xid']])
        assert at<len(cuts),'unpublished transaction'
        p=observer.publications[at];r=runs[p['run']]
        stamps=(s['ack_ms'],r['created_at_ms'],r['started_at_ms'],p['prepared'],p['at'])
        stages['commit_to_publication'].append(max(0,p['at']-s['ack_ms']))
        for name,left,right in zip(list(stages)[1:5],stamps,stamps[1:]):stages[name].append(max(0,right-left))
        stages['capture_observed_upper_bound'].append(max(0,observer.captured[s['xid']]-s['ack_ms']))
    return {name:dict(percentiles(values),maximum=round(max(values),3)) for name,values in stages.items()}


def drain_timeout_evidence(required,captured_sequence,missing_markers):
    # Pruning retains the newest anchor, so SQLite's next implicit sequence never
    # resets. Even counting barriers, a smaller total proves capture is behind.
    assert required>0 and captured_sequence>=0 and missing_markers>=0
    if missing_markers and captured_sequence>=required:
        raise AssertionError('missing transaction markers with no proof of incomplete capture; trial invalid')
    if not missing_markers and captured_sequence<required:
        raise AssertionError('transaction markers contradict the durable capture sequence')
    return dict(required_source_commits=required,captured_sequence_after_stop=captured_sequence,
        missing_observed_markers=missing_markers,uncaptured_commits_lower_bound=max(0,required-captured_sequence))


class RuntimeFailure(Exception):
    """Valid overload/failure observation, without a successful latency claim."""


def healthy(cell):
    try:return cell.healthy()
    except AssertionError as error:
        policy=error.args[0] if error.args else None
        if isinstance(policy,dict) and policy.get('continuous_status',{}).get('state') in ('blocked','failed'):
            raise RuntimeFailure(policy.get('error') or 'policy_blocked') from error
        raise


def trial(args):
    root=Path(tempfile.mkdtemp(prefix='sb-scale-',dir=str(args.scratch))).resolve();root.chmod(0o700)
    release=args.release.resolve();cell=InstalledContinuous(release,root)
    report=dict(status='error',scope='local scaling benchmark; not EC2 emulation or release qualification',
        parameters={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
        release_identity=sha(release/'release.json'),binary_sha256=sha(release/'bin/supabricks'),
        affinity=sorted(os.sched_getaffinity(0)),cgroup_limits=counters(),checks=[])
    observer=None
    try:
        assert json.loads(subprocess.check_output([str(cell.binary),'installation','verify']))['identity']==report['release_identity']
        n=args.rows
        cell.setup_source(release/'python/analytics/python',release/'python/analytics/export.py',
            f'CREATE TABLE orders(id int PRIMARY KEY,value int); CREATE TABLE payments(id int PRIMARY KEY,value int); INSERT INTO orders SELECT i,0 FROM generate_series(1,{n}) i; INSERT INTO payments SELECT i,0 FROM generate_series(1,{n}) i')
        report['phase']='baseline';print('baseline',flush=True)
        _,report['baseline']=load(cell,args.rate,args.baseline,args.clients,n)
        cell.sql(cell.parent,'UPDATE orders SET value=0; UPDATE payments SET value=0')
        p=cell.cli('sync','create','--branch','main','--mode','continuous','--key','policy');cell.policy_id=p['id']
        report['phase']='bootstrap'
        healthy(cell);observer=Observer(cell,p['capture_id']);observer.thread.start()
        report['phase']='warmup';print('warmup',flush=True)
        _,report['warmup']=load(cell,args.rate,args.warmup,args.clients,n)
        healthy(cell)
        report['phase']='measure';print('measure',flush=True)
        before=counters();host_before=host_counters();start=time.perf_counter();wall_start=time.time()*1000
        samples,report['source']=load(cell,args.rate,args.seconds,args.clients,n,offset=1000000)
        wall_end=time.time()*1000;after=counters();elapsed=time.perf_counter()-start
        report['cpu']=cpu_delta(before,after,elapsed)
        report['host']=host_delta(host_before,host_counters());report['cgroup_before']=before;report['cgroup_after']=after
        # Freeze source writes and bound catch-up. A timeout remains an error,
        # not a passing sample with its slow transactions silently discarded.
        report['measurement_start_ms']=wall_start;report['measurement_end_ms']=wall_end
        report['phase']='drain'
        drain=time.perf_counter();deadline=drain+120
        while time.perf_counter()<deadline:
            if observer.errors:raise RuntimeError('observer failed: '+','.join(observer.errors))
            if observer.runtime_error:raise RuntimeFailure(observer.runtime_error)
            if all(s['xid'] in observer.ends for s in samples):
                target=max(observer.ends[s['xid']] for s in samples)
                if observer.publications and observer.publications[-1]['end']>=target:break
            time.sleep(.1)
        else:raise RuntimeFailure('publication_drain_timeout')
        report['drain_seconds']=round(time.perf_counter()-drain,3)
        observer.finish();report['backlog_series']=observer.series;report['observer_busy_samples']=observer.busy_samples
        with closing(sqlite3.connect(f'file:{root}/state.sqlite3?mode=ro',uri=True)) as db:
            runs={r['id']:r for (text,) in db.execute('SELECT record FROM incremental_runs') for r in [json.loads(text)]}
        report['stages_ms']=attribute(samples,observer,runs)
        during=[x for x in observer.series if wall_start<=x['at_ms']<=wall_end]
        report['peak_memory_bytes']=max(x['memory_bytes'] for x in during)
        report['peak_backlog_bytes']=max((x['backlog_bytes'] or 0) for x in during)
        report['last_observed_backlog_bytes']=during[-1]['backlog_bytes']
        report['phase']='correctness'
        current=cell.current()
        for table in ('orders','payments'):
            with cell.source() as db:source=[dict(id=i,value=v) for i,v in db.execute(f'SELECT id,value FROM {table} ORDER BY id').fetchall()]
            actual=sorted(cell.version_rows(current,table),key=lambda row:row['id'])
            assert actual==source,'published table differs from frozen source'
        report['checks'].append('both_published_tables_equal_frozen_postgres_source')
        report['within_5s_p95']=report['stages_ms']['commit_to_publication']['p95']<=5000
        report['offered_load_met']=report['source']['achieved_rows_per_second']>=args.rate*.95
        report['status']='measured';report['phase']='complete'
    except RuntimeFailure as error:
        report['status']='runtime_failed';report['runtime_error']=str(error)
    except Exception as error:
        report['error_type']=type(error).__name__
        # Local private log retains full error; exported reports omit source data.
        import traceback;traceback.print_exc()
    finally:
        if observer:
            observer.stop.set();observer.thread.join(timeout=10)
            report['observer_busy_samples']=observer.busy_samples
            report.setdefault('backlog_series',observer.series)
            report['observed_transactions']=len(observer.ends)
            report['observed_publications']=len(observer.publications)
            during=[x for x in observer.series if report.get('measurement_start_ms',float('inf'))<=x['at_ms']<=report.get('measurement_end_ms',0)]
            if during:
                report['peak_memory_bytes']=max(x['memory_bytes'] for x in during)
                report['peak_backlog_bytes']=max(x['backlog_bytes'] or 0 for x in during)
                report['last_observed_backlog_bytes']=during[-1]['backlog_bytes']
        try:
            if (root/'control.sock').exists():cell.stop()
            report['checks'].append('owned_runtime_stopped')
        except Exception as error:report['status']='error';report['cleanup_error']=type(error).__name__
        if report['status']=='runtime_failed' and report.get('runtime_error')=='publication_drain_timeout':
            try:
                with closing(sqlite3.connect(f'file:{observer.spool}?mode=ro',uri=True,timeout=3)) as db:
                    sequence=db.execute('SELECT COALESCE(max(seq),0) FROM transactions').fetchone()[0]
                report['drain_timeout_evidence']=drain_timeout_evidence(
                    report['warmup']['completed_transactions']+len(samples),sequence,
                    sum(s['xid'] not in observer.ends for s in samples))
            except Exception as error:
                report['status']='error';report['error_type']=type(error).__name__
                import traceback;traceback.print_exc()
        assert sha(release/'release.json')==report['release_identity']
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='measured':shutil.rmtree(root)
        else:print('private fixture retained:',root,flush=True)
    print(json.dumps({k:report.get(k) for k in ('status','source','stages_ms','cpu','within_5s_p95','error_type')}),flush=True)
    return {'measured':0,'runtime_failed':2,'error':1}[report['status']]


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    p.add_argument('--scratch',type=Path,default=Path('/tmp'))
    p.add_argument('--rate',type=int,required=True);p.add_argument('--seconds',type=int,default=45)
    p.add_argument('--baseline',type=int,default=5);p.add_argument('--warmup',type=int,default=5)
    p.add_argument('--clients',type=int,default=4);p.add_argument('--rows',type=int,default=10000)
    a=p.parse_args()
    if min(a.rate,a.seconds,a.baseline,a.warmup,a.clients)<1 or a.rows<a.clients:p.error('positive workload dimensions required')
    raise SystemExit(trial(a))
