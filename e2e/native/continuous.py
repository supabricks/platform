#!/usr/bin/env python3
"""SY05: continuous correctness, lifecycle and the declared two-table workload."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
from triggered import Triggered
from cell import wait,lsn


def percentiles(values):
    values=sorted(values)
    return {f'p{p}':round(values[min(len(values)-1,math.ceil(len(values)*p/100)-1)],3) for p in (50,95,99)}

class Continuous(Triggered):
    def check(self,name):
        super().check(name);print(name,flush=True)
    def policy(self):return self.sync('get',id=self.policy_id)
    def healthy(self):
        def ready():
            p=self.policy();state=p['continuous_status']['state']
            if state in ('blocked','failed'):raise AssertionError(p)
            return p if state=='healthy' else False
        return wait(ready,timeout=180)
    def current(self):return self.api('current_snapshot',branch='main')['publication']
    def transaction(self,db,key,value):
        start=time.perf_counter();db.execute('BEGIN')
        db.execute(f'UPDATE orders SET value={value} WHERE id={key}; UPDATE payments SET value={value} WHERE id={key}')
        xid=int(db.execute('SELECT pg_current_xact_id()::text').fetchone()[0]) % (1<<32)
        db.execute('COMMIT')
        return dict(xid=xid,ack_ms=time.time()*1000,latency_ms=(time.perf_counter()-start)*1000)
    def process_samples(self):
        rows=[line.split() for line in subprocess.check_output(['ps','-axo','pid=,ppid=,rss=,time='],text=True).splitlines()]
        owned={self.daemons[-1].pid}
        while True:
            children={int(pid) for pid,parent,_,_ in rows if int(parent) in owned}
            if children<=owned:break
            owned|=children
        for pid,_,mem,cpu in rows:
            if int(pid) not in owned:continue
            days,sep,clock=cpu.partition('-');clock=clock if sep else days
            seconds=0
            for part in clock.split(':'):seconds=seconds*60+float(part)
            if sep:seconds+=int(days)*86400
            yield pid,int(mem)*1024,seconds
    def workload(self,cap):
        samples=[];errors=[];duration=30;count=750;stop=threading.Event();resources=dict(peak_owned_rss_bytes=0,peak_allocated_data_bytes=0,spool_bytes=0,retained_wal_bytes=0)
        cpu_first={};cpu_last={};ends={}
        def commits():
            # SY07 removes published prefixes. Record the benchmark's markers
            # while they are present, without retaining a SQLite reader lease
            # or disabling the production pruning path during measurement.
            ends.update({struct.unpack('!I',payload[21:25])[0]:end for end,payload in self.journal(cap)})
        def observe():
            while not stop.is_set():
                try:
                    commits()
                    rss=0
                    for pid,memory,seconds in self.process_samples():
                        rss+=memory
                        cpu_first.setdefault(pid,seconds);cpu_last[pid]=seconds
                    resources['peak_owned_rss_bytes']=max(resources['peak_owned_rss_bytes'],rss)
                    seen=set();disk=0
                    for path in self.root.rglob('*'):
                        try:
                            stat=path.stat();identity=(stat.st_dev,stat.st_ino)
                            if path.is_file() and identity not in seen:seen.add(identity);disk+=stat.st_blocks*512
                        except FileNotFoundError:pass
                    resources['peak_allocated_data_bytes']=max(resources['peak_allocated_data_bytes'],disk)
                    c=self.status(cap)
                    for name in ('spool_bytes','retained_wal_bytes'):resources[name]=max(resources[name],c[name] or 0)
                except Exception as e:errors.append(repr(e));return
                stop.wait(.5)
        monitor=threading.Thread(target=observe);monitor.start()
        started=time.perf_counter()
        try:
            with self.source() as db:
                for i in range(count):
                    delay=started+i/25-time.perf_counter()
                    if delay>0:time.sleep(delay)
                    samples.append(self.transaction(db,1000+i,i+1))
                elapsed=time.perf_counter()-started
                db.execute('BEGIN');db.execute('UPDATE orders SET value=777 WHERE id BETWEEN 9001 AND 9100; UPDATE payments SET value=777 WHERE id BETWEEN 9001 AND 9100')
                xid=int(db.execute('SELECT pg_current_xact_id()::text').fetchone()[0])%(1<<32);db.execute('COMMIT')
                burst=dict(xid=xid,ack_ms=time.time()*1000)
            def burst_end():
                commits()
                return ends.get(burst['xid'],False)
            end=wait(burst_end)
            wait(lambda:lsn(self.current()['descriptor']['manifest']['source']['lsn'])>=end,timeout=120)
            self.healthy()
        finally:stop.set();monitor.join(timeout=10)
        assert not errors,errors
        # Match each source XID to the durable complete commit and first atomic
        # publication covering that end LSN. Polling latency is not the metric.
        commits()
        assert all(s['xid'] in ends for s in [*samples,burst]),'benchmark missed a transaction marker before reclamation'
        with sqlite3.connect(f'file:{self.root}/state.sqlite3?mode=ro',uri=True) as db:
            publications=[(json.loads(d),at) for d,at in db.execute("SELECT descriptor,published_at_ms FROM publications WHERE state='published' ORDER BY ordinal")]
            runs={r['id']:r for (record,) in db.execute('SELECT record FROM incremental_runs') for r in [json.loads(record)]}
        phases={name:[] for name in ('admission_to_worker_start','worker_start_to_prepared','prepared_to_publication')}
        for descriptor,at in publications:
            run=runs.get(descriptor['export_id'])
            if not run or at<samples[0]['ack_ms']:continue
            times=(run['created_at_ms'],run['started_at_ms'],descriptor['prepared_at_ms'],at)
            for name,left,right in zip(phases,times,times[1:]):phases[name].append(max(0,right-left))
        self.metrics['materialization_ms']={name:dict(percentiles(values),maximum=max(values),batches=len(values)) for name,values in phases.items() if values}
        cuts=[(lsn(d['manifest']['source']['lsn']),at) for d,at in publications]
        manifests=[d['manifest'] for d,at in publications if at>=samples[0]['ack_ms']]
        input_bytes=sum(m.get('input_bytes',0) for m in manifests)
        written=sum(t['metrics'].get('new_parquet_bytes',0) for m in manifests for t in m.get('apply_metrics',[]))
        self.metrics['storage']=dict(input_bytes=input_bytes,new_parquet_bytes=written,
            write_amplification_ratio=round(written/input_bytes,3) if input_bytes else None,
            peak_inventory_files=max(len(m['files']) for m in manifests),
            peak_generation_bytes=max(m.get('generation_bytes',0) for m in manifests),
            compaction_bytes=sum((m.get('compaction') or {}).get('output_bytes',0) for m in manifests))
        def latency(s):
            end=ends[s['xid']];at=next(at for boundary,at in cuts if boundary>=end)
            return max(0,at-s['ack_ms'])
        lags=[latency(s) for s in samples];bursts=latency(burst)
        resources['sampled_owned_cpu_seconds_lower_bound']=round(sum(cpu_last[p]-cpu_first[p] for p in cpu_first),3)
        self.metrics.update(workload=dict(tables=2,rows_per_table=10000,transactions=count,changed_rows=count*2,
            target_rows_per_second=50,achieved_rows_per_second=round(count*2/elapsed,2),elapsed_seconds=round(elapsed,3),
            commit_to_publication_ms=percentiles(lags),oltp_transaction_ms=percentiles([s['latency_ms'] for s in samples]),
            burst_changed_rows=200,burst_commit_to_publication_ms=round(bursts,3)),resources=resources)
        assert elapsed<=duration*1.1,self.metrics
        assert percentiles(lags)['p95']<=5000,self.metrics
        assert bursts<=5000,self.metrics
        current=self.current();left=self.version_rows(current,'orders');right=self.version_rows(current,'payments')
        assert sorted(left,key=lambda r:r['id'])==sorted(right,key=lambda r:r['id'])
        expected={1000+i:i+1 for i in range(count)}|{key:777 for key in range(9001,9101)}
        assert all(r['value']==expected[r['id']] for r in left if r['id'] in expected)
        self.check('sustained_50_rows_per_second_and_200_row_burst_meet_5s_p95_with_atomic_groups')
    def run(self,python,worker):
        self.metrics={};self.python=python;self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{self.project}"\nname = "continuous-sync"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        self.parent=self.create('main');self.configure(worker)
        self.sql(self.parent,'CREATE TABLE orders(id int PRIMARY KEY,value int); CREATE TABLE payments(id int PRIMARY KEY,value int); INSERT INTO orders SELECT i,0 FROM generate_series(1,10000) i; INSERT INTO payments SELECT i,0 FROM generate_series(1,10000) i')
        with self.source() as db:
            self.metrics['source_table_bytes']=int(db.execute("SELECT sum(pg_total_relation_size(x)) FROM unnest(ARRAY['public.orders'::regclass,'public.payments'::regclass]) x").fetchone()[0])
            for i in range(10):self.transaction(db,1,0)
            baseline=[self.transaction(db,1,0)['latency_ms'] for _ in range(100)]
        self.metrics['baseline_oltp_transaction_ms']=percentiles(baseline)
        p=self.cli('sync','create','--branch','main','--mode','continuous','--key','policy');self.policy_id=p['id'];cap=dict(id=p['capture_id'])
        p=self.healthy();initial=self.current();bootstrap=self.status(cap)['bootstrap_id']
        time.sleep(3);assert self.healthy()['last_epoch_id']==p['last_epoch_id'],'idle barriers must not churn epochs'
        self.check('automatic_bootstrap_and_fresh_idle_observation_without_empty_epoch_churn')
        reader=self.opened(epoch=initial['epoch_id'],ttl_ms=600000)
        self.workload(cap)
        assert self.query(reader,'SELECT sum(value) FROM public.orders')['rows']==[['0']];self.close(reader)
        # A transaction opened before idle observations is invisible until commit.
        with self.source() as db:
            db.execute('BEGIN');db.execute('UPDATE orders SET value=900 WHERE id=9500; UPDATE payments SET value=900 WHERE id=9500')
            time.sleep(2);before=self.current();assert next(r['value'] for r in self.version_rows(before,'orders') if r['id']==9500)==0
            db.execute('COMMIT')
        self.wait_value(9500,900)
        self.check('long_transaction_is_invisible_until_complete_commit_and_old_reader_stays_pinned')
        gate=self.hold(worker)
        self.sql(self.parent,'UPDATE orders SET value=901 WHERE id=9501; UPDATE payments SET value=901 WHERE id=9501')
        wait(lambda:next((r for r in self.sync('runs',id=p['id']) if r['state']=='running' and r['apply_id']),False))
        time.sleep(5.5);lagging=self.policy()['continuous_status'];assert lagging['state']=='lagging' and lagging['backlog_bytes']>0,lagging
        p=self.policy();p=self.sync('pause',id=p['id'],expected_revision=p['revision'],key='pause');assert p['pause_requested']
        gate.unlink();self.configure(worker)
        p=wait(lambda:(p if (p:=self.policy())['state']=='paused' else False))
        assert not p['pause_requested'] and self.status(cap)['desired']=='running'
        prior=self.current()['epoch_id'];self.sql(self.parent,'UPDATE orders SET value=902 WHERE id=9502; UPDATE payments SET value=902 WHERE id=9502');time.sleep(2)
        assert self.current()['epoch_id']==prior
        p=self.sync('resume',id=p['id'],expected_revision=p['revision'],key='resume');self.wait_value(9502,902)
        assert self.status(cap)['bootstrap_id']==bootstrap
        self.check('held_batch_reports_lag_then_pause_drains_and_resume_reuses_checkpoint')
        process=next(r for r in self.records() if r['role']=='capture-'+cap['id']);os.kill(process['pid'],signal.SIGSTOP)
        try:
            time.sleep(5.5);status=self.policy()['continuous_status'];assert status['state']=='unavailable' and status['oldest_unpublished_commit_age_ms'] is None,status
            self.sql(self.parent,'UPDATE orders SET value=903 WHERE id=9503; UPDATE payments SET value=903 WHERE id=9503')
        finally:os.kill(process['pid'],signal.SIGKILL)
        self.wait_value(9503,903)
        self.stop();self.start();self.healthy();assert self.status(cap)['bootstrap_id']==bootstrap
        self.check('stale_worker_is_unavailable_then_sigkill_and_full_restart_recover_without_resync')
        with self.source() as db:
            os.kill(self.daemons[-1].pid,signal.SIGSTOP)
            try:
                time.sleep(5.5);self.transaction(db,9504,904)
            finally:os.kill(self.daemons[-1].pid,signal.SIGCONT)
        self.wait_value(9504,904)
        self.check('delayed_controller_tick_coalesces_catchup_without_losing_captured_commits')
        # Source schema fencing stops the whole group and never silently bootstraps.
        prior=self.current()['epoch_id'];self.sql(self.parent,'ALTER TABLE orders ADD COLUMN unsupported int')
        wait(lambda:self.policy()['continuous_status']['state']=='blocked')
        assert self.current()['epoch_id']==prior
        self.delete_capture(cap)
        self.sql(self.parent,'ALTER TABLE orders DROP COLUMN unsupported')
        p=self.policy();replacement=self.capture('start',policy_id=p['id'],expected_revision=p['revision'],key='resync',limits=dict(spool_bytes=512*1024*1024,wal_bytes=512*1024*1024))
        p=self.sync('resume',id=p['id'],expected_revision=p['revision'],key='resync-resume')
        self.wait_value(9504,904);assert self.status(replacement)['bootstrap_id']!=bootstrap
        self.check('schema_failure_preserves_epoch_and_explicit_resync_builds_new_baseline')
        p=self.policy();self.sync('delete',id=p['id'],expected_revision=p['revision'],key='delete');self.state_is(replacement,'deleted')
        self.check('policy_delete_cleans_owned_continuous_capture')
        self.stop()
    def wait_value(self,key,value):
        def done():
            self.healthy();current=self.current()
            rows=self.version_rows(current,'payments')
            return current if next(r['value'] for r in rows if r['id']==key)==value else False
        return wait(done,timeout=120)

def main():
    p=argparse.ArgumentParser()
    for name in ('binary','bundle','helpers','python','worker','report'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();root=Path(tempfile.mkdtemp(prefix='sb-sy05-',dir='/tmp')).resolve();root.chmod(0o700)
    cell=Continuous(a.binary.resolve(),a.bundle.resolve(),a.helpers.resolve(),root)
    report=dict(status='FAIL',checks=cell.checks,host=dict(system=platform.system(),machine=platform.machine(),cpu_count=os.cpu_count()),scope='Disposable source-level native test; no installed archive or production SLA claim.')
    def sha(path):
        with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
    repo=Path(__file__).resolve().parents[2]
    files=[repo/'crates/local/src/sync.rs',repo/'crates/local/src/store/sync.rs',*sorted((repo/'crates/local/src/store/sync').glob('*.rs')),
        repo/'crates/local/src/engine/captures.rs',repo/'crates/local/src/engine/incremental.rs',Path(__file__).resolve(),
        *sorted((repo/'python/analytics').glob('*.py')),*sorted((repo/'python/analytics/capture').glob('*.py')),*sorted((repo/'python/analytics/incremental').glob('*.py'))]
    report['provenance']=dict(binary_sha256=sha(a.binary),engine_manifest_sha256=sha(a.bundle/'manifest.json'),
        source_files={str(path.relative_to(repo)):sha(path) for path in files})
    try:cell.run(a.python.absolute(),a.worker.resolve());report['status']='PASS'
    finally:
        report['metrics']=getattr(cell,'metrics',{})
        if (root/'control.sock').exists():
            try:cell.stop()
            except Exception:report.update(status='FAIL',cleanup='failed')
        if report['status']!='PASS':report['state_dir']=str(root)
        a.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='PASS' and 'cleanup' not in report:shutil.rmtree(root)
    print(json.dumps(report,indent=2));assert report['status']=='PASS',report
if __name__=='__main__':main()
