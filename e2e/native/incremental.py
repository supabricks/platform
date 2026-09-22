#!/usr/bin/env python3
"""SY03 real capture/application, atomic group epochs and pinned Sail readers."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from capture import Capture
from cell import wait,lsn

class Incremental(Capture):
    def applied(self,cap,key):
        r=self.cli('sync','apply',cap['id'],'--key',key)
        def done():
            current=self.cli('sync','applied',r['id'])
            return current if current['state'] in ('succeeded','failed','cancelled') else False
        r=wait(done,timeout=150)
        assert r['state']=='succeeded',r
        return r,self.api('get_snapshot',id=r['epoch_id'])['publication']
    def caught(self,cap,previous):
        return wait(lambda:(c if (c:=self.status(cap))['captured_lsn'] and lsn(c['captured_lsn'])>lsn(previous) else False))
    def version_rows(self,publication,name):
        d=publication['descriptor'];table=next(t for t in d['manifest']['tables'] if t['name']==name)
        path=self.root/d['generation']/table['path']
        code="import sys,json;from deltalake import DeltaTable;import pyarrow.fs as f;p=sys.argv[1];print(json.dumps(DeltaTable(p,version=int(sys.argv[2])).to_pyarrow_table(filesystem=f.SubTreeFileSystem(p,f.LocalFileSystem())).to_pylist(),default=str))"
        return json.loads(subprocess.check_output([str(self.python),'-c',code,str(path),str(table['version'])],text=True))
    def run(self,python,worker):
        self.python=python;self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{self.project}"\nname = "incremental-epochs"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        self.parent=self.create('main');self.configure(worker)
        self.sql(self.parent,"CREATE TABLE orders(id int PRIMARY KEY,amount numeric(38,8),note text); CREATE TABLE payments(id int PRIMARY KEY); CREATE TABLE unchanged(id int PRIMARY KEY); ALTER TABLE orders ALTER COLUMN note SET STORAGE EXTERNAL; INSERT INTO orders VALUES(1,12345678901234567890.12345678,repeat('toast-',3000)); INSERT INTO payments VALUES(1); INSERT INTO unchanged VALUES(1)")
        # Existing v1 content is retained byte-for-byte while a new v2 root advances.
        full,old=self.publish();old_generation=self.root/old['descriptor']['generation']
        original={str(p.relative_to(old_generation)):p.read_bytes() for p in old_generation.rglob('*') if p.is_file()}
        p=self.sync('create',branch='main',key='policy',config=dict(mode='snapshot',strategy='full',schedule=None))
        cap=self.begin_capture(p,'capture');ready=self.state_is(cap,'capturing')
        first_run,first=self.applied(cap,'bootstrap')
        assert first['descriptor']['format_version']==2 and first_run['applied_lsn']==ready['bootstrap_lsn']
        assert self.cli('sync','apply',cap['id'],'--key','bootstrap')['id']==first_run['id']
        reader=self.opened(epoch=first_run['epoch_id'],ttl_ms=600000)
        assert self.query(reader,'SELECT count(*) FROM public.orders')['rows']==[['1']]
        self.check('bootstrap_epoch_is_explicit_idempotent_and_readable_by_sail')
        self.sql(self.parent,"BEGIN; UPDATE orders SET id=2,amount=12345678901234567891.12345678 WHERE id=1; DELETE FROM payments WHERE id=1; INSERT INTO payments VALUES(2); COMMIT")
        self.caught(cap,ready['captured_lsn'])
        second_run,second=self.applied(cap,'changes')
        rows=self.version_rows(second,'orders');assert rows==[dict(id=2,amount='12345678901234567891.12345678',note='toast-'*3000)],rows
        assert self.version_rows(second,'payments')==[dict(id=2)]
        assert self.version_rows(first,'payments')==[dict(id=1)]
        assert next(t for t in second['descriptor']['manifest']['tables'] if t['name']=='unchanged')['version']==0
        assert self.query(reader,'SELECT id FROM public.payments')['rows']==[['1']]
        latest=self.opened(epoch=second_run['epoch_id'],ttl_ms=600000)
        assert self.query(latest,'SELECT id FROM public.payments')['rows']==[['2']]
        assert original=={str(p.relative_to(old_generation)):p.read_bytes() for p in old_generation.rglob('*') if p.is_file()}
        self.check('multi_table_iud_key_move_toast_decimal_and_v1_immutability_with_pinned_readers')
        self.close(latest)
        # Crash before publication at a deterministic real-worker boundary.
        previous=self.status(cap)['captured_lsn']
        self.sql(self.parent,"BEGIN; INSERT INTO orders VALUES(3,3,'three'); INSERT INTO payments VALUES(3); COMMIT")
        self.caught(cap,previous)
        faultdir=self.root/'fault-runtime';faultdir.mkdir()
        # The platform clears environment. A qualification-only worker wrapper sets
        # a one-shot fault, then invokes the actual production worker on each retry.
        shutil.copy2(worker,faultdir/'export.py')
        (faultdir/'incremental_worker.py').write_text(f"import os,runpy,sys,signal\nfrom pathlib import Path\nsys.path.insert(0,{str(worker.parent)!r})\np=Path({str(faultdir/'once')!r})\nif not p.exists():\n p.touch();os.environ['SUPABRICKS_CAPTURE_FAILPOINT']='after_first_table'\nimport capture.spool\ndef crash(point):\n if os.environ.get('SUPABRICKS_CAPTURE_FAILPOINT')==point:os.kill(os.getpid(),signal.SIGKILL)\ncapture.spool.fault=crash\nrunpy.run_path({str(worker.with_name('incremental_worker.py'))!r},run_name='__main__')\n")
        # Capture already owns its real worker. Keep its sibling path available.
        shutil.copy2(worker.with_name('capture_worker.py'),faultdir/'capture_worker.py')
        for package in ('capture','incremental'):(faultdir/package).symlink_to(worker.parent/package,target_is_directory=True)
        self.configure(faultdir/'export.py')
        third_run,third=self.applied(cap,'crash-replay')
        assert third_run['attempts']==2,third_run
        assert sorted(r['id'] for r in self.version_rows(third,'payments'))==[2,3]
        assert self.query(reader,'SELECT id FROM public.payments')['rows']==[['1']]
        self.configure(worker)
        self.check('sigkill_after_first_delta_commit_reconciles_same_plan_without_partial_visibility')
        self.close(reader)
        self.stop();self.start();self.state_is(cap,'capturing')
        current=self.api('current_snapshot',branch='main')['publication'];assert current['epoch_id']==third['epoch_id']
        reopened=self.opened(epoch=third_run['epoch_id'],ttl_ms=600000)
        assert self.query(reopened,'SELECT count(*) FROM public.payments')['rows']==[['2']]
        self.close(reopened)
        self.check('restart_verifies_v2_prefix_and_reopens_exact_published_versions')
        # Delete capture releases source/spool, while its published analytical roots
        # remain readable. Only a later full snapshot + history GC can retire them.
        self.delete_capture(cap)
        root=self.root/third['descriptor']['generation'];assert root.exists()
        lease=self.cli('analytics','pin',first_run['epoch_id'],'--ttl-ms','300000')
        replacement,full=self.publish()
        self.api('collect_snapshots',branch='main',keep=1)
        wait(lambda:self.api('get_snapshot',id=third_run['epoch_id'])['state']=='deleted')
        assert root.exists() and self.version_rows(first,'payments')==[dict(id=1)]
        self.cli('analytics','unpin',lease['id'])
        self.api('collect_snapshots',branch='main',keep=1)
        wait(lambda:not root.exists())
        self.check('shared_root_retained_for_pinned_history_then_collected_after_last_reference')
        self.stop()

def main():
    p=argparse.ArgumentParser()
    for name in ('binary','bundle','helpers','python','worker','report'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();root=Path(tempfile.mkdtemp(prefix='sb-sy03-',dir='/tmp')).resolve();root.chmod(0o700)
    cell=Incremental(a.binary.resolve(),a.bundle.resolve(),a.helpers.resolve(),root);report=dict(status='FAIL',checks=cell.checks)
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
