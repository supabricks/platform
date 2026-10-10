#!/usr/bin/env python3
"""EQ232 source-only control: frozen TPC-DS bytes, order and COPY transactions.

No capture/apply policy. WAL/logical settings, indexes and source durability
stay enabled. Verify directly against the generated prefix after timing; large
full-bootstrap limits are tracked separately in issue #237.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from source_profile import Observer


def run(args):
    frozen = args.load.resolve()
    sys.path.insert(0, str(frozen.parent.parent / 'native')); sys.path.insert(0, str(frozen.parent))
    spec = importlib.util.spec_from_file_location('frozen_load', frozen)
    load = importlib.util.module_from_spec(spec); spec.loader.exec_module(load)
    from installed_sync import InstalledContinuous
    selected = load.workload('sf100-growing-prefix'); bounds = selected['load_bounds']
    args.output.mkdir(exist_ok=False, parents=True); root = args.output / 'state'; root.mkdir(mode=0o700)
    cell = InstalledContinuous(args.release.resolve(), root.resolve())
    if cache_profile := os.environ.get("EQ236_COMPUTE_CACHE_PROFILE"):
        from compute_cache_profile import install
        install(cell, cache_profile)
    observer = Observer(cell, cell.source, args.output / 'source-profile')
    manifest = load.inventory(json.loads(load.LOCK.read_text()), args.inputs)
    generation = json.loads((args.dataset / 'generation.json').read_text())
    profile = json.loads(load.PROFILE.read_text())
    expected = load.validate_generation(selected, generation, load.sha(args.dataset / 'generation.json'), load.sha(load.LOCK), manifest['tables'])
    report = dict(status='RUNNING', arm='source_only', stage='admission',
        scope='EQ232 source capacity; post-timing sync bootstrap is not concurrent sync throughput',
        started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        scale=selected['scale'], workload_profile_sha256=selected['profile_sha256'],
        workload_profile=selected, load_profile=profile, load_profile_sha256=load.sha(load.PROFILE),
        generation_receipt_sha256=load.sha(args.dataset / 'generation.json'),
        input_lock_sha256=load.sha(load.LOCK), release_identity=load.sha(args.release / 'release.json'),
        fixture_sha256=load.sha(Path(__file__)), frozen_load_sha256=load.sha(frozen),
        committed_rows=0, committed_transactions=0, flow_control_wait_seconds=0, tables=[], checks=cell.checks)
    def save():
        load.save(args.output / 'result.json', report)
    try:
        load.require_storage(selected, shutil.disk_usage(root).free, admission=True)
        for name, data in expected.items():
            path = args.dataset / 'data' / name
            assert path.stat().st_size == data['bytes'] and load.sha(path) == data['sha256']
        assert json.loads(subprocess.check_output([str(cell.binary), 'installation', 'verify']))['identity'] == report['release_identity']
        cell.setup_source(str(cell.release / 'python/analytics/python'), cell.release / 'python/analytics/export.py', ';'.join(t['ddl'] for t in manifest['tables']))
        with sqlite3.connect(f'file:{root}/state.sqlite3?mode=ro', uri=True) as control:
            assert control.execute('SELECT count(*) FROM sync_policies').fetchone() == (0,)
            assert control.execute('SELECT count(*) FROM sync_captures').fetchone() == (0,)
        report.update(project_id=cell.project, branch_id=cell.parent['id'])
        cell.check('no_capture_or_apply_during_source_capacity_measurement')
        report['stage'] = 'load'; save()
        # The same frozen batching function and transaction statements as load.py.
        import psycopg
        with observer.source() as db, (args.output / 'commits.jsonl').open('x') as journal:
            db.execute("SET DateStyle='ISO,YMD'"); db.execute("SET statement_timeout='120s'")
            started = time.monotonic(); report['load_started_unix_ns'] = time.time_ns(); last = started; last_storage = started
            for table in manifest['tables']:
                if report['committed_rows'] == selected['load_rows']:
                    break
                name = table['name']; table_start = time.monotonic()
                target = min(expected[name + '.dat']['rows'], selected['load_rows'] - report['committed_rows'])
                entry = dict(table=name, rows=0, transactions=0); report['tables'].append(entry)
                sql = psycopg.sql.SQL('COPY {} FROM STDIN WITH (ENCODING {})').format(psycopg.sql.Identifier(name), psycopg.sql.Literal(profile['postgres_copy_encoding']))
                for begin, end, count, data in load.batches(args.dataset / 'data' / (name + '.dat'), table['columns']):
                    if entry['rows'] == target:
                        break
                    if entry['rows'] + count > target:
                        raise ValueError('prefix must end on COPY boundary')
                    if time.monotonic() - started > bounds['timeout_seconds']:
                        raise TimeoutError('source load deadline')
                    if time.monotonic() - last >= 1:
                        save(); last = time.monotonic()
                        if time.monotonic() - last_storage >= 10:
                            total = 0
                            for path in root.rglob('*'):
                                try:
                                    if path.is_file(): total += path.stat().st_blocks * 512
                                except FileNotFoundError:
                                    pass
                            load.require_storage(selected, shutil.disk_usage(root).free, total)
                            report['peak_sampled_cell_bytes'] = max(report.get('peak_sampled_cell_bytes', 0), total)
                            last_storage = time.monotonic()
                    before = time.monotonic()
                    attempt = dict(kind='attempt', table=name, start_offset=begin, end_offset=end, rows=count,
                                   encoded_bytes=len(data), copy_sha256=hashlib.sha256(data).hexdigest())
                    journal.write(json.dumps(attempt) + '\n'); journal.flush()
                    with db.transaction():
                        with db.cursor().copy(sql) as copy:
                            copy.write(data)
                        boundary = db.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone()[0]
                    ack = time.monotonic()
                    journal.write(json.dumps(dict(kind='ack', table=name, end_offset=end, rows=count, boundary_lsn=boundary,
                        elapsed_seconds=ack-started, copy_and_commit_ms=(ack-before)*1000)) + '\n'); journal.flush()
                    entry['rows'] += count; entry['transactions'] += 1
                    report['committed_rows'] += count; report['committed_transactions'] += 1; report['last_ack_lsn'] = boundary
                entry['elapsed_seconds'] = time.monotonic() - table_start
                assert entry['rows'] == target
                if name == 'customer':
                    key, country = load.country_control(args.dataset / 'data/customer.dat', table['columns'])
                    assert db.execute('SELECT c_birth_country FROM customer WHERE c_customer_sk=%s', (key,)).fetchone()[0] == country
                    report['unicode_control'] = dict(customer_key=key, birth_country=country)
                print('LOADED', name, entry['rows'], flush=True); save()
            report['load_seconds'] = time.monotonic() - started; report['load_finished_unix_ns'] = time.time_ns()
            report['source_rows'] = {t['name']: db.execute(psycopg.sql.SQL('SELECT count(*) FROM {}').format(psycopg.sql.Identifier(t['name']))).fetchone()[0] for t in manifest['tables']}
        observer.finish(); observer = None
        counts = {t['table']: t['rows'] for t in report['tables']}
        assert all(report['source_rows'][t['name']] == counts.get(t['name'], 0) for t in manifest['tables'])
        assert report['committed_rows'] == selected['load_rows']
        report.update(status='SOURCE_LOAD_PASS', stage='loaded_requires_exact_source_verification')
    except BaseException as error:
        report.update(status='FAIL', error_type=type(error).__name__); raise
    finally:
        if observer is not None:
            observer.finish()
        if (root / 'control.sock').exists():
            cell.stop(); report['stopped'] = True
        save()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('load', 'release', 'inputs', 'dataset', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    run(p.parse_args())
