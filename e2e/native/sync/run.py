#!/usr/bin/env python3
"""SY00 real PG17/Neon capture probe; exclusively creates disposable fixture state."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import math
import resource
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

import psycopg
from psycopg import sql

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from branches import BranchCell
from cell import wait, process_tree, assert_stopped
from prepare import verify
from protocol import Decoder, Model, Unsupported, UNCHANGED, lsn, format_lsn


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def distribution(values):
    ordered = sorted(values)
    return {f'p{p}_ms': round(ordered[min(len(ordered)-1, math.ceil(len(ordered)*p/100)-1)], 3)
            for p in (50, 95, 99)} | {'samples': len(values)}


class Probe(BranchCell):
    def __init__(self, *args):
        super().__init__(*args)
        self.connections = ExitStack()
        self.metrics = {}

    def check(self, name, condition, **facts):
        if not condition:
            raise AssertionError(name)
        self.checks.append(dict(name=name, status='PASS', **facts))
        print('PASS ' + name, flush=True)

    def db(self, branch, user='cloud_admin', password=None, autocommit=True):
        connection = psycopg.connect(host='127.0.0.1', port=branch['ports']['sql'],
            user=user, password=password or self.credentials(branch), dbname='postgres',
            connect_timeout=5, autocommit=autocommit,
            options='-c statement_timeout=15000 -c lock_timeout=5000 -c timezone=UTC -c datestyle=ISO,YMD')
        self.connections.callback(connection.close)
        return connection

    def metadata(self, db):
        rows = db.execute("""SELECT c.oid,n.nspname,c.relname,c.relreplident,a.attname,a.atttypid,a.atttypmod,
                EXISTS(SELECT 1 FROM pg_index i WHERE i.indrelid=c.oid AND i.indisprimary AND a.attnum=ANY(i.indkey))
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
            WHERE n.nspname='public' AND c.relname IN ('orders','payments') AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY c.oid,a.attnum""").fetchall()
        relations = {}
        for oid, namespace, name, identity, column, type_oid, modifier, key in rows:
            if oid not in relations:
                relations[oid] = [namespace, name, identity, []]
            relations[oid][3].append((int(key), column, type_oid, modifier))
        return {oid: (n, name, identity, tuple(columns)) for oid, (n, name, identity, columns) in relations.items()}

    def rows(self, db, expected):
        result = {}
        for oid, (namespace, name, _, columns) in expected.items():
            fields = sql.SQL(',').join(sql.SQL('{}::text').format(sql.Identifier(c[1])) for c in columns)
            rows = db.execute(sql.SQL('SELECT {} FROM {}.{} ORDER BY id').format(
                fields, sql.Identifier(namespace), sql.Identifier(name))).fetchall()
            result[oid] = {(row[0],): list(row) for row in rows}
        return result

    def peek(self, db):
        # Non-consuming SQL interface: SQL transport is qualified, streaming is not.
        return [bytes(row[0]) for row in db.execute("""SELECT data FROM
            pg_logical_slot_peek_binary_changes('sy00_slot',NULL,10000,
            'proto_version','1','publication_names','sy00_pub','messages','true')""").fetchall()]

    def barrier(self, db):
        return db.execute("SELECT pg_logical_emit_message(true,'supabricks.sy00.barrier','cut')::text").fetchone()[0]

    def identity(self, branch):
        return (self.project, branch['branch']['tenant_id'], branch['branch']['timeline_id'], branch['branch']['id'])

    def run(self, release, analytical_python):
        self.start()
        self.request(method='register_project', config=dict(format_version=1, id=self.project, name='sy00'))
        parent = self.create('main')
        db = self.db(parent)
        settings = dict(db.execute("SELECT name,setting FROM pg_settings WHERE name=ANY(%s)", ([
            'server_version_num','wal_level','max_replication_slots','max_wal_senders','fsync',
            'max_slot_wal_keep_size','logical_decoding_work_mem','max_prepared_transactions',
            'wal_sender_timeout'],)).fetchall())
        self.check('shipped_pg17_logical_capture_settings', settings['server_version_num'] == '170008'
                   and settings['wal_level'] == 'logical' and settings['fsync'] == 'on', settings=settings)
        db.execute("""CREATE TABLE orders(id integer PRIMARY KEY, amount numeric(38,8), note text);
            CREATE TABLE payments(LIKE orders INCLUDING ALL);
            INSERT INTO orders SELECT i,i::numeric,'seed' FROM generate_series(1,1000) i;
            UPDATE orders SET note=(SELECT string_agg(md5(i::text),'') FROM generate_series(1,500) i) WHERE id=4;
            INSERT INTO payments SELECT * FROM orders;
            CREATE FUNCTION sy00_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$ DECLARE relevant boolean; BEGIN
                IF TG_EVENT='sql_drop' THEN
                    SELECT EXISTS(SELECT 1 FROM pg_event_trigger_dropped_objects()
                        WHERE schema_name='public' AND object_type IN ('table','table column','index')
                        AND object_name IS DISTINCT FROM 'health_check') INTO relevant;
                ELSE
                    SELECT EXISTS(SELECT 1 FROM pg_event_trigger_ddl_commands()
                        WHERE schema_name='public' AND object_type IN ('table','table column','index')
                        AND object_identity <> 'public.health_check') INTO relevant;
                END IF;
                IF relevant THEN PERFORM pg_logical_emit_message(true,'supabricks.sy00.ddl',TG_TAG); END IF;
                END $$;
            CREATE EVENT TRIGGER sy00_ddl_end ON ddl_command_end EXECUTE FUNCTION sy00_ddl();
            CREATE EVENT TRIGGER sy00_ddl_drop ON sql_drop EXECUTE FUNCTION sy00_ddl();
            CREATE PUBLICATION sy00_pub FOR TABLE orders,payments;""")
        expected = self.metadata(db)
        self.check('qualified_two_table_key_schema', len(expected) == 2 and all(r[2]=='d' and r[3][0][0]==1 for r in expected.values()))
        start = time.monotonic()
        slot, consistent = db.execute("SELECT * FROM pg_create_logical_replication_slot('sy00_slot','pgoutput')").fetchone()
        self.metrics['slot_creation_ms'] = round((time.monotonic()-start)*1000,3)
        self.check('pgoutput_slot_created', slot == 'sy00_slot', consistent_point=consistent)
        password = secrets.token_hex(24)
        db.execute(sql.SQL('CREATE ROLE sy00_denied LOGIN PASSWORD {} NOSUPERUSER NOREPLICATION').format(sql.Literal(password)))
        denied = self.db(parent, 'sy00_denied', password)
        try:
            self.peek(denied)
        except psycopg.Error as error:
            self.check('ordinary_role_cannot_decode', error.sqlstate=='42501', sqlstate=error.sqlstate)
        else:
            raise AssertionError('unprivileged capture accepted')
        denied.close()
        # CREATE ROLE is not represented by table metadata; the probe uses an administrative
        # connection. Restricted production service identity is a separate SY06 gate.
        db.execute("BEGIN; INSERT INTO orders VALUES(2000,20,'before-cut'); INSERT INTO payments VALUES(2000,20,'before-cut'); COMMIT")
        spanning, aborted = self.db(parent, autocommit=False), self.db(parent, autocommit=False)
        spanning.execute("INSERT INTO orders VALUES(3000,30,'spanning'); INSERT INTO payments VALUES(3000,30,'spanning')")
        aborted.execute("INSERT INTO orders VALUES(4000,40,'aborted'); INSERT INTO payments VALUES(4000,40,'aborted')")
        db.execute('SELECT pg_stat_force_next_flush()'); db.execute('SELECT pg_stat_clear_snapshot()')
        scans_before = db.execute("SELECT sum(seq_scan)::int FROM pg_stat_user_tables WHERE relname IN ('orders','payments')").fetchone()[0]
        start = time.monotonic()
        frozen = self.fork(parent, 'bootstrap')
        frozen_db = self.db(frozen)
        boundary = lsn(frozen['branch']['ancestor_lsn'])
        self.check('capture_precedes_frozen_boundary', lsn(consistent) <= boundary,
                   source_boundary=format_lsn(boundary), distinct_timeline=frozen['branch']['timeline_id'] != parent['branch']['timeline_id'])
        with frozen_db.transaction():
            frozen_db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            base = self.rows(frozen_db, expected)
        self.metrics['fork_and_bootstrap_ms'] = round((time.monotonic()-start)*1000,3)
        db.execute('SELECT pg_stat_force_next_flush()'); db.execute('SELECT pg_stat_clear_snapshot()')
        scans_after = db.execute("SELECT sum(seq_scan)::int FROM pg_stat_user_tables WHERE relname IN ('orders','payments')").fetchone()[0]
        self.check('isolated_bootstrap_excludes_inflight', all(('2000',) in t and ('3000',) not in t and ('4000',) not in t for t in base.values()) and scans_before == scans_after,
                   primary_seq_scans_before=scans_before, primary_seq_scans_after=scans_after,
                   rows_per_table=1001)
        child_slots = frozen_db.execute("SELECT slot_name FROM pg_replication_slots WHERE slot_type='logical'").fetchall()
        self.check('branch_capture_identity_is_distinct', self.identity(parent)!=self.identity(frozen),
                   inherited_logical_slot_names=[r[0] for r in child_slots])
        spanning.commit(); spanning.close(); aborted.rollback(); aborted.close()
        db.execute("""BEGIN;
            UPDATE orders SET amount=123456789012345678901234567890.12345678 WHERE id=1;
            UPDATE payments SET amount=123456789012345678901234567890.12345678 WHERE id=1;
            UPDATE orders SET id=12002 WHERE id=2; UPDATE payments SET id=12002 WHERE id=2;
            DELETE FROM orders WHERE id=3; DELETE FROM payments WHERE id=3;
            UPDATE orders SET amount=44 WHERE id=4; UPDATE payments SET amount=44 WHERE id=4;
            SAVEPOINT discarded; INSERT INTO orders VALUES(4999,0,'savepoint-rollback'); ROLLBACK TO discarded;
            COMMIT;""")
        older = self.db(parent, autocommit=False)
        older.execute("INSERT INTO orders VALUES(5000,50,NULL); INSERT INTO payments VALUES(5000,50,NULL)")
        db.execute("BEGIN; INSERT INTO orders VALUES(5001,51,'younger'); INSERT INTO payments VALUES(5001,51,'younger'); COMMIT")
        older.commit(); older.close()
        self.barrier(db)
        payloads = self.peek(db)
        decoder = Decoder(expected)
        transactions = decoder.decode(payloads)
        model = Model(self.identity(parent), boundary, expected, base)
        selected = [t for t in transactions if t.end_lsn > boundary]
        toast_count = sum(v is UNCHANGED for t in selected for _,_,_,new in t.changes for v in (new or []))
        for transaction in transactions:
            model.apply(self.identity(parent), transaction)
            # Each admitted fixture transaction updates both tables as one visible unit.
            values = list(model.rows.values())
            self.check('atomic_fixture_transaction_'+str(transaction.xid), values[0]==values[1])
        oracle = self.rows(db, expected)
        self.check('bootstrap_plus_commit_tail_matches_source', model.rows==oracle
                   and all(('3000',) in t and ('4000',) not in t and ('4999',) not in t for t in oracle.values()),
                   decoded_transactions=len(transactions), tail_transactions=len(selected),
                   capture_bytes=sum(map(len,payloads)), unchanged_toast_fields=toast_count)
        self.check('exact_numeric_key_change_delete_and_toast', toast_count >= 2 and all(
            t[('1',)][1]=='123456789012345678901234567890.12345678' and ('2',) not in t
            and ('12002',) in t and ('3',) not in t and len(t[('4',)][2])==16000 for t in model.rows.values()))
        self.check('commit_order_not_xid_order', any(a.xid>b.xid for a,b in zip(selected,selected[1:])))
        applied = model.applied
        for transaction in decoder.decode(self.peek(db)):
            model.apply(self.identity(parent), transaction)
        self.check('unacknowledged_replay_is_idempotent', model.applied==applied and model.rows==oracle)
        try:
            model.apply(self.identity(frozen), selected[-1])
        except Unsupported:
            self.check('checkpoint_rejects_other_lineage', True)
        else:
            raise AssertionError('foreign source checkpoint accepted')
        self.check('frozen_reader_remains_pinned', self.rows(frozen_db,expected)==base)
        # Qualify the columnar representation in the shipped isolated analytical interpreter.
        data = {'tables': [{'name': relation[1], 'before':list(base[oid].values()),
                            'after':list(model.rows[oid].values())} for oid,relation in expected.items()]}
        source = self.root/'columnar-input.json'; source.write_text(json.dumps(data)); source.chmod(0o600)
        result = subprocess.run([str(analytical_python), str(HERE/'storage.py'), str(source), str(self.root/'columnar')],
                                capture_output=True,text=True,timeout=120)
        if result.returncode:
            (self.root/'columnar-private.log').write_text(result.stdout+'\n'+result.stderr)
            raise AssertionError('columnar probe failed; private fixture diagnostics retained')
        self.metrics['columnar'] = json.loads(result.stdout)
        self.check('versioned_delta_roots_and_atomic_epoch_map', self.metrics['columnar']['status']=='PASS')
        # Consumer acknowledgement here is disposable probe state, NOT a durable spool implementation.
        db.execute('SELECT pg_replication_slot_advance(%s,%s)',('sy00_slot',format_lsn(model.boundary)))
        acknowledged = db.execute("SELECT confirmed_flush_lsn::text FROM pg_replication_slots WHERE slot_name='sy00_slot'").fetchone()[0]
        frozen_db.close(); db.close()
        parent = self.state(parent,'suspended'); parent=self.state(parent,'running')
        db=self.db(parent)
        resumed=db.execute("SELECT confirmed_flush_lsn::text FROM pg_replication_slots WHERE slot_name='sy00_slot'").fetchone()
        self.check('slot_survives_suspend_wake', resumed is not None and lsn(resumed[0]) >= lsn(acknowledged))
        db.execute("BEGIN; INSERT INTO orders VALUES(6000,60,'after-wake'); INSERT INTO payments VALUES(6000,60,'after-wake'); COMMIT")
        self.barrier(db)
        before_crash = Decoder(expected).decode(self.peek(db))
        db.close()
        compute = next(r for r in self.records() if r.get('branch') and r['branch'][0]==parent['branch']['id'])
        descendants=process_tree([compute['pid']]); os.kill(compute['pid'],signal.SIGKILL)
        wait(lambda:any(r.get('branch') and r['branch'][0]==parent['branch']['id'] and r['pid']!=compute['pid'] for r in self.records()))
        wait(lambda:self.sql(parent,'SELECT 1')=='1'); assert_stopped(descendants)
        db=self.db(parent)
        after_crash=Decoder(expected).decode(self.peek(db))
        prior_ends={t.end_lsn for t in before_crash}
        self.check('unacknowledged_commits_survive_compute_kill', prior_ends <= {t.end_lsn for t in after_crash})
        for t in after_crash:model.apply(self.identity(parent),t)
        self.check('post_restart_tail_matches_source',model.rows==self.rows(db,expected))
        db.execute('SELECT pg_replication_slot_advance(%s,%s)',('sy00_slot',format_lsn(model.boundary)))
        baseline, capture, decode_latencies = [], [], []
        for _ in range(30):
            start=time.monotonic();db.execute('UPDATE orders SET amount=amount+1 WHERE id=10');baseline.append((time.monotonic()-start)*1000)
        # Catch up the baseline before measuring individual capture rounds.
        for t in Decoder(expected).decode(self.peek(db)):model.apply(self.identity(parent),t)
        db.execute('SELECT pg_replication_slot_advance(%s,%s)',('sy00_slot',format_lsn(model.boundary)))
        for _ in range(30):
            start=time.monotonic();db.execute('UPDATE orders SET amount=amount+1 WHERE id=10');committed=time.monotonic()
            capture.append((committed-start)*1000)
            for t in Decoder(expected).decode(self.peek(db)):model.apply(self.identity(parent),t)
            decode_latencies.append((time.monotonic()-committed)*1000)
            db.execute('SELECT pg_replication_slot_advance(%s,%s)',('sy00_slot',format_lsn(model.boundary)))
        self.metrics['write_without_consumption']=distribution(baseline)
        self.metrics['write_with_consumption']=distribution(capture)
        self.metrics['commit_ack_to_reference_apply']=distribution(decode_latencies)
        self.metrics['measurement_scope']='30 sequential one-row commits each, warm local host, capture enabled in both phases; SQL polling/reference model, not end-to-end columnar publication or throughput SLO'
        self.check('benchmark_oracle',model.rows==self.rows(db,expected))
        # DDL without subsequent row traffic must still be an ordered invalidation.
        for label, statement in [('add_column','ALTER TABLE orders ADD COLUMN future integer'),
                                 ('new_empty_table','CREATE TABLE new_member(id integer PRIMARY KEY)'),
                                 ('drop_empty_table','DROP TABLE new_member')]:
            db.execute(statement);self.barrier(db)
            feed=Decoder(expected).decode(self.peek(db))
            messages=[m for t in feed for m in t.messages if m[0]=='supabricks.sy00.ddl']
            self.check('ddl_fence_'+label,bool(messages))
            try:
                for t in feed:model.apply(self.identity(parent),t)
            except Unsupported:
                self.check('blocks_publication_'+label,True)
            else:raise AssertionError('DDL fence ignored')
            db.execute('SELECT pg_replication_slot_advance(%s,%s)',('sy00_slot',format_lsn(feed[-1].end_lsn)))
        db.execute('UPDATE orders SET future=1 WHERE id=1')
        try:Decoder(expected).decode(self.peek(db))
        except Unsupported:self.check('changed_relation_rejected',True)
        else:raise AssertionError('new column accepted')
        # Explicit source eligibility limitations: these are rejection experiments.
        db.execute('CREATE TABLE keyless(id integer); ALTER PUBLICATION sy00_pub ADD TABLE keyless; INSERT INTO keyless VALUES(1)')
        try:db.execute('DELETE FROM keyless')
        except psycopg.Error as error:self.check('keyless_delete_rejected_by_source',error.sqlstate=='55000',sqlstate=error.sqlstate)
        else:raise AssertionError('keyless delete unexpectedly accepted')
        slot_state=db.execute("SELECT restart_lsn::text,confirmed_flush_lsn::text,wal_status,safe_wal_size FROM pg_replication_slots WHERE slot_name='sy00_slot'").fetchone()
        self.metrics['slot_before_cleanup']=dict(zip(('restart_lsn','confirmed_flush_lsn','wal_status','safe_wal_size'),slot_state))
        db.execute("SELECT pg_drop_replication_slot('sy00_slot')")
        self.check('owned_capture_slot_removed',db.execute("SELECT count(*) FROM pg_replication_slots WHERE slot_name='sy00_slot'").fetchone()[0]==0)
        self.connections.close()
        self.stop()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',required=True,type=Path)
    parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args();release=args.release.resolve()
    # Cell never attaches to a user's existing installation data root.
    root=Path(tempfile.mkdtemp(prefix='sb-sy00-',dir='/tmp')).resolve();root.chmod(0o700)
    cell=Probe(release/'bin/supabricks',release/'engine',release/'helpers',root)
    started=time.monotonic()
    report={'format_version':1,'slice':'SY00','status':'FAIL','profile':'local-owner',
        'platform':platform.platform(),'architecture':platform.machine(),'python':platform.python_version(),
        'logical_cpus':os.cpu_count(),
        'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=HERE,text=True).strip(),
        'source_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=HERE,text=True).strip()),
        'inputs':{'release_manifest_sha256':digest(release/'release.json'),
                  'binary_sha256':digest(release/'bin/supabricks'),
                  'engine_manifest':json.loads((release/'engine/manifest.json').read_text()),
                  'probe_sha256':{p.name:digest(p) for p in sorted(HERE.glob('*.py'))}},
        'checks':cell.checks,'metrics':cell.metrics,
        'limitations':['SQL pgoutput v1 polling, not a streaming transport or durable spool',
            'administrative local fixture, not governed service-principal qualification',
            'SIGKILL/restart, not power loss or distributed failover',
            'DDL fence is probe-installed, not a production migration/authorization design',
            'bounded two-table fixture, not a production latency/throughput promise']}
    failure=None
    try:
        inventory=verify(release)
        report['inputs']['verified_inventory_files']=len(inventory['files'])
        receipt=release.parent/'sy00-inputs.json'
        if receipt.exists():
            data=json.loads(receipt.read_text())
            if data['release_manifest_sha256'] != report['inputs']['release_manifest_sha256']:
                raise ValueError('archive receipt does not match release')
            report['inputs']['archive_receipt']=data
        cell.run(release,release/'python/analytics/python');report['status']='PASS'
    except Exception as error:
        failure=error
        report['failure_type']=type(error).__name__
        print('Probe failed; private diagnostics: '+str(root),file=sys.stderr)
    finally:
        cell.connections.close()
        if (root/'control.sock').exists():
            try:cell.stop()
            except Exception as error:
                report['status']='FAIL';report['cleanup_failure_type']=type(error).__name__;failure=failure or error
        report['metrics']['duration_seconds']=round(time.monotonic()-started,3)
        usage=resource.getrusage(resource.RUSAGE_SELF)
        report['metrics']['harness_cpu_seconds']=round(usage.ru_utime+usage.ru_stime,3)
        report['metrics']['harness_peak_rss_bytes']=int(usage.ru_maxrss*(1 if sys.platform=='darwin' else 1024))
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2,default=str)+'\n')
        if report['status']=='PASS':shutil.rmtree(root)
    if failure:raise failure
    print('SY00 PASS: '+str(args.report))


if __name__=='__main__':main()
