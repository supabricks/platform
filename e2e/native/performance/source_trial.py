#!/usr/bin/env python3
"""SP06 source-only capacity. Production runtime and transaction shape are unchanged."""
import argparse
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

from trial import InstalledContinuous, counters, cpu_delta, host_counters, host_delta, percentiles, sha
from profile_trial import Profile

SETTINGS = ('fsync','full_page_writes','synchronous_commit','wal_sync_method','track_io_timing','track_wal_io_timing')
SAMPLE_COLUMNS = ('ack_seconds','latency_ms','begin_ms','updates_ms','xid_ms','commit_ms')


def windows(samples, seconds, width=60):
    """Only acknowledgments inside each complete measured window count as input."""
    result=[]
    for begin in range(0,seconds,width):
        end=min(begin+width,seconds)
        selected=[s for s in samples if begin <= s[0] < end]
        result.append(dict(start_seconds=begin,end_seconds=end,transactions=len(selected),
            changed_rows_s=2*len(selected)/(end-begin),latency_ms=percentiles([s[1] for s in selected]) if selected else None))
    return result


def summarize(samples,seconds,elapsed):
    assert samples and elapsed>=seconds
    inside=[s for s in samples if 0<=s[0]<seconds]
    tails=[s for s in samples if s[0]>=seconds]
    return dict(committed_transactions=len(samples),in_window_transactions=len(inside),tail_transactions=len(tails),
        measurement_seconds=seconds,elapsed_seconds=elapsed,
        committed_changed_rows_s=2*len(inside)/seconds,including_tail_changed_rows_s=2*len(samples)/elapsed,
        windows=windows(samples,seconds),transaction_ms=percentiles([s[1] for s in inside]),
        sql_ms={name:percentiles([s[i] for s in inside]) for i,name in enumerate(SAMPLE_COLUMNS) if i>=2 and inside and inside[0][i] is not None})


def load_source(cell,rate,seconds,clients,rows,offset=0):
    # Same SQL in Continuous.transaction and same disjoint-key rule as trial.load.
    # The high offered rate screens saturation; clients never share a key.
    start=time.perf_counter()+.25;total=int(rate*seconds/2)
    samples=[];errors=[];expected={};lock=threading.Lock()
    def writer(client):
        local=[];values={}
        try:
            with cell.source() as db:
                db.execute("SET statement_timeout='10s'")
                for index in range(client,total,clients):
                    due=start+index*2/rate
                    delay=due-time.perf_counter()
                    if delay>0:time.sleep(delay)
                    if time.perf_counter()>=start+seconds:break
                    key=1+client+clients*((index//clients)%(rows//clients));value=offset+index+1
                    result=cell.transaction(db,key,value);ack=time.perf_counter()-start
                    sql=result.get('sql_ms',{})
                    local.append([ack,result['latency_ms'],*[sql.get(k) for k in ('begin','updates','xid','commit')]])
                    values[key]=value
        except Exception as error:errors.append(type(error).__name__)
        with lock:samples.extend(local);expected.update(values)
    threads=[threading.Thread(target=writer,args=(c,)) for c in range(clients)]
    wall_start=time.time()*1000+(start-time.perf_counter())*1000
    for t in threads:t.start()
    for t in threads:t.join()
    remaining=start+seconds-time.perf_counter()
    if remaining>0:time.sleep(remaining)
    elapsed=time.perf_counter()-start
    if errors:raise RuntimeError('source writer failure: '+','.join(errors))
    samples.sort(key=lambda s:s[0])
    return samples,expected,dict(summarize(samples,seconds,elapsed),offered_changed_rows_s=rate,
        offered_transactions=total,unsent_transactions=total-len(samples),measurement_start_ms=wall_start,
        measurement_end_ms=wall_start+seconds*1000)


def disk_counters():
    result={}
    for line in Path('/proc/diskstats').read_text().splitlines():
        values=line.split();fields=list(map(int,values[3:]))
        if len(fields)>=11:
            result[values[0]+':'+values[1]]=dict(read_ios=fields[0],read_sectors=fields[2],read_ms=fields[3],
                write_ios=fields[4],write_sectors=fields[6],write_ms=fields[7],io_ms=fields[9],weighted_io_ms=fields[10])
    return result


class SourceProfile(Profile):
    """Reuse the frozen PG/process/storage sampler; source-only collection contract."""
    def __init__(self,cell,report):
        super().__init__(cell,report);self.resources=[]
    def processes(self):
        result=super().processes()
        self.resources.append(dict(at_ms=time.time()*1000,cgroup=counters(),host_disks=disk_counters()))
        return result
    def collect_source(self,path):
        data=dict(scope='Source-only: no capture or apply policy. PG/storage samples use the existing profiler; host disk utilization is shared, cgroup counters are isolated. SQL text and row values are excluded.',
            observations=self.rows,resources=self.resources,monitor_wall_ns=self.monitor_ns,
            pg_settings=self.pg_settings,storage_status=self.storage_status,monitor_errors=self.errors,
            process_sample_errors=self.process_sample_errors)
        path.write_bytes(gzip.compress((json.dumps(data,separators=(',',':'))+'\n').encode(),mtime=0))
        assert not self.errors,self.errors
        assert self.rows and self.resources
        assert len(self.storage_urls)==2,'storage metrics unavailable'
        assert not any('observation_error' in metrics for row in self.rows for metrics in row.get('storage_metrics',{}).values()),'storage observation failed'
        return dict(path=path.name,observations=len(self.rows),resources=len(self.resources))


def run(args):
    root=Path(tempfile.mkdtemp(prefix='sb-source-',dir=args.scratch)).resolve();root.chmod(0o700)
    release=args.release.resolve();cell=InstalledContinuous(release,root)
    report=dict(status='error',scope='SP06 source-only capacity, not replication throughput or release qualification',
        parameters={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
        release_identity=sha(release/'release.json'),binary_sha256=sha(release/'bin/supabricks'),
        affinity=sorted(os.sched_getaffinity(0)),cgroup_limits=counters(),checks=[])
    profile=SourceProfile(cell,report) if args.profile else None
    try:
        assert json.loads(subprocess.check_output([str(cell.binary),'installation','verify']))['identity']==report['release_identity']
        n=args.rows
        cell.setup_source(release/'python/analytics/python',release/'python/analytics/export.py',
            f'CREATE TABLE orders(id int PRIMARY KEY,value int); CREATE TABLE payments(id int PRIMARY KEY,value int); INSERT INTO orders SELECT i,0 FROM generate_series(1,{n}) i; INSERT INTO payments SELECT i,0 FROM generate_series(1,{n}) i')
        with cell.source() as db:
            report['pg_settings']={k:v for k,v in db.execute('SELECT name,setting FROM pg_settings WHERE name=ANY(%s)',(list(SETTINGS),)).fetchall()}
        assert report['pg_settings']==dict(fsync='on',full_page_writes='on',synchronous_commit='on',wal_sync_method='fdatasync',track_io_timing='off',track_wal_io_timing='off'),report['pg_settings']
        with closing(sqlite3.connect(f'file:{root}/state.sqlite3?mode=ro',uri=True)) as db:
            assert db.execute('SELECT count(*) FROM sync_policies').fetchone()==(0,)
            assert db.execute('SELECT count(*) FROM sync_captures').fetchone()==(0,)
        report['checks'].append('no_capture_or_apply_policy')
        if profile:profile.start()
        report['phase']='warmup';print('warmup',flush=True)
        _,warm_expected,report['warmup']=load_source(cell,args.rate,args.warmup,args.clients,n)
        report['phase']='measure';print('measure',flush=True)
        before=counters();host_before=host_counters();disk_before=disk_counters();start=time.perf_counter()
        samples,expected,report['source']=load_source(cell,args.rate,args.seconds,args.clients,n,offset=10000000)
        elapsed=time.perf_counter()-start;after=counters()
        report.update(cpu=cpu_delta(before,after,elapsed),cgroup_before=before,cgroup_after=after,
            peak_memory_bytes=int(after['memory.peak']),memory_peak_scope='cgroup lifetime through load, including setup and warmup',host=host_delta(host_before,host_counters()),
            host_disk_before=disk_before,host_disk_after=disk_counters())
        # Store numeric timings only. Row values and private connection metadata stay local.
        path=args.report.with_name('source-samples.json.gz')
        path.write_bytes(gzip.compress((json.dumps(dict(columns=SAMPLE_COLUMNS,samples=samples),separators=(',',':'))+'\n').encode(),mtime=0))
        report['samples']=dict(path=path.name,sha256=sha(path),count=len(samples))
        report['phase']='correctness';warm_expected.update(expected)
        expected_rows=[(key,warm_expected.get(key,0)) for key in range(1,n+1)]
        for table in ('orders','payments'):
            with cell.source() as db:actual=db.execute(f'SELECT id,value FROM {table} ORDER BY id').fetchall()
            assert actual==expected_rows,'frozen source differs from acknowledged generator state'
        report['checks'].append('both_source_tables_equal_acknowledged_generator_state')
        report['final_state_sha256']=hashlib.sha256(json.dumps(expected_rows,separators=(',',':')).encode()).hexdigest()
        report['status']='measured';report['phase']='complete'
    except Exception as error:
        report['error_type']=type(error).__name__
        import traceback;traceback.print_exc()
    finally:
        if profile:
            try:profile.finish()
            except Exception as error:report.update(status='error',profile_error=type(error).__name__)
        try:
            if (root/'control.sock').exists():cell.stop()
            report['checks'].append('owned_runtime_stopped')
        except Exception as error:report.update(status='error',cleanup_error=type(error).__name__)
        if profile:
            try:report['profile']=profile.collect_source(args.report.with_name('profile.json.gz'))
            except Exception as error:report.update(status='error',profile_error=type(error).__name__)
        assert sha(release/'release.json')==report['release_identity']
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='measured':shutil.rmtree(root)
        else:print('private fixture retained:',root,flush=True)
    print(json.dumps({k:report.get(k) for k in ('status','source','cpu','error_type','profile_error')}),flush=True)
    return 0 if report['status']=='measured' else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('release','report','scratch'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--rate',type=int,default=10000);p.add_argument('--seconds',type=int,default=300)
    p.add_argument('--warmup',type=int,default=60);p.add_argument('--clients',type=int,required=True)
    p.add_argument('--rows',type=int,default=10000);p.add_argument('--profile',action='store_true')
    a=p.parse_args()
    if min(a.rate,a.seconds,a.warmup,a.clients)<1 or a.rows<a.clients:p.error('positive dimensions and at least one key/client required')
    raise SystemExit(run(a))
