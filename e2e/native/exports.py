#!/usr/bin/env python3
"""Real frozen-compute exports, isolation, failure and owned cleanup on both targets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid
import urllib.request

import psycopg
from cell import Cell, wait


class Exports(Cell):
    def storage_metrics(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.config['ports']['ps_http']}/metrics",timeout=5) as response:
            lines=response.read().decode().splitlines()
        names=('pageserver_smgr_query_started_global_count','pageserver_smgr_query_seconds_global_sum',
               'pageserver_page_cache_read_hits_total','pageserver_page_cache_read_accesses_total')
        return {line.rsplit(' ',1)[0]:float(line.rsplit(' ',1)[1]) for line in lines
                if line.startswith(names)}

    def api(self, action, **fields):
        return self.request(method='api', api_version=1,
            binding=dict(project_id=self.project, worktree=str(self.work)),
            action=dict(action=action, **fields))

    def configure(self, worker):
        self.api('configure_analytics', python=str(self.python), worker=str(worker))

    def begin(self, **fields):
        return self.api('export', branch='main', key=str(uuid.uuid4()), **fields)

    def terminal(self, e, expected='complete'):
        def done():
            result = self.api('get_export', id=e['id'])
            return result if result['state'] in ('complete', 'failed', 'cancelled') else False
        result = wait(done, timeout=180)
        assert result['state'] == expected, result
        child = self.request(method='branch', id=e['child_id'])
        assert child['endpoint']['desired_state'] == 'deleted'
        assert child['observed_revision'] == child['revision']
        assert not any(r.get('branch_id') == e['child_id'] or r['role'] == 'export-'+e['id'] for r in self.records())
        assert not (self.root/'export-work'/e['id']).exists()
        with sqlite3.connect(self.root/'state.sqlite3') as db:
            assert db.execute('SELECT count(*) FROM credentials WHERE endpoint_id=?',(child['endpoint']['id'],)).fetchone()[0] == 0
        if expected != 'complete':
            assert not (self.root/'analytics/staging'/e['id']).exists()
        return result

    def manifest(self, result):
        path = Path(result['outcome']['manifest'])
        manifest = json.loads(path.read_text())
        assert manifest['published'] is False
        for entry in manifest['files']:
            file = path.parent/entry['path']
            assert file.stat().st_size == entry['bytes']
            assert hashlib.sha256(file.read_bytes()).hexdigest() == entry['sha256']
        return path.parent, manifest

    def values(self, generation, manifest, name):
        table = next(t for t in manifest['tables'] if t['name'] == name)
        code = '''import json,sys
from deltalake import DeltaTable
import pyarrow.fs as fs
path=sys.argv[1]
table=DeltaTable(path).to_pyarrow_table(filesystem=fs.SubTreeFileSystem(path,fs.LocalFileSystem()))
print(json.dumps(table.to_pylist(),default=str,sort_keys=True))
'''
        return json.loads(subprocess.check_output([str(self.python), '-W', 'error', '-c', code,
            str(generation/table['path'])], text=True, timeout=30))

    def run(self, python, worker):
        self.python = python
        self.work = self.root/'work'
        self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{self.project}"\nname = "exports"\n')
        self.start()
        self.request(method='register_project', config=dict(format_version=1,id=self.project,name='exports'))
        parent = self.create('main')
        self.parent = parent
        self.sql(parent, """
            CREATE TABLE orders(id int PRIMARY KEY, amount numeric(38,8), note text);
            ALTER TABLE orders OWNER TO supabricks_owner;
            GRANT INSERT,UPDATE,DELETE ON orders TO PUBLIC; GRANT UPDATE(note) ON orders TO PUBLIC;
            CREATE TABLE payments(id int PRIMARY KEY, amount numeric(38,8));
            INSERT INTO orders VALUES (1,123456789012345678901234567890.12345678,'before'),(2,2,'delete');
            INSERT INTO payments SELECT id,amount FROM orders;
            UPDATE orders SET note='updated' WHERE id=1;
            DELETE FROM orders WHERE id=2; DELETE FROM payments WHERE id=2;
            CREATE TABLE types(b bool,i2 smallint,i4 integer,i8 bigint,t text,v varchar(20),d date,ts timestamp,tz timestamptz);
            INSERT INTO types VALUES(true,-32768,2147483647,9223372036854775807,'héllo','world','2024-02-29','2024-02-29 12:34:56.123456','2024-02-29 12:34:56.123456+05');
            INSERT INTO types DEFAULT VALUES;
            CREATE TABLE empty(id integer);
            CREATE TABLE bulk(id int, payload text);
            INSERT INTO bulk SELECT i,repeat(md5(i::text),16) FROM generate_series(1,12000) i;
        """)
        self.sql(parent,"BEGIN; INSERT INTO orders VALUES(99,99,'rollback'); ROLLBACK;")
        # Hold a real uncommitted cross-table transaction across the captured boundary.
        txn = psycopg.connect(host='127.0.0.1',port=parent['ports']['sql'],user='cloud_admin',
            password=self.credentials(parent),dbname='postgres')
        txn.execute("INSERT INTO orders VALUES(3,3,'long'); INSERT INTO payments VALUES(3,3)")
        gate = self.root/'release-worker'
        paused = self.root/'paused.py'
        paused.write_text(f"import sys,time\nfrom pathlib import Path\nsys.path.insert(0,{str(worker.parent)!r})\nimport export\nwhile not Path({str(gate)!r}).exists(): time.sleep(.05)\nsys.exit(export.main())\n")
        self.configure(paused)
        baseline=[]
        for _ in range(5):
            start=time.monotonic(); self.sql(parent,'SELECT 1'); baseline.append(time.monotonic()-start)
        before = self.ps(parent)
        e = self.begin()
        wait(lambda: (self.root/'export-work'/e['id']/'input.json').exists())
        config = json.loads((self.root/'export-work'/e['id']/'input.json').read_text())
        assert config['source']['lsn']
        assert config['source']['export_timeline_id'] != parent['branch']['timeline_id']
        assert len(self.api('list_branches')['branches']) == 1
        for action, fields in [('get_branch',dict(branch=e['child_id'])), ('connect',dict(branch=e['child_id']))]:
            try: self.api(action, **fields)
            except RuntimeError: pass
            else: raise AssertionError('internal export branch exposed')
        try: self.begin()
        except RuntimeError: pass
        else: raise AssertionError('concurrent export admitted')
        child=self.request(method='branch',id=e['child_id'])
        assert self.sql(child,'SHOW shared_preload_libraries') == 'neon'
        assert self.sql(child,'SHOW max_logical_replication_workers') == '0'
        with psycopg.connect(host='127.0.0.1',port=config['port'],user=config['username'],
                password=config['password'],dbname='postgres',autocommit=True) as reader:
            assert reader.execute('SHOW default_transaction_read_only').fetchone()[0] == 'on'
            for statement in ['INSERT INTO orders VALUES(4,4,\'bad\')', 'SET default_transaction_read_only=off; INSERT INTO orders VALUES(4,4,\'bad\')']:
                try: reader.execute(statement)
                except psycopg.Error: pass
                else: raise AssertionError('exporter wrote a user table')
        txn.commit(); txn.close()
        self.sql(parent, """BEGIN; INSERT INTO orders VALUES(4,4,'after'); INSERT INTO payments VALUES(4,4); COMMIT;
            UPDATE orders SET note='after' WHERE id=1;
            ALTER TABLE orders ADD COLUMN later text; ALTER TABLE orders DROP COLUMN note;
            ALTER TABLE payments ALTER COLUMN amount TYPE text;
            CREATE TABLE later(id int); DROP TABLE empty;""")
        parent_scans_before=self.sql(parent,"SELECT seq_scan FROM pg_stat_user_tables WHERE relname='bulk'")
        storage_before=self.storage_metrics()
        gate.touch()
        latencies=[]
        while self.api('get_export',id=e['id'])['state'] not in ('complete','failed','cancelled'):
            start=time.monotonic(); self.sql(parent,'INSERT INTO later VALUES(1); SELECT 1'); latencies.append(time.monotonic()-start)
            time.sleep(.05)
        result=self.terminal(e)
        generation,manifest=self.manifest(result)
        assert len(manifest['tables']) == 5
        assert {(r['schema'],r['name']) for r in manifest['omitted_relations']
                if r['reason']=='engine control relation'} == {('public','health_check'),('neon_migration','migration_id')}
        assert self.values(generation,manifest,'orders') == [dict(id=1,amount='123456789012345678901234567890.12345678',note='updated')]
        assert self.values(generation,manifest,'payments') == [dict(id=1,amount='123456789012345678901234567890.12345678')]
        assert self.values(generation,manifest,'empty') == []
        typed=self.values(generation,manifest,'types')
        assert typed[0]['i8']==9223372036854775807 and typed[0]['t']=='héllo'
        assert typed[0]['tz']=='2024-02-29 07:34:56.123456+00:00'
        assert all(v is None for v in typed[1].values())
        assert not any(t['name']=='later' for t in manifest['tables'])
        bulk=next(t for t in manifest['tables'] if t['name']=='bulk')
        scan=next(s for s in manifest['export_compute_scans'] if s['oid']==bulk['oid'])
        assert bulk['rows']==12000 and scan['seq_tup_read']>=12000
        assert bulk['max_arrow_batch_bytes'] <= 8*1024*1024
        after=self.ps(parent)
        storage_after=self.storage_metrics()
        assert self.sql(parent,"SELECT seq_scan FROM pg_stat_user_tables WHERE relname='bulk'")==parent_scans_before
        self.checks.append(dict(name='frozen_snapshot',status='PASS',source=manifest['source'],
            parent_baseline_max_ms=round(max(baseline)*1000,3),
            shared_storage_counter_deltas={k:round(v-storage_before.get(k,0),6) for k,v in storage_after.items() if v != storage_before.get(k,0)},
            measurement_scope='whole pageserver during worker plus cleanup; parent probes include psql launch and writes',
            parent_probe_count=len(latencies),parent_probe_max_ms=round(max(latencies,default=0)*1000,3),
            parent_last_record_lsn_before=before['last_record_lsn'],parent_last_record_lsn_after=after['last_record_lsn'],
            export_compute_scans=manifest['export_compute_scans'],output_bytes=result['outcome']['bytes']))
        limited = self.root/'limited.py'
        limited.write_text(f"import sys\nsys.path.insert(0,{str(worker.parent)!r})\nimport export\nexport.MAX_BATCHES=2\nsys.exit(export.main())\n")
        self.configure(limited)
        assert 'metadata budget' in self.terminal(self.begin(),'failed')['outcome']['message']
        self.checks.append(dict(name='metadata_budget',status='PASS'))
        self.configure(worker)
        # Each failure must retire compute, lease, credentials and unpublished files.
        for name,ddl in [
            ('uuid','CREATE TABLE unsupported(v uuid)'),
            ('decimal_precision','CREATE TABLE unsupported(v numeric(39,2))'),
            ('unbounded_decimal','CREATE TABLE unsupported(v numeric)'),
            ('infinity',"CREATE TABLE unsupported(v timestamp); INSERT INTO unsupported VALUES('infinity')"),
            ('collation','CREATE TABLE unsupported(v text COLLATE "C")'),
            ('oversized_row',"CREATE TABLE unsupported(v text); INSERT INTO unsupported VALUES(repeat('x',300000))"),
            ('partition','CREATE TABLE unsupported(v int) PARTITION BY RANGE(v)'),
        ]:
            self.sql(parent,ddl)
            rejected=self.terminal(self.begin(), 'failed')
            self.sql(parent,'DROP TABLE unsupported')
            self.checks.append(dict(name=name,status='PASS',outcome=rejected['outcome']))
        self.sql(parent,"INSERT INTO bulk SELECT i,repeat(md5(i::text),16) FROM generate_series(12001,70000) i")
        self.terminal(self.begin(limits=dict(max_bytes=16*1024*1024,timeout_ms=120000)), 'failed')
        self.checks.append(dict(name='output_budget',status='PASS'))
        e=self.begin()
        wait(lambda:any((self.root/'analytics/staging'/e['id']).glob('*/*.parquet')))
        self.api('cancel_export',id=e['id']); self.terminal(e,'cancelled')
        self.checks.append(dict(name='cancel_streaming',status='PASS'))
        gate.unlink(); self.configure(paused)
        e=self.begin(); wait(lambda:(self.root/'export-work'/e['id']/'input.json').exists())
        self.api('cancel_export',id=e['id']); self.terminal(e,'cancelled')
        self.checks.append(dict(name='cancel_running_worker',status='PASS'))
        e=self.begin(limits=dict(max_bytes=64*1024*1024,timeout_ms=10000))
        assert self.terminal(e,'failed')['outcome']['code']=='deadline'
        self.checks.append(dict(name='worker_deadline',status='PASS'))
        e=self.begin(); wait(lambda:any(r['role']=='export-'+e['id'] for r in self.records()))
        worker_pid=next(r['pid'] for r in self.records() if r['role']=='export-'+e['id'])
        os.kill(worker_pid,signal.SIGKILL)
        assert self.terminal(e,'failed')['outcome']['code']=='worker_exit'
        self.checks.append(dict(name='worker_exit_cleanup',status='PASS'))
        e=self.begin(); wait(lambda:any(r['role']=='export-'+e['id'] for r in self.records()))
        storage_pid=next(r['pid'] for r in self.records() if r['role']=='pageserver')
        os.kill(storage_pid,signal.SIGSTOP)
        try:
            self.api('cancel_export',id=e['id'])
            wait(lambda:not any(r['role']=='export-'+e['id'] for r in self.records()),timeout=20)
        finally:os.kill(storage_pid,signal.SIGCONT)
        self.terminal(e,'cancelled')
        self.checks.append(dict(name='cancel_while_storage_unavailable',status='PASS'))
        e=self.begin(); wait(lambda:any(r['role']=='export-'+e['id'] for r in self.records()))
        self.daemons[-1].kill(); self.daemons[-1].wait(timeout=10)
        self.start(); self.terminal(e,'failed')
        self.checks.append(dict(name='daemon_crash_cleanup',status='PASS'))
        self.configure(worker)
        assert self.sql(parent,'SELECT count(*) FROM orders')=='3'
        self.stop()


def main():
    parser=argparse.ArgumentParser()
    for name in ('binary','bundle','helpers','python','worker','report'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='sb-a01-'))
    cell=Exports(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    report=dict(status='FAIL',checks=cell.checks)
    try:
        cell.run(args.python.absolute(),args.worker.resolve())
        report['status']='PASS'
    finally:
        if (root/'control.sock').exists():
            try:cell.stop()
            except Exception:pass
        report['state_dir']=str(root)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if (root/'daemon.log').exists():
            args.report.with_suffix('.log').write_bytes((root/'daemon.log').read_bytes())
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
