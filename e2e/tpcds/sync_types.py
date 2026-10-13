#!/usr/bin/env python3
"""Installed DATE/CHAR correctness with separately packaged type slices."""
from datetime import date
from decimal import Decimal
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
from cell import wait
sys.path.insert(0,str(Path(__file__).resolve().parent))
from composite import CompositeChecks
from inputs import sha
from type_cases import cases


class TypeChecks(CompositeChecks):
    def source(self):
        conn=super().source();conn.execute("SET DateStyle='ISO,YMD'");return conn
    def same(self,publication,names):
        with self.source() as db:
            for name in names:
                cursor=db.execute('SELECT * FROM '+name);cols=[c.name for c in cursor.description]
                expected=[dict(zip(cols,[str(v) if isinstance(v,(date,Decimal)) else v for v in row])) for row in cursor.fetchall()]
                actual=self.version_rows(publication,name)
                key=lambda r:json.dumps(r,sort_keys=True)
                assert sorted(expected,key=key)==sorted(actual,key=key),name
    def run_type(self,kind):
        spec=cases(kind)
        native=[]
        if kind=='char':
            path=Path(__file__).resolve().parents[2]/'docs/architecture/tpcds-evidence/2026-10-06/inventory.json'
            expected=next(line.split()[0] for line in path.with_name('SHA256SUMS').read_text().splitlines() if line.split()[1]=='inventory.json')
            assert sha(path)==expected
            native=json.loads(path.read_text())['tables'];assert len(native)==24
        ddl=spec['ddl']+';'+spec['insert']
        if native:ddl+=';'+';'.join(t['ddl'] for t in native)
        if kind=='date':ddl+=";ALTER DATABASE postgres SET DateStyle TO 'SQL, DMY'"
        self.setup_source(str(self.release/'python/analytics/python'),self.release/'python/analytics/export.py',ddl)
        p=self.cli('sync','create','--branch','main','--mode','continuous','--key',kind);self.policy_id=p['id'];cap=dict(id=p['capture_id'])
        self.healthy();first=self.current();bootstrap=self.status(cap)['bootstrap_id'];self.same(first,['typed']+[t['name'] for t in native])
        if native:self.check('all_24_native_tpcds_schemas_bootstrap_with_date_char_and_composite_keys')
        reader=self.opened(epoch=first['epoch_id'],ttl_ms=600000)
        self.metrics['bootstrap_queries']=self.type_queries(reader,kind,'bootstrap')
        self.postgres_queries(reader,kind)
        self.check(kind+'_native_bootstrap_types_nulls_bounds_and_sail_queries')
        sql=spec['update']
        boundary=self.transaction_at(sql);current=self.drain(boundary);self.same(current,['typed'])
        assert self.type_queries(reader,kind,'bootstrap')==self.metrics['bootstrap_queries']
        latest=self.opened(epoch=current['epoch_id'],ttl_ms=600000)
        self.metrics['updated_queries']=self.type_queries(latest,kind,'updated')
        self.postgres_queries(latest,kind)
        self.close(latest);self.close(reader)
        self.check(kind+'_insert_update_delete_key_move_and_old_epoch_immutability')
        process=next(r for r in self.records() if r['role']=='capture-'+cap['id']);os.kill(process['pid'],signal.SIGKILL)
        boundary=self.transaction_at('UPDATE typed SET amount=4.75 WHERE id=3');self.same(self.drain(boundary),['typed'])
        self.stop();self.start();self.healthy();assert self.status(cap)['bootstrap_id']==bootstrap
        boundary=self.transaction_at('UPDATE typed SET amount=5.25 WHERE id=3');self.same(self.drain(boundary),['typed'])
        self.check(kind+'_capture_sigkill_and_daemon_restart_replay')
        if kind=='date':
            last=self.current()['epoch_id']
            self.transaction_at("INSERT INTO typed VALUES(99,'infinity',0,'unsupported')")
            wait(lambda:self.policy()['continuous_status']['state']=='blocked',timeout=90)
            assert self.current()['epoch_id']==last
            self.check('infinite_date_fails_closed_without_publishing_or_losing_previous_epoch')
        p=self.policy();self.sync('delete',id=p['id'],expected_revision=p['revision'],key='delete');self.state_is(cap,'deleted')
        self.stop()
    def type_queries(self,reader,kind,phase):
        reference=json.loads(Path(__file__).with_name('type_reference.json').read_text())
        assert reference['status']=='PASS' and reference['version']=='4.2.0'
        assert [q['sql'] for q in reference['kinds'][kind][phase]]==cases(kind)['queries']
        assert reference['fixture_sha256']==sha(Path(__file__).with_name('type_cases.py'))
        results=[]
        for expected in reference['kinds'][kind][phase]:
            sql=expected['sql'];result=self.query(reader,sql);assert not result['truncated'],sql
            actual=dict(sql=sql,types=[c['type'] for c in result['columns']],rows=result['rows'])
            assert actual==expected,dict(sql=sql,expected=expected,actual=actual)
            results.append(actual)
        if kind=='date':assert results[0]['types'][1]=='date'
        return results

    def postgres_queries(self,reader,kind):
        # Dialect-shared semantics only. Spark length(CHAR) and casts to STRING
        # intentionally differ from PostgreSQL's length(bpchar)/casts to TEXT.
        queries=([cases(kind)['queries'][i] for i in (0,2,3)] if kind=='date' else
            [cases(kind)['queries'][i] for i in (0,3)]+[
             "SELECT id,c='x',c='x ',c=d,c IS NULL FROM public.typed ORDER BY id"])
        with self.source() as db:
            for sql in queries:
                expected=[[None if v is None else str(v) for v in row] for row in db.execute(sql).fetchall()]
                actual=self.query(reader,sql);assert actual['rows']==expected and not actual['truncated'],sql
            if kind=='char':
                sql="SELECT id,length(c),CAST(c AS TEXT)='x',c IN ('x',NULL),c IN (SELECT d FROM public.typed) FROM public.typed ORDER BY id"
                self.metrics.setdefault('postgres_dialect_results',[]).append(dict(sql=sql,
                    rows=[[None if v is None else str(v) for v in row] for row in db.execute(sql).fetchall()]))
        self.metrics.setdefault('postgres_shared_queries',[]).append(queries)


def main():
    from installed_sync import InstalledContinuous
    class Fixture(TypeChecks,InstalledContinuous):pass
    p=argparse.ArgumentParser();p.add_argument('--release',type=Path,required=True);p.add_argument('--report',type=Path,required=True);p.add_argument('--kind',choices=['date','char'],required=True);a=p.parse_args()
    if a.report.exists():raise ValueError('fresh report required')
    root=Path(tempfile.mkdtemp(prefix='eq171-')).resolve();root.chmod(0o700);cell=Fixture(a.release.resolve(),root)
    report=dict(status='FAIL',kind=a.kind,scope='installed engineering type qualification; separate reference comparison required',checks=cell.checks,metrics=cell.metrics,release_identity=sha(a.release/'release.json'),fixture_sha256=sha(Path(__file__)))
    started=time.monotonic()
    try:
        verified=json.loads(subprocess.check_output([str(cell.binary),'installation','verify'],text=True));assert verified['verified'] and verified['identity']==report['release_identity']
        cell.run_type(a.kind);report['status']='PASS'
    except BaseException as error:report['error']=str(error);raise
    finally:
        report['elapsed_seconds']=time.monotonic()-started;a.report.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
