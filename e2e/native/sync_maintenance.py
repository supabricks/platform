#!/usr/bin/env python3
"""SY07 real PG capture, automatic Delta rollover, spool pruning and pinned Sail."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from incremental import Incremental
from cell import wait,lsn


class Maintenance(Incremental):
    def caught(self,cap,previous):
        def observed():
            current=self.status(cap)
            assert current['state']!='resync_required',current
            return current if current['captured_lsn'] and lsn(current['captured_lsn'])>lsn(previous) else False
        return wait(observed)

    def run(self,python,worker):
        self.python=python;self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{self.project}"\nname = "sync-maintenance"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        self.parent=self.create('main');self.configure(worker)
        self.sql(self.parent,'CREATE TABLE orders(id int PRIMARY KEY,value int); INSERT INTO orders VALUES(1,0)')
        policy=self.sync('create',branch='main',key='policy',config=dict(mode='snapshot',strategy='full',schedule=None))
        cap=self.begin_capture(policy,'capture');self.state_is(cap,'capturing')
        first_run,first=self.applied(cap,'bootstrap');old_root=self.root/first['descriptor']['generation']
        reader=self.opened(epoch=first_run['epoch_id'],ttl_ms=600000)
        assert self.query(reader,'SELECT value FROM public.orders')['rows']==[['0']]
        # A qualification-only wrapper crashes once after the real compaction
        # rename. Admission, threshold, root ownership and all SQL stay unmodified.
        faults=self.root/'fault-runtime';faults.mkdir()
        for name in ('export.py','capture_worker.py'):shutil.copy2(worker.parent/name,faults/name)
        for package in ('capture','incremental'):(faults/package).symlink_to(worker.parent/package,target_is_directory=True)
        (faults/'incremental_worker.py').write_text(f"import json,os,runpy,sys\nfrom pathlib import Path\nsys.path.insert(0,{str(worker.parent)!r})\nc=json.loads(Path(sys.argv[1]).read_text());once=Path({str(faults/'once')!r})\nif c.get('storage_generation') and not once.exists():\n once.touch();os.environ['SUPABRICKS_CAPTURE_FAILPOINT']='after_compaction_rename'\nrunpy.run_path({str(worker.with_name('incremental_worker.py'))!r},run_name='__main__')\n")
        self.configure(faults/'export.py')
        for value in range(1,66):
            before=self.status(cap)['captured_lsn']
            self.sql(self.parent,f'UPDATE orders SET value={value}')
            self.caught(cap,before);latest_run,latest=self.applied(cap,f'update-{value}')
        new_root=self.root/latest['descriptor']['generation']
        assert new_root!=old_root and latest_run['attempts']==2,latest_run
        assert latest['descriptor']['manifest']['compaction']['rows']==1
        assert self.version_rows(latest,'orders')==[dict(id=1,value=65)]
        assert self.query(reader,'SELECT value FROM public.orders')['rows']==[['0']]
        self.check('automatic_64_version_rollover_recovers_renamed_root_and_keeps_pinned_sail_epoch')
        self.configure(worker)
        with self.source() as db:
            for value in range(66,366):db.execute('UPDATE orders SET value=%s',(value,))
        wait(lambda:len(self.journal(cap))>=365,timeout=60)
        target=self.journal(cap)[-1][0]
        wait(lambda:lsn(self.status(cap)['captured_lsn'])>=target,timeout=30)
        last_run,last=self.applied(cap,'burst')
        assert self.version_rows(last,'orders')==[dict(id=1,value=365)]
        spool=self.root/'capture'/cap['id']/'spool/spool.sqlite3'
        def pruned():
            with sqlite3.connect(f'file:{spool}?mode=ro',uri=True) as db:
                return db.execute("SELECT value FROM metadata WHERE key='pruned_prefix'").fetchone()
        wait(pruned,timeout=30)
        assert len(self.journal(cap))<365
        self.stop();self.start();self.state_is(cap,'capturing')
        reopened=self.opened(epoch=last_run['epoch_id'],ttl_ms=600000)
        assert self.query(reopened,'SELECT value FROM public.orders')['rows']==[['365']]
        self.close(reopened)
        self.check('published_spool_prefix_reclaimed_and_capture_restarts_without_gap_or_duplicate')
        # Restart closes the old session, so use an explicit durable pin for GC.
        pin=self.cli('analytics','pin',first_run['epoch_id'],'--ttl-ms','300000')
        self.api('collect_snapshots',branch='main',keep=1)
        assert old_root.exists() and self.version_rows(first,'orders')==[dict(id=1,value=0)]
        self.cli('analytics','unpin',pin['id'])
        self.api('collect_snapshots',branch='main',keep=1)
        wait(lambda:not old_root.exists(),timeout=60)
        assert new_root.exists() and self.status(cap)['state']=='capturing'
        self.check('unpinned_old_generation_collected_while_capture_and_current_generation_remain_live')
        self.stop();shutil.rmtree(faults)
        with tempfile.TemporaryDirectory(prefix='sb-sy07-recovery-',dir='/tmp') as recovery:
            backup=Path(recovery)/'backup';restored=Path(recovery)/'restored'
            subprocess.check_output([str(self.binary),'backup','create',str(backup),'--data-dir',str(self.root)],timeout=180)
            subprocess.check_output([str(self.binary),'backup','restore',str(backup),'--data-dir',str(restored)],timeout=180)
            with sqlite3.connect(f'file:{restored}/state.sqlite3?mode=ro',uri=True) as db:
                assert db.execute('SELECT state FROM sync_captures WHERE id=?',(cap['id'],)).fetchone()==('resync_required',)
                assert db.execute('SELECT count(*) FROM sync_storage_roots WHERE id=?',(new_root.name,)).fetchone()==(1,)
            source_root=self.root
            try:
                self.root=restored
                assert self.version_rows(last,'orders')==[dict(id=1,value=365)]
            finally:self.root=source_root
            with sqlite3.connect(f'file:{restored}/capture/{cap["id"]}/spool/spool.sqlite3?mode=ro',uri=True) as db:
                assert db.execute("SELECT value FROM metadata WHERE key='pruned_prefix'").fetchone()==pruned()
        self.check('stopped_backup_restores_compacted_epoch_and_pruned_spool_with_capture_fenced')
        self.start();self.state_is(cap,'capturing')
        self.delete_capture(cap)
        assert self.version_rows(last,'orders')==[dict(id=1,value=365)]
        self.check('source_retirement_removes_slot_and_spool_without_deleting_retained_compacted_epoch')
        self.stop()


def main():
    parser=argparse.ArgumentParser()
    for name in ('binary','bundle','helpers','python','worker','report'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();root=Path(tempfile.mkdtemp(prefix='sb-sy07-',dir='/tmp')).resolve();root.chmod(0o700)
    cell=Maintenance(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    report=dict(status='FAIL',checks=cell.checks)
    try:cell.run(args.python.absolute(),args.worker.resolve());report['status']='PASS'
    finally:
        if (root/'control.sock').exists():
            try:cell.stop()
            except Exception:report.update(status='FAIL',cleanup='failed')
        if report['status']!='PASS':report['state_dir']=str(root)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='PASS' and 'cleanup' not in report:shutil.rmtree(root)
    print(json.dumps(report,indent=2));assert report['status']=='PASS',report


if __name__=='__main__':main()
