#!/usr/bin/env python3
"""I00 transaction/branch/export receipt contract against the installed engine.

Synthetic data only; run with the installed private Python. This is a protocol
fixture, not an ingestion command or a qualification of I01 CSV parsing.
"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import psycopg


def qualify(binary, data, project):
    binary, data, project = map(Path, (binary, data, project))
    release = binary.resolve().parent.parent

    def cli(*args):
        result = subprocess.run([str(binary), *map(str, args), '--data-dir', str(data), '--project', str(project)],
                                capture_output=True, text=True, timeout=240)
        if result.returncode:
            raise AssertionError('receipt fixture CLI failed: ' + str(args[0]))
        return json.loads(result.stdout)

    uri = cli('connect', 'main')['uri']
    # Read-only fixture observation. The daemon remains the only catalog writer.
    with sqlite3.connect(f'file:{data}/state.sqlite3?mode=ro', uri=True) as db:
        origin = db.execute('SELECT origin FROM ingest_identity WHERE id=1').fetchone()[0]
        branch, project_id = db.execute("SELECT id,project_id FROM branches WHERE name='main' AND desired!='deleted'").fetchone()
    job = str(uuid.uuid4())
    ddl = (release / 'share/ingest/receipt.sql').read_text()
    with psycopg.connect(uri) as conn:
        conn.execute(ddl)
        conn.commit()
        try:
            with conn.transaction():
                conn.execute('CREATE TABLE public.receipt_rollback(id bigint)')
                with conn.cursor().copy('COPY public.receipt_rollback FROM STDIN') as copy:
                    copy.write_row((9007199254740993,))
                raise ValueError('synthetic failure before receipt/commit')
        except ValueError:
            pass
        assert conn.execute("SELECT to_regclass('public.receipt_rollback')").fetchone()[0] is None
        conn.commit()
        with conn.transaction():
            conn.execute('CREATE TABLE public.receipt_committed(id bigint)')
            with conn.cursor().copy('COPY public.receipt_committed FROM STDIN') as copy:
                copy.write_row((9007199254740993,))
            oid = conn.execute("SELECT 'public.receipt_committed'::regclass::oid").fetchone()[0]
            conn.execute('INSERT INTO _supabricks.ingest_receipts VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                         (origin, project_id, branch, job, 'a'*64, 'b'*64, 'public', 'receipt_committed', oid, 1))
    # Reconnect independently after commit, representing a lost worker response.
    with psycopg.connect(uri) as conn:
        assert conn.execute('SELECT id FROM public.receipt_committed').fetchall() == [(9007199254740993,)]
        assert conn.execute('SELECT target_oid,committed_rows FROM _supabricks.ingest_receipts WHERE origin=%s AND project_id=%s AND branch_id=%s AND job_id=%s',
                            (origin, project_id, branch, job)).fetchone() == (oid, 1)
    cli('branch', 'create', 'receipt-child', '--from', 'main', '--wait')
    child_uri = cli('connect', 'receipt-child')['uri']
    with psycopg.connect(child_uri) as conn:
        assert conn.execute('SELECT branch_id::text FROM _supabricks.ingest_receipts WHERE job_id=%s', (job,)).fetchone() == (branch,)
        assert conn.execute('SELECT count(*) FROM _supabricks.ingest_receipts WHERE branch_id!=%s AND job_id=%s', (branch,job)).fetchone() == (0,)
        conn.execute('UPDATE public.receipt_committed SET id=7')
    with psycopg.connect(uri) as conn:
        assert conn.execute('SELECT id FROM public.receipt_committed').fetchall() == [(9007199254740993,)]
    catalog = cli('catalog', '--branch', 'main')
    assert '_supabricks' not in json.dumps(catalog), 'receipt leaked into normal table discovery'
    cli('analytics', 'refresh', '--branch', 'main', '--wait')
    # Receipt contains unsupported UUID/OID types: export success additionally
    # proves discovery excluded it before type validation. Inspect durable output.
    manifests = list((data / 'analytics').rglob('manifest.json'))
    assert manifests, 'no analytical manifest found'
    for path in manifests:
        value = json.loads(path.read_text())
        assert '_supabricks' not in json.dumps(value), 'receipt leaked into analytical manifest'
    cli('branch', 'delete', 'receipt-child', '--wait')
    with psycopg.connect(uri) as conn:
        conn.execute('DROP TABLE public.receipt_committed')
    return {'status': 'passed', 'checks': ['COPY rollback leaves no table', 'table and origin-scoped receipt commit together',
            'independent receipt read resolves lost response', 'physical child inherits original branch receipt and isolates writes',
            'catalog and analytical exports exclude reserved schema']}


if __name__ == '__main__':
    print(json.dumps(qualify(*sys.argv[1:])))
