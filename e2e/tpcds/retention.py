"""Installed PG slot retention under pressure and a pinned long transaction.

A bounded correctness fixture, not a throughput benchmark. It uses the existing
32-MiB minimum WAL profile to exercise pressure without changing production limits.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
from installed_sync import InstalledContinuous
from cell import lsn,wait

WAL_BYTES=32*1024**2


class Retention(InstalledContinuous):
    def run(self,python,worker):
        self.setup_source(python,worker,"CREATE TABLE orders(id int PRIMARY KEY,payload text); ALTER TABLE orders ALTER COLUMN payload SET STORAGE EXTERNAL")
        policy=self.sync('create',branch='main',key='retention',config=dict(mode='snapshot',strategy='full',schedule=None))
        cap=self.begin_capture(policy,'capture',wal=WAL_BYTES);self.state_is(cap,'capturing')
        self.applied(cap,'bootstrap');slot='sbcap_'+cap['id'].replace('-','')
        spool=self.root/'capture'/cap['id']/'spool/spool.sqlite3'
        transactions=0;next_key=1;observations=[];self.metrics.update(wal_limit_bytes=WAL_BYTES,slot_observations=observations)
        def journal():
            try:
                with sqlite3.connect(spool.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1) as db:
                    return db.execute('SELECT count(*),max(end_lsn) FROM transactions').fetchone()
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):return None
                raise
        def captured():
            def done():
                status=self.status(cap)
                assert status['state']=='capturing',status
                rows=journal()
                return rows if rows is not None and rows[0]==transactions else False
            return wait(done,timeout=30)
        def requested(count):
            def done():
                status=self.status(cap);assert status['state']=='capturing',status
                progress=(status.get('progress') or {}).get('source_slot') or {}
                marker=progress.get('restart_snapshot') or {}
                return marker if marker.get('requests',0)>=count else False
            return wait(done,timeout=30)
        with self.source() as db:
            def observe():
                row=db.execute("SELECT confirmed_flush_lsn::text,restart_lsn::text,pg_current_wal_flush_lsn()::text FROM pg_replication_slots WHERE slot_name=%s",(slot,)).fetchone()
                assert row is not None
                value=dict(at_ms=int(time.time()*1000),confirmed=row[0],restart=row[1],source=row[2],retained_bytes=lsn(row[2])-lsn(row[1]))
                observations.append(value);return value
            def insert(count=16):
                nonlocal transactions,next_key
                db.execute("INSERT INTO orders SELECT i,repeat('x',8192) FROM generate_series(%s::integer,%s::integer) i",(next_key,next_key+count-1))
                next_key+=count;transactions+=1
            def grow():
                before=lsn(db.execute('SELECT pg_current_wal_flush_lsn()::text').fetchone()[0])
                for _ in range(4096):
                    insert()
                    after=lsn(db.execute('SELECT pg_current_wal_flush_lsn()::text').fetchone()[0])
                    if after-before>=WAL_BYTES//3:return after-before
                raise AssertionError('bounded WAL fixture did not generate expected bytes')
            initial=observe();self.metrics['phase_wal_bytes']=[grow()]
            first=requested(1);insert(1);count,cut=captured()
            advanced=wait(lambda:(value if lsn((value:=observe())['restart'])>lsn(initial['restart']) else False),timeout=10)
            assert lsn(first['lsn'])<=lsn(advanced['confirmed'])<=int(cut,16)
            self.check('pressure_snapshot_advances_restart_only_after_durable_capture_feedback')
            with self.source() as long:
                before_long=lsn(db.execute('SELECT pg_current_wal_flush_lsn()::text').fetchone()[0])
                long.execute('BEGIN');long.execute("INSERT INTO orders VALUES(-1,'held')")
                self.metrics['phase_wal_bytes'].append(grow());requested(2);insert(1);captured()
                held=observe();assert lsn(held['restart'])<=before_long<lsn(held['confirmed']),held
                self.check('snapshot_request_preserves_the_open_transaction_restart_pin')
                long.execute('COMMIT');transactions+=1
            self.metrics['phase_wal_bytes'].append(grow());requested(3);insert(1);count,cut=captured()
            released=wait(lambda:(value if lsn((value:=observe())['restart'])>before_long else False),timeout=10)
            assert lsn(released['confirmed'])<=int(cut,16)
            assert int(db.execute("SELECT setting FROM pg_settings WHERE name='max_slot_wal_keep_size'").fetchone()[0])*1024**2==WAL_BYTES
            expected_count=db.execute('SELECT count(*) FROM orders').fetchone()[0]
        for attempt in range(20):
            run,publication=self.applied(cap,'final-'+str(attempt))
            if lsn(run['applied_lsn'])>=int(cut,16):break
        else:raise AssertionError('bounded complete-transaction apply did not drain')
        rows=self.version_rows(publication,'orders')
        assert len(rows)==expected_count==next_key
        assert {row['id'] for row in rows}=={-1,*range(1,next_key)}
        assert all(row['payload']==('held' if row['id']==-1 else 'x'*8192) for row in rows)
        self.metrics.update(exact_rows=len(rows),source_transactions=transactions,maximum_observed_retained_bytes=max(r['retained_bytes'] for r in observations))
        assert self.metrics['maximum_observed_retained_bytes']<WAL_BYTES*4//5
        self.check('released_transaction_and_every_committed_row_apply_exactly_with_unchanged_wal_cap')
        self.stop()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();release=args.release.resolve();args.output.mkdir(parents=True,exist_ok=False)
    root=Path(tempfile.mkdtemp(prefix='eq225-',dir='/tmp'));root.chmod(0o700)
    cell=Retention(release,root);report=dict(status='FAIL',scope=__doc__,release_identity=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),checks=cell.checks,metrics=cell.metrics)
    try:
        cell.run(release/'python/analytics/python',release/'python/analytics/export.py');report['status']='PASS'
    finally:
        if (root/'control.sock').exists():
            try:cell.stop()
            except Exception:report.update(status='FAIL',cleanup='failed')
        (args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='PASS':shutil.rmtree(root)
        else:print('Private retained fixture:',root,flush=True)
    assert report['status']=='PASS'


if __name__=='__main__':main()
