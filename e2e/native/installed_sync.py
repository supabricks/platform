#!/usr/bin/env python3
"""SY08: unchanged installed workers, real PG/Sail and archive-bound sync evidence."""
import argparse
import hashlib
import json
import os
import platform
import psutil
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from continuous import Continuous
from triggered import Triggered
from sync_maintenance import Maintenance
from sync_surfaces import Surfaces
from cell import wait


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


class Installed:
    def __init__(self,release,root):
        self.release=release
        super().__init__(release/'bin/supabricks',release/'engine',release/'helpers',root,exact_installed=True)
        self.catalog_runtime=release/'share/unity-catalog'
        self.notebook_worker=release/'python/analytics/export.py'
        self.metrics={}
    def configure(self,worker):
        assert worker==self.release/'python/analytics/export.py'
        assert Path(self.python)==self.release/'python/analytics/python'
        # Installed startup configures its verified runtime. A source override
        # would invalidate the claim that the packaged workers were exercised.
        assert self.config['installation_identity']==sha(self.release/'release.json')
        assert not (self.root/'analytics.json').exists()
    def api(self,action,**fields):
        if action=='configure_analytics':
            assert Path(fields['python'])==self.release/'python/analytics/python'
            self.configure(Path(fields['worker']))
            return {}
        return super().api(action,**fields)
    def request(self,**request):
        if request.get('method')=='catalog_service' and request.get('command',{}).get('action')=='configure':
            command=dict(request['command']);provider=dict(command['provider'])
            assert provider==dict(mode='local',runtime=str(self.catalog_runtime))
            provider.pop('runtime');command['provider']=provider;request['command']=command
        return super().request(**request)
    def check(self,name):
        # Installed evidence uses the release collectors' list-of-names contract.
        self.checks.append(name)
        print('PASS',name,flush=True)
    def process_samples(self):
        # macOS Seatbelt cannot execute the system ps binary. Inspect only this
        # daemon's descendants and retain creation time to distinguish PID reuse.
        daemon=psutil.Process(self.daemons[-1].pid)
        for process in [daemon,*daemon.children(recursive=True)]:
            try:
                with process.oneshot():
                    identity=(process.pid,process.create_time());cpu=process.cpu_times()
                    yield identity,process.memory_info().rss,cpu.user+cpu.system
            except psutil.NoSuchProcess:pass
    def stop(self):
        descendants=set()
        for record in self.records():
            try:
                process=psutil.Process(record['pid'])
                descendants.add(process);descendants.update(process.children(recursive=True))
            except psutil.NoSuchProcess:pass
        self.request(method='shutdown')
        wait(lambda:not (self.root/'control.sock').exists(),timeout=60)
        for process in self.daemons:
            if process.poll() is None:process.wait(timeout=10)
            assert process.returncode in (0,-signal.SIGKILL),'daemon failed during shutdown'
        assert not self.records(),'owned processes remain after shutdown'
        for process in descendants:
            try:assert not process.is_running() or process.status()==psutil.STATUS_ZOMBIE,'owned descendant survived cleanup'
            except psutil.NoSuchProcess:pass
    def setup_source(self,python,worker,ddl):
        self.python=python;self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version=1\nid="{self.project}"\nname="installed-sync"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        self.parent=self.create('main');self.configure(worker);self.sql(self.parent,ddl)


class InstalledTriggered(Installed,Triggered):
    def run(self,python,worker):
        self.setup_source(python,worker,"CREATE TABLE orders(id int PRIMARY KEY,note text); CREATE TABLE payments(id int PRIMARY KEY); INSERT INTO orders VALUES(1,'one'); INSERT INTO payments VALUES(1)")
        p=self.cli('sync','create','--branch','main','--mode','triggered','--strategy','incremental','--key','policy')
        run=self.trigger(p,'first');cap=dict(id=run['capture_id'])
        assert self.trigger(p,'first')['id']==run['id']
        first,publication=self.finished(run)
        reader=self.opened(epoch=first['epoch_id'],ttl_ms=600000)
        self.sql(self.parent,"BEGIN; INSERT INTO orders VALUES(2,'two'); INSERT INTO payments VALUES(2); COMMIT")
        with self.source() as long:
            long.execute('BEGIN');long.execute("INSERT INTO orders VALUES(3,'later'); INSERT INTO payments VALUES(3)")
            bounded,current=self.finished(self.trigger(p,'bounded'))
            assert sorted(row['id'] for row in self.version_rows(current,'payments'))==[1,2]
            long.execute('COMMIT')
        assert self.query(reader,'SELECT count(*) FROM public.payments')['rows']==[['1']]
        self.close(reader)
        later,current=self.finished(self.trigger(p,'later'))
        assert len(self.version_rows(current,'payments'))==3
        unchanged,current2=self.finished(self.trigger(p,'no-change'))
        assert [t['version'] for t in current['descriptor']['manifest']['tables']]==[t['version'] for t in current2['descriptor']['manifest']['tables']]
        self.check('triggered_fixed_barrier_idempotency_long_transaction_and_pinned_reader')
        for batch in range(12):self.sql(self.parent,f"INSERT INTO orders SELECT i,repeat('x',200000) FROM generate_series({100+batch*8},{107+batch*8}) i")
        bulk,current=self.finished(self.trigger(p,'multi-batch'))
        assert bulk['batches']>=2 and len(self.version_rows(current,'orders'))==99,bulk
        self.check('triggered_production_input_budget_preserves_complete_transactions')
        bootstrap=self.status(cap)['bootstrap_id'];self.stop();self.start()
        restart,current=self.finished(self.trigger(p,'restart'))
        assert self.status(cap)['bootstrap_id']==bootstrap
        assert len(self.version_rows(current,'orders'))==99
        self.sync('delete',id=p['id'],expected_revision=p['revision'],key='delete');self.state_is(cap,'deleted')
        self.check('triggered_restart_reuses_checkpoint_and_delete_retires_source')
        self.stop()


class InstalledContinuous(Installed,Continuous):
    def run(self,python,worker):
        self.setup_source(python,worker,'CREATE TABLE orders(id int PRIMARY KEY,value int); CREATE TABLE payments(id int PRIMARY KEY,value int); INSERT INTO orders SELECT i,0 FROM generate_series(1,10000) i; INSERT INTO payments SELECT i,0 FROM generate_series(1,10000) i')
        with self.source() as db:
            from continuous import percentiles
            self.metrics['source_table_bytes']=int(db.execute("SELECT sum(pg_total_relation_size(x)) FROM unnest(ARRAY['public.orders'::regclass,'public.payments'::regclass]) x").fetchone()[0])
            baseline=[self.transaction(db,1,0)['latency_ms'] for _ in range(100)]
            self.metrics['baseline_oltp_transaction_ms']=percentiles(baseline)
        p=self.cli('sync','create','--branch','main','--mode','continuous','--key','policy');self.policy_id=p['id'];cap=dict(id=p['capture_id'])
        p=self.healthy();first=self.current();bootstrap=self.status(cap)['bootstrap_id']
        time.sleep(3);assert self.healthy()['last_epoch_id']==p['last_epoch_id']
        reader=self.opened(epoch=first['epoch_id'],ttl_ms=600000)
        self.workload(cap)
        assert self.query(reader,'SELECT sum(value) FROM public.orders')['rows']==[['0']];self.close(reader)
        self.check('continuous_idle_and_old_epoch_remain_stable_during_workload')
        p=self.policy();self.sync('pause',id=p['id'],expected_revision=p['revision'],key='pause')
        wait(lambda:self.policy()['state']=='paused');before=self.current()['epoch_id']
        self.sql(self.parent,'UPDATE orders SET value=902 WHERE id=9502; UPDATE payments SET value=902 WHERE id=9502')
        time.sleep(2);assert self.current()['epoch_id']==before
        p=self.policy();self.sync('resume',id=p['id'],expected_revision=p['revision'],key='resume');self.wait_value(9502,902)
        process=next(r for r in self.records() if r['role']=='capture-'+cap['id'])
        os.kill(process['pid'],signal.SIGKILL)
        self.sql(self.parent,'UPDATE orders SET value=903 WHERE id=9503; UPDATE payments SET value=903 WHERE id=9503')
        self.wait_value(9503,903);self.stop();self.start();self.healthy()
        assert self.status(cap)['bootstrap_id']==bootstrap
        self.check('continuous_pause_sigkill_and_daemon_restart_keep_durable_checkpoint')
        before=self.current()['epoch_id'];self.sql(self.parent,'ALTER TABLE orders ADD COLUMN unsupported int')
        wait(lambda:self.policy()['continuous_status']['state']=='blocked')
        assert self.current()['epoch_id']==before
        self.delete_capture(cap);self.sql(self.parent,'ALTER TABLE orders DROP COLUMN unsupported')
        p=self.policy();replacement=self.capture('start',policy_id=p['id'],expected_revision=p['revision'],key='resync',limits=dict(spool_bytes=512*1024*1024,wal_bytes=512*1024*1024))
        self.sync('resume',id=p['id'],expected_revision=p['revision'],key='resync-resume');self.wait_value(9503,903)
        assert self.status(replacement)['bootstrap_id']!=bootstrap
        p=self.policy();self.sync('delete',id=p['id'],expected_revision=p['revision'],key='delete');self.state_is(replacement,'deleted')
        self.check('continuous_schema_drift_preserves_epoch_and_explicit_resync_recovers')
        self.stop()


class InstalledMaintenance(Installed,Maintenance):
    inject_compaction_fault=False


class InstalledSurfaces(Installed,Surfaces):pass


SUITES={'triggered':InstalledTriggered,'continuous':InstalledContinuous,'maintenance':InstalledMaintenance,'governed':InstalledSurfaces}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--suite',choices=SUITES,required=True)
    args=parser.parse_args();release=args.release.resolve()
    root=Path(tempfile.mkdtemp(prefix='sy08-',dir='/tmp')).resolve();root.chmod(0o700)
    cell=SUITES[args.suite](release,root)
    identity=json.loads(subprocess.check_output([str(cell.binary),'installation','verify'],text=True))['identity']
    report=dict(status='FAIL',suite=args.suite,exact_installed=True,release_identity=identity,
        binary_sha256=sha(cell.binary),checks=cell.checks,metrics=cell.metrics,
        host=dict(system=platform.system(),machine=platform.machine(),cpu_count=os.cpu_count(),memory_bytes=psutil.virtual_memory().total))
    try:
        cell.run(release/'python/analytics/python',release/'python/analytics/export.py')
        assert json.loads(subprocess.check_output([str(cell.binary),'installation','verify'],text=True))['identity']==identity
        report['status']='PASS'
    finally:
        if (root/'control.sock').exists():
            try:cell.stop()
            except Exception:report.update(status='FAIL',cleanup='failed')
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='PASS':shutil.rmtree(root)
        else:print('Private installed sync diagnostics:',root,flush=True)
    assert report['status']=='PASS'


if __name__=='__main__':main()
