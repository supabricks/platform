"""Installed in-flight preparation fencing; functional qualification, not timing.

Hold the actual daemon-owned preparer and apply worker with SIGSTOP, then pause
or crash/recover the daemon. Use the immutable installed workers and assert the
old process identities and preparation directory cannot survive or be replayed.
Requires a package built with the experimental sync-lookahead Cargo feature;
the normal release deliberately does not launch this stage.
"""
from pathlib import Path
import os
import hashlib
import json
import signal
import sys
import threading
import time
import psutil

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
import installed_sync
from cell import wait
from preparation_lifecycle import LargeProfile

ROWS=32768
VALUE='x'*512


class Lookahead(LargeProfile,installed_sync.InstalledContinuous):
    def freeze_pipeline(self,first):
        failures=[]
        def write():
            try:
                with self.source() as db:
                    for start in range(first,first+ROWS,1024):
                        with db.transaction():
                            for table in ('orders','payments'):
                                db.execute(f"INSERT INTO {table} SELECT i,repeat('x',512) FROM generate_series(%s::int,%s::int) i",(start,start+1023))
            except BaseException as error:failures.append(error)
        producer=threading.Thread(target=write);producer.start()
        frozen=[];directory=None
        try:
            deadline=time.monotonic()+90
            while time.monotonic()<deadline and not failures:
                records=self.records()
                prep=next((r for r in records if r['role']=='incremental-prepare'),None)
                apply=next((r for r in records if r['role'].startswith('incremental-') and r['role']!='incremental-prepare'),None)
                if prep and apply:
                    try:
                        process=psutil.Process(prep['pid'])
                        if process.status()==psutil.STATUS_ZOMBIE:continue
                        process.suspend();frozen.append(process)
                        writer=psutil.Process(apply['pid']);writer.suspend();frozen.append(writer)
                        paths=list((self.root/'analytics/prepare-work').iterdir())
                        assert len(paths)==1
                        directory=paths[0]
                        break
                    except psutil.NoSuchProcess:
                        for p in frozen:
                            try:p.resume()
                            except psutil.NoSuchProcess:pass
                        frozen=[]
                time.sleep(.005)
            producer.join(timeout=60)
            assert not producer.is_alive(),'source writer did not finish'
            if failures:raise failures[0]
            assert directory is not None and len(frozen)==2,'no in-flight preparation observed'
            assert frozen[0].status()==psutil.STATUS_STOPPED,'preparer exited before fencing'
            return frozen,directory
        except BaseException:
            for p in frozen:
                try:p.resume()
                except psutil.NoSuchProcess:pass
            raise

    def gone(self,process):
        try:return not process.is_running() or process.status()==psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:return True

    def exact(self,count):
        # Unique integers, cardinality and closed min/max prove every key in the
        # interval. Equal min/max payload proves every corresponding value.
        def published():
            p=self.policy();assert p['continuous_status']['state'] not in ('failed','blocked'),p
            return sum(t['rows'] for t in self.current()['descriptor']['manifest']['tables'])==count*2
        wait(published,timeout=180)
        reader=self.opened(epoch=self.current()['epoch_id'],ttl_ms=600000)
        try:
            for table in ('orders','payments'):
                rows=self.query(reader,f'SELECT count(*),count(DISTINCT id),min(id),max(id),min(payload),max(payload) FROM public.{table}')['rows']
                assert rows==[[str(count),str(count),'0',str(count-1),VALUE,VALUE]],rows
        finally:self.close(reader)

    def run(self,python,worker):
        self.metrics['fixture_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.setup_source(python,worker,"CREATE TABLE orders(id int PRIMARY KEY,payload text); CREATE TABLE payments(id int PRIMARY KEY,payload text); INSERT INTO orders VALUES(0,repeat('x',512)); INSERT INTO payments VALUES(0,repeat('x',512))")
        policy=self.cli('sync','create','--branch','main','--mode','continuous','--key','lookahead')
        self.policy_id=policy['id'];self.healthy()
        processes,directory=self.freeze_pipeline(1)
        try:
            p=self.policy();self.sync('pause',id=p['id'],expected_revision=p['revision'],key='pause-inflight')
            wait(lambda:self.gone(processes[0]),timeout=30)
            assert not directory.exists(),'pause retained preparation'
        finally:
            for process in processes:
                try:process.resume()
                except psutil.NoSuchProcess:pass
        wait(lambda:self.policy()['state']=='paused',timeout=60)
        p=self.policy();self.sync('resume',id=p['id'],expected_revision=p['revision'],key='resume-inflight')
        self.exact(ROWS+1)
        self.check('pause_fences_stopped_preparer_and_resume_applies_every_key_exactly')

        processes,directory=self.freeze_pipeline(ROWS+1)
        os.kill(self.daemons[-1].pid,signal.SIGKILL);self.daemons[-1].wait(timeout=10)
        self.start()
        assert all(self.gone(p) for p in processes),'old pipeline process survived recovery'
        assert not directory.exists(),'old preparation directory survived recovery'
        self.exact(2*ROWS+1)
        self.check('daemon_sigkill_fences_inflight_pipeline_and_replays_only_durable_apply')
        self.metrics.update(rows_per_table=2*ROWS+1,inflight_fences=2)
        self.stop()


class LookaheadDisabled(Lookahead):
    def run(self,python,worker):
        self.metrics['fixture_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.setup_source(python,worker,"CREATE TABLE orders(id int PRIMARY KEY,payload text); CREATE TABLE payments(id int PRIMARY KEY,payload text); INSERT INTO orders VALUES(0,repeat('x',512)); INSERT INTO payments VALUES(0,repeat('x',512))")
        self.policy_id=self.cli('sync','create','--branch','main','--mode','continuous','--key','serial-default')['id']
        self.healthy()
        with self.source() as db:
            for start in range(1,ROWS+1,1024):
                with db.transaction():
                    for table in ('orders','payments'):
                        db.execute(f"INSERT INTO {table} SELECT i,repeat('x',512) FROM generate_series(%s::int,%s::int) i",(start,start+1023))
        self.exact(ROWS+1)
        inputs=list((self.root/'analytics/apply-workers').glob('*/input.json'))
        assert inputs,'no installed incremental worker exercised'
        for path in inputs:
            config=json.loads(path.read_text())
            assert config['storage_profile']=='large' and config['prepare_next'] is False
            assert 'prepared_batch' not in config
        assert all(p['role']!='incremental-prepare' for p in self.records())
        directory=self.root/'analytics/prepare-work'
        assert not directory.exists() or not list(directory.iterdir())
        self.check('normal_large_profile_keeps_serial_default_and_exact_all_keys')
        self.metrics['rows_per_table']=ROWS+1
        self.stop()


if __name__=='__main__':
    installed_sync.SUITES={'lookahead':Lookahead,'lookahead-disabled':LookaheadDisabled}
    installed_sync.main()
