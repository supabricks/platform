#!/usr/bin/env python3
"""Probe unchanged installed capture admission with native TPC-DS DDL."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from inputs import LOCK, inventory, sha, statements


def worker(args):
    # Separate process: native fixture capture.py shadows the installed package.
    sys.path.insert(0, str(args.release / 'python/analytics'))
    import psycopg
    from capture.source import inspect
    from capture.spool import CaptureError
    from session import read_sql
    credentials = json.load(sys.stdin)
    manifest = inventory(json.loads(LOCK.read_text()), args.inputs)
    controls = [
        dict(name='eq_control', ddl='CREATE TABLE eq_control(id integer PRIMARY KEY, value decimal(7,2))'),
        dict(name='eq_composite', ddl='CREATE TABLE eq_composite(a integer,b integer,PRIMARY KEY(a,b))'),
        dict(name='eq_date', ddl='CREATE TABLE eq_date(id integer PRIMARY KEY,value date)'),
        dict(name='eq_char', ddl='CREATE TABLE eq_char(id integer PRIMARY KEY,value char(4))')]
    results = []
    with psycopg.connect(**credentials, autocommit=True) as conn:
        tenant, timeline = conn.execute("SELECT current_setting('neon.tenant_id'),current_setting('neon.timeline_id')").fetchone()
        identity = dict(tenant_id=tenant, timeline_id=timeline)
        for table in controls + manifest['tables']:
            conn.execute(table['ddl'])
            try:
                profile = inspect(conn, identity)
                results.append(dict(table=table['name'], status='accepted', profile_bytes=len(json.dumps(profile))))
            except CaptureError as error:
                results.append(dict(table=table['name'], status='rejected', reason=str(error)))
            finally:
                conn.execute(psycopg.sql.SQL('DROP TABLE {}').format(psycopg.sql.Identifier(table['name'])))
        if results[0]['status'] != 'accepted':
            raise ValueError('positive control failed; schema probe is not valid')
    for query in manifest['queries']:
        sql = statements((args.inputs / 'spark' / (query['id'] + '.sql')).read_text())[0]
        try:
            read_sql(sql)
            query['lexical_admission'] = 'accepted'
        except ValueError as error:
            query['lexical_admission'] = 'rejected'
            query['lexical_reason'] = str(error)
    print(json.dumps(dict(controls=results[:len(controls)], tables=results[len(controls):],
                          queries=manifest['queries'], scope='schema admission and SQL lexical checks only; no sync or query execution')))


def run(args):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'native'))
    from installed_sync import InstalledContinuous
    args.report.parent.mkdir(parents=True, exist_ok=True)
    if args.report.exists():
        raise ValueError('report already exists')
    root = Path(tempfile.mkdtemp(prefix='eq00-')).resolve(); root.chmod(0o700)
    cell = InstalledContinuous(args.release, root)
    started = time.monotonic()
    report = dict(status='FAIL', fixture_root=str(root), input_lock_sha256=sha(LOCK),
                  package_scope='engineering native overlay; not exact release archive qualification')
    try:
        report['installation_identity'] = json.loads(subprocess.check_output(
            [str(cell.binary), 'installation', 'verify'], text=True))['identity']
        report['capture_source_sha256'] = sha(args.release / 'python/analytics/capture/source.py')
        python = args.release / 'python/analytics/python'
        cell.setup_source(str(python), args.release / 'python/analytics/export.py', 'SELECT 1')
        credentials = dict(host='127.0.0.1', port=cell.parent['ports']['sql'],
                           user='cloud_admin', password=cell.credentials(cell.parent), dbname='postgres')
        child = subprocess.run([str(python), str(Path(__file__).resolve()), '--worker',
            '--release', str(args.release), '--inputs', str(args.inputs)],
            input=json.dumps(credentials), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if child.returncode:
            # Worker tracebacks contain code locations, not credential dictionaries.
            raise RuntimeError(child.stderr)
        report.update(json.loads(child.stdout))
        cell.stop()
        report['status'] = 'PASS'
    except BaseException as error:
        report['error'] = str(error)
        raise
    finally:
        report['elapsed_seconds'] = time.monotonic() - started
        args.report.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    if args.worker: worker(args)
    else:
        if args.report is None: parser.error('--report required')
        run(args)
