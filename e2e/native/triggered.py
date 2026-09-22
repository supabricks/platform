#!/usr/bin/env python3
"""SY04: real source barriers, fixed targets, bounded runs and schedule recovery."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from incremental import Incremental
from cell import wait

class Triggered(Incremental):
    def finished(self,run):
        r=wait(lambda:(r if (r:=self.sync('run',id=run['id']))['state'] in ('succeeded','failed','cancelled') else False),timeout=240)
        assert r['state']=='succeeded',r
        assert r['source_lsn']==r['target_lsn'] and r['epoch_id'] and r['capture_id'] and not r['refresh_id'],r
        return r,self.api('get_snapshot',id=r['epoch_id'])['publication']
    def trigger(self,p,key):return self.sync('run_now',id=p['id'],expected_revision=p['revision'],key=key)
    def hold(self,worker):
        directory=self.root/'held-runtime';directory.mkdir(exist_ok=True);gate=directory/'hold';gate.touch()
        shutil.copy2(worker,directory/'export.py');shutil.copy2(worker.with_name('capture_worker.py'),directory/'capture_worker.py')
        for package in ('capture','incremental'):
            if not (directory/package).exists():(directory/package).symlink_to(worker.parent/package,target_is_directory=True)
        (directory/'incremental_worker.py').write_text(f"import runpy,sys,time\nfrom pathlib import Path\nsys.path.insert(0,{str(worker.parent)!r})\nwhile Path({str(gate)!r}).exists():time.sleep(.05)\nrunpy.run_path({str(worker.with_name('incremental_worker.py'))!r},run_name='__main__')\n")
        self.configure(directory/'export.py');return gate
    def run(self,python,worker):
        self.python=python;self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{self.project}"\nname = "triggered-sync"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        self.parent=self.create('main');self.configure(worker)
        self.sql(self.parent,"CREATE TABLE orders(id int PRIMARY KEY,note text); ALTER TABLE orders ALTER COLUMN note SET STORAGE EXTERNAL; CREATE TABLE payments(id int PRIMARY KEY); INSERT INTO orders VALUES(1,'one'); INSERT INTO payments VALUES(1)")
        p=self.cli('sync','create','--branch','main','--mode','triggered','--strategy','incremental','--key','policy')
        first=self.trigger(p,'first');cap=dict(id=first['capture_id']);assert p['capture_id']==cap['id']
        assert self.trigger(p,'first')['id']==first['id']
        self.failed_api('managed_snapshots',command=dict(kind='run_now',id=p['id'],expected_revision=1,key='overlap'))
        first,publication=self.finished(first);bootstrap=self.status(cap)['bootstrap_id']
        self.check('explicit_policy_enrollment_bootstrap_barrier_and_idempotent_run')
        reader=self.opened(epoch=first['epoch_id'],ttl_ms=600000)
        self.sql(self.parent,"BEGIN; INSERT INTO orders VALUES(2,'two'); INSERT INTO payments VALUES(2); COMMIT")
        long=self.source();long.execute('BEGIN');long.execute("INSERT INTO orders VALUES(3,'later'); INSERT INTO payments VALUES(3)")
        gate=self.hold(worker);r=self.trigger(p,'bounded')
        run_id=r['id']
        active=wait(lambda:(state if (state:=self.sync('run',id=run_id))['target_lsn'] and state['apply_id'] else False))
        target=active['target_lsn']
        long.execute('COMMIT');long.close()
        assert self.query(reader,'SELECT count(*) FROM public.payments')['rows']==[['1']]
        self.close(reader)
        self.stop();self.start()
        assert self.sync('run',id=run_id)['target_lsn']==target
        gate.unlink();self.configure(worker)
        r,current=self.finished(r);assert r['target_lsn']==target
        assert sorted(v['id'] for v in self.version_rows(current,'payments'))==[1,2]
        reader=self.opened(epoch=first['epoch_id'],ttl_ms=600000)
        assert self.query(reader,'SELECT count(*) FROM public.payments')['rows']==[['1']]
        self.close(reader)
        later,current=self.finished(self.trigger(p,'later'));assert len(self.version_rows(current,'payments'))==3
        self.check('restart_and_concurrent_later_writes_do_not_move_target_or_old_reader')
        unchanged,current2=self.finished(self.trigger(p,'no-change'))
        assert [t['version'] for t in current['descriptor']['manifest']['tables']]==[t['version'] for t in current2['descriptor']['manifest']['tables']]
        self.check('idle_source_barrier_finishes_no_change_run_without_rewriting_tables')
        # Exceed the production 16 MiB input budget with separate complete commits.
        for batch in range(12):
            self.sql(self.parent,f"INSERT INTO orders SELECT i,repeat('x',200000) FROM generate_series({100+batch*8},{107+batch*8}) i")
        large,current=self.finished(self.trigger(p,'multi-batch'))
        assert large['batches']>=2,large
        assert len(self.version_rows(current,'orders'))==99
        assert self.status(cap)['bootstrap_id']==bootstrap
        self.check('production_input_budget_splits_complete_transactions_without_new_full_export')
        config=dict(mode='triggered',strategy='incremental',schedule=dict(interval_seconds=60,timezone='UTC',missed_run='coalesce'))
        p=self.sync('update',id=p['id'],expected_revision=p['revision'],key='schedule',config=config)
        self.stop()
        with sqlite3.connect(self.root/'state.sqlite3') as db:
            record=json.loads(db.execute('SELECT record FROM sync_policies WHERE id=?',(p['id'],)).fetchone()[0]);record['next_due_at_ms']=int(time.time()*1000)-300000
            db.execute('UPDATE sync_policies SET record=? WHERE id=?',(json.dumps(record),p['id']))
        self.start()
        scheduled=wait(lambda:next((r for r in self.sync('runs',id=p['id']) if r['trigger']=='schedule'),False));scheduled,_=self.finished(scheduled)
        assert len([r for r in self.sync('runs',id=p['id']) if r['trigger']=='schedule'])==1
        p=self.cli('sync','pause',p['id'],'--revision',str(p['revision']),'--key','pause')
        self.sql(self.parent,"INSERT INTO payments VALUES(4)")
        self.failed_api('managed_snapshots',command=dict(kind='run_now',id=p['id'],expected_revision=p['revision'],key='paused'))
        p=self.cli('sync','resume',p['id'],'--revision',str(p['revision']),'--key','resume')
        resumed,current=self.finished(self.trigger(p,'resume-run'));assert len(self.version_rows(current,'payments'))==4
        assert self.status(cap)['bootstrap_id']==bootstrap
        self.check('restart_coalesces_schedule_and_idle_pause_resume_reuses_capture')
        gate=self.hold(worker);cancel=self.trigger(p,'cancel')
        wait(lambda:self.sync('run',id=cancel['id'])['apply_id'])
        self.sync('cancel',id=cancel['id'],key='cancel-request');assert self.sync('run',id=cancel['id'])['state']=='cancelled'
        assert self.api('current_snapshot',branch='main')['publication']['epoch_id']==resumed['epoch_id']
        gate.unlink();self.configure(worker)
        self.state_is(cap,'resync_required')
        self.sync('delete',id=p['id'],expected_revision=p['revision'],key='delete')
        self.state_is(cap,'deleted')
        self.check('cancel_fences_partial_work_and_policy_delete_releases_owned_capture')
        self.stop()

def main():
    p=argparse.ArgumentParser()
    for name in ('binary','bundle','helpers','python','worker','report'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();root=Path(tempfile.mkdtemp(prefix='sb-sy04-',dir='/tmp')).resolve();root.chmod(0o700)
    cell=Triggered(a.binary.resolve(),a.bundle.resolve(),a.helpers.resolve(),root);report=dict(status='FAIL',checks=cell.checks)
    try:cell.run(a.python.absolute(),a.worker.resolve());report['status']='PASS'
    finally:
        if (root/'control.sock').exists():
            try:cell.stop()
            except Exception:report.update(status='FAIL',cleanup='failed')
        if report['status']!='PASS':report['state_dir']=str(root)
        a.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='PASS' and 'cleanup' not in report:shutil.rmtree(root)
    print(json.dumps(report,indent=2));assert report['status']=='PASS',report
if __name__=='__main__':main()
