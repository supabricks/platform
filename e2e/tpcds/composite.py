#!/usr/bin/env python3
"""EQ01 installed composite-key correctness and unchanged scalar control screen."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
from cell import wait,lsn
sys.path.insert(0,str(Path(__file__).resolve().parent))
from inputs import LOCK,inventory,sha


class CompositeChecks:
    def run(self,python,worker):
        self.run_composite(None)
    def drain(self,target):
        def ready():
            self.healthy();current=self.current()
            return current if lsn(current['descriptor']['manifest']['source']['lsn'])>=lsn(target) else False
        return wait(ready,timeout=90)
    def transaction_at(self,sql):
        with self.source() as db:
            with db.transaction():
                db.execute(sql)
                boundary=db.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone()[0]
        return boundary
    def equal(self,publication,names):
        with self.source() as db:
            for name in names:
                cursor=db.execute('SELECT * FROM '+name)
                cols=[c.name for c in cursor.description]
                expected=[dict(zip(cols,[str(v) if isinstance(v,Decimal) else v for v in row])) for row in cursor.fetchall()]
                actual=self.version_rows(publication,name)
                key=lambda row:json.dumps(row,sort_keys=True)
                assert sorted(expected,key=key)==sorted(actual,key=key),name
    def run_control(self):
        self.setup_source(str(self.release/'python/analytics/python'),self.release/'python/analytics/export.py',
            'CREATE TABLE orders(id integer PRIMARY KEY,value integer); CREATE TABLE payments(id integer PRIMARY KEY,value integer); INSERT INTO orders SELECT i,0 FROM generate_series(1,10000)i; INSERT INTO payments SELECT i,0 FROM generate_series(1,10000)i')
        p=self.cli('sync','create','--branch','main','--mode','continuous','--key','control');self.policy_id=p['id']
        self.healthy();samples=[]
        with self.source() as db:
            for n in range(12):
                started=time.perf_counter()
                with db.transaction():
                    db.execute('UPDATE orders SET value=%s WHERE id BETWEEN %s AND %s',(n+1,n*32+1,n*32+32))
                    db.execute('UPDATE payments SET value=%s WHERE id BETWEEN %s AND %s',(n+1,n*32+1,n*32+32))
                    boundary=db.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone()[0]
                ack=time.perf_counter();self.drain(boundary)
                samples.append(dict(changed_rows=64,commit_ms=(ack-started)*1000,
                                    ack_to_observed_publication_ms=(time.perf_counter()-ack)*1000))
        self.equal(self.current(),['orders','payments'])
        self.check('unchanged_scalar_control_exact_equality')
        self.metrics['control_samples']=samples
        self.stop()
    def run_composite(self,inputs):
        if inputs is None:
            # Release qualification is offline. Use the complete pinned EQ00
            # schema inventory, verifying its retained receipt before use.
            path=Path(__file__).resolve().parents[2]/'docs/architecture/tpcds-evidence/2026-10-06/inventory.json'
            expected=next(line.split()[0] for line in path.with_name('SHA256SUMS').read_text().splitlines() if line.split()[1]=='inventory.json')
            assert sha(path)==expected
            manifest=json.loads(path.read_text())
        else:manifest=inventory(json.loads(LOCK.read_text()),inputs)
        native=[t for t in manifest['tables'] if len(t['primary_key'])>1]
        assert len(native)==7
        ddl=';'.join(t['ddl'] for t in native)
        ddl+=''';CREATE TABLE pairs(a bigint,amount decimal(18,2),b smallint,note text,PRIMARY KEY(b,a) INCLUDE(amount));
          CREATE TABLE triples(a integer,b integer,c integer,value integer,PRIMARY KEY(c,a,b));
          CREATE TABLE scalar(id integer PRIMARY KEY,value decimal(18,2));
          ALTER TABLE pairs ALTER COLUMN note SET STORAGE EXTERNAL;
          INSERT INTO pairs VALUES(1,1.25,1,repeat('toast-',3000)),(1,2.50,2,'neighbor'),(2,3.75,1,'neighbor'),(2,5.00,2,'delete');
          INSERT INTO triples VALUES(1,1,1,1),(1,1,2,2),(1,2,1,3);
          INSERT INTO scalar VALUES(1,1234567890123456.78)'''
        self.setup_source(str(self.release/'python/analytics/python'),self.release/'python/analytics/export.py',ddl)
        p=self.cli('sync','create','--branch','main','--mode','continuous','--key','composite');self.policy_id=p['id'];cap=dict(id=p['capture_id'])
        self.healthy();first=self.current();bootstrap=self.status(cap)['bootstrap_id']
        names=['pairs','triples','scalar']+[t['name'] for t in native]
        self.equal(first,names);reader=self.opened(epoch=first['epoch_id'],ttl_ms=600000)
        self.check('native_seven_tpcds_composite_schemas_and_reordered_include_keys_bootstrap')
        # Source rejects duplicate complete identities but permits shared prefixes.
        import psycopg
        with self.source() as db:
            try:db.execute('INSERT INTO pairs VALUES(1,0,1,\'duplicate\')')
            except psycopg.errors.UniqueViolation:pass
            else:raise AssertionError('duplicate complete key accepted')
        boundary=self.transaction_at('''UPDATE pairs SET b=3,amount=6.25 WHERE a=1 AND b=1;
            DELETE FROM pairs WHERE a=2 AND b=2;
            INSERT INTO pairs VALUES(1,NULL,4,'new'),(-9223372036854775808,1.00,-32768,'minimum'),(9223372036854775807,2.00,32767,'maximum');
            INSERT INTO pairs VALUES(8,8,8,'temporary'); UPDATE pairs SET a=9 WHERE a=8 AND b=8; DELETE FROM pairs WHERE a=9 AND b=8;
            UPDATE triples SET c=3,value=9 WHERE a=1 AND b=1 AND c=1;
            DELETE FROM triples WHERE a=1 AND b=2 AND c=1;
            UPDATE scalar SET value=1234567890123456.79 WHERE id=1''')
        current=self.drain(boundary);self.equal(current,names)
        assert len(self.version_rows(first,'pairs'))==4
        assert self.query(reader,'SELECT count(*) FROM public.pairs')['rows']==[['4']]
        latest=self.opened(epoch=current['epoch_id'],ttl_ms=600000)
        result=self.query(latest,'SELECT a,b,amount FROM public.pairs WHERE a=1 ORDER BY b')
        assert result['rows']==[['1','2','2.50'],['1','3','6.25'],['1','4',None]],result
        self.close(latest);self.close(reader)
        self.check('continuous_iud_all_key_components_toast_decimal_bounds_duplicates_and_pinned_sail')
        process=next(r for r in self.records() if r['role']=='capture-'+cap['id'])
        os.kill(process['pid'],signal.SIGKILL)
        boundary=self.transaction_at('UPDATE pairs SET a=3 WHERE a=1 AND b=3; UPDATE triples SET a=2,b=2 WHERE a=1 AND b=1 AND c=3')
        self.equal(self.drain(boundary),names)
        self.stop();self.start();self.healthy()
        assert self.status(cap)['bootstrap_id']==bootstrap
        boundary=self.transaction_at('DELETE FROM pairs WHERE a=3 AND b=3; INSERT INTO triples VALUES(2,2,4,10)')
        self.equal(self.drain(boundary),names)
        self.check('capture_sigkill_and_daemon_restart_preserve_composite_identity')
        p=self.policy();self.sync('delete',id=p['id'],expected_revision=p['revision'],key='delete');self.state_is(cap,'deleted')
        self.stop()


from installed_sync import InstalledContinuous


class Composite(CompositeChecks,InstalledContinuous):pass


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    p.add_argument('--inputs',type=Path);p.add_argument('--control-only',action='store_true');a=p.parse_args()
    if a.report.exists():raise ValueError('fresh report required')
    root=Path(tempfile.mkdtemp(prefix='eq01-')).resolve();root.chmod(0o700)
    cell=Composite(a.release.resolve(),root)
    report=dict(status='FAIL',scope='EQ01 engineering overlay qualification; not a signed release or sustained SP trial',checks=cell.checks,metrics=cell.metrics,
        release_identity=sha(a.release/'release.json'),fixture_sha256=sha(Path(__file__)),control_only=a.control_only)
    started=time.monotonic()
    try:
        verified=json.loads(subprocess.check_output([str(cell.binary),'installation','verify'],text=True))
        assert verified['verified'] and verified['identity']==report['release_identity']
        if a.control_only:cell.run_control()
        else:cell.run_composite(a.inputs)
        report['status']='PASS'
    except BaseException as error:
        report['error']=str(error);raise
    finally:
        report['elapsed_seconds']=time.monotonic()-started
        a.report.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
