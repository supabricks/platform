#!/usr/bin/env python3
"""Bounded native TPC-DS COPY with continuous sync already active.

This is a pilot, not a throughput qualification. Each attempt owns a fresh cell;
failed/ambiguous commits are retained and never automatically retried.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from inputs import LOCK, inventory, sha
from workload import workload, validate_generation, require_storage

ROWS = 1024
BYTES = 4 * 1024**2
PROFILE = Path(__file__).with_name('load-profile.json')


def batches(path, columns, max_rows=ROWS, max_bytes=BYTES):
    """Convert generated empty fields to COPY NULL without changing text values."""
    parts, size, start, offset = [], 0, 0, 0
    with path.open('rb') as stream:
        for raw in stream:
            if not raw.endswith(b'|\n'):
                raise ValueError('incomplete generated row')
            fields = raw[:-2].split(b'|')
            if len(fields) != len(columns):
                raise ValueError('generated field count differs')
            if any(not value and not col['nullable'] for value, col in zip(fields, columns)):
                raise ValueError('required generated value missing')
            # COPY text escaping preserves literal backslashes, tabs and CR.
            values = [v.replace(b'\\', b'\\\\').replace(b'\t', b'\\t').replace(b'\r', b'\\r')
                      if v else b'\\N' for v in fields]
            encoded = b'\t'.join(values) + b'\n'
            if len(encoded) > max_bytes:
                raise ValueError('one row exceeds COPY byte ceiling')
            if parts and (len(parts) == max_rows or size + len(encoded) > max_bytes):
                yield start, offset, len(parts), b''.join(parts)
                parts, size, start = [], 0, offset
            parts.append(encoded); size += len(encoded); offset += len(raw)
        if parts:
            yield start, offset, len(parts), b''.join(parts)


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, default=str) + '\n')
    temporary.replace(path)


def country_control(path, columns):
    """Bind the Unicode control to this dataset, without assuming SF1 row keys."""
    names = [c['name'] for c in columns]
    key, country = names.index('c_customer_sk'), names.index('c_birth_country')
    with path.open('rb') as stream:
        for index, raw in enumerate(stream):
            fields = raw.rstrip(b'\n').split(b'|')
            if any(byte >= 128 for byte in fields[country]):
                return int(fields[key]), fields[country].decode('latin1')
            if index >= 1023:
                break
    raise ValueError('no non-ASCII country control in first 1024 customer rows')


def run(args):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'native'))
    from installed_sync import InstalledContinuous
    from cell import lsn
    import psycopg

    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output / 'state'; root.mkdir(mode=0o700)
    root = root.resolve()
    cell = InstalledContinuous(args.release.resolve(), root)
    manifest = inventory(json.loads(LOCK.read_text()), args.inputs)
    generation = json.loads((args.dataset / 'generation.json').read_text())
    selected = workload(args.workload)
    bounds = selected['load_bounds']
    profile = json.loads(PROFILE.read_text())
    assert profile['rows_per_commit']==ROWS and profile['encoded_copy_bytes_per_commit']==BYTES
    report = dict(status='RUNNING', stage='admission', scope=f'SF{selected["scale"]} engineering load pilot; no full-suite or release claim',
                  scale=selected['scale'], workload_profile=selected,
                  workload_profile_sha256=selected['profile_sha256'],
                  started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  input_lock_sha256=sha(LOCK), generation_receipt_sha256=sha(args.dataset / 'generation.json'),
                  load_profile=profile, load_profile_sha256=sha(PROFILE),
                  fixture_sha256=sha(Path(__file__)), release_identity=sha(args.release / 'release.json'),
                  bounds=dict(rows_per_commit=ROWS, encoded_bytes_per_commit=BYTES,
                              timeout_seconds=args.timeout, minimum_free_gib=bounds['minimum_free_gib'],
                              maximum_cell_gib=bounds['maximum_cell_gib'], storage_sample_seconds=10,
                              minimum_remaining_free_gib=bounds['minimum_remaining_free_gib'],
                              max_unpublished_rows=args.max_unpublished_rows),
                  load_order=[t['name'] for t in manifest['tables']],
                  committed_rows=0, committed_transactions=0, flow_control_wait_seconds=0,
                  tables=[], checks=cell.checks,
                  queries=[dict(q, status='not_run', reason='full load and exact verification pending') for q in manifest['queries']])
    started = time.monotonic(); last_sample = 0; cap = None
    def checkpoint():
        report['elapsed_seconds'] = time.monotonic() - started
        save(args.output / 'result.json', report)
    def sample(force=False):
        nonlocal last_sample
        now = time.monotonic()
        if now - started > args.timeout:
            raise TimeoutError('load/drain pilot deadline')
        if not force and now - last_sample < 1:
            return
        old = last_sample; last_sample = now
        policy = cell.policy(); capture = cell.status(cap)
        current = cell.current()
        value = dict(elapsed_seconds=now-started, committed_rows=report['committed_rows'],
                     policy=policy, capture=capture,
                     publication=None if current is None else dict(epoch_id=current['epoch_id'],
                         rows=sum(t['rows'] for t in current['descriptor']['manifest']['tables']),
                         source=current['descriptor']['manifest']['source']))
        with (args.output / 'observations.jsonl').open('a') as stream:
            stream.write(json.dumps(value, default=str) + '\n')
        report['latest'] = value
        if old == 0 or int((now-started)/10) != int((old-started)/10):
            # Sampled ceiling, not a filesystem quota. Inventory only this cell.
            total = 0
            for path in root.rglob('*'):
                try:
                    if path.is_file(): total += path.stat().st_blocks * 512
                except FileNotFoundError:
                    pass
            report['peak_sampled_cell_bytes'] = max(report.get('peak_sampled_cell_bytes', 0), total)
            report['latest_free_bytes'] = shutil.disk_usage(root).free
            require_storage(selected, report['latest_free_bytes'], total)
        checkpoint()
        if policy['continuous_status']['state'] in ('blocked', 'failed') or capture['state'] in ('resync_required', 'failed'):
            raise RuntimeError('continuous sync blocked; see retained policy/capture observation')
    try:
        expected = validate_generation(selected, generation, report['generation_receipt_sha256'],
                                       sha(LOCK), manifest['tables'])
        report['storage_admission'] = dict(free_bytes=shutil.disk_usage(root).free,
                                          required_free_bytes=bounds['minimum_free_gib'] * 1024**3,
                                          filesystem_device=root.stat().st_dev,
                                          budget_gib=selected.get('storage_budget_gib'),
                                          note='Sampled bounds; full retained workload fit is unproven')
        checkpoint()
        require_storage(selected, report['storage_admission']['free_bytes'], admission=True)
        for name, data in expected.items():
            path = args.dataset / 'data' / name
            assert path.stat().st_size == data['bytes'] and sha(path) == data['sha256'], name
        verified = json.loads(subprocess.check_output([str(cell.binary), 'installation', 'verify'], text=True))
        assert verified['verified'] and verified['identity'] == report['release_identity']
        report['stage'] = 'bootstrap'; checkpoint()
        cell.setup_source(str(cell.release/'python/analytics/python'), cell.release/'python/analytics/export.py',
                          ';'.join(t['ddl'] for t in manifest['tables']))
        storage_args = [] if selected['storage_profile'] == 'compact' else ['--storage-profile', selected['storage_profile']]
        policy = cell.cli('sync', 'create', '--branch', 'main', '--mode', 'continuous',
                          '--key', args.workload, *storage_args)
        cell.policy_id = policy['id']; cap = dict(id=policy['capture_id'])
        assert policy['config'].get('storage_profile', 'compact') == selected['storage_profile']
        cell.healthy(); report['initial_capture'] = cell.status(cap)
        assert cell.current()['descriptor']['manifest'].get('storage_profile', 'compact') == selected['storage_profile']
        report['empty_epoch'] = cell.current()['epoch_id']
        cell.check('all_24_native_tables_enrolled_and_continuous_sync_healthy_before_first_copy')
        report['stage'] = 'load'; report['load_started_seconds'] = time.monotonic()-started
        sample(True)
        with cell.source() as db, (args.output/'commits.jsonl').open('x') as journal:
            db.execute("SET DateStyle='ISO,YMD'")
            db.execute("SET statement_timeout='120s'")
            for table in manifest['tables']:
                name = table['name']; table_start = time.monotonic()
                entry = dict(table=name, rows=0, transactions=0); report['tables'].append(entry)
                sql = psycopg.sql.SQL('COPY {} FROM STDIN WITH (ENCODING {})').format(
                    psycopg.sql.Identifier(name), psycopg.sql.Literal(profile['postgres_copy_encoding']))
                for begin, end, count, data in batches(args.dataset/'data'/(name+'.dat'), table['columns']):
                    sample()
                    # This insert-only, single-loader fixture begins empty. The
                    # coherent publication's exact row total is a conservative
                    # acknowledgment window. No source transaction is held while
                    # waiting, and no safety limits are increased or disabled.
                    while (args.max_unpublished_rows and report['committed_rows']+count-
                           report['latest']['publication']['rows']>args.max_unpublished_rows):
                        wait_started=time.monotonic()
                        time.sleep(.2);sample(True)
                        report['flow_control_wait_seconds']+=time.monotonic()-wait_started
                    before = time.monotonic()
                    report['inflight'] = dict(table=name, start_offset=begin, end_offset=end, rows=count)
                    # The durable ledger records attempts before issuing COMMIT.
                    attempt = dict(kind='attempt', **report['inflight'], encoded_bytes=len(data),
                                   copy_sha256=hashlib.sha256(data).hexdigest())
                    journal.write(json.dumps(attempt)+'\n'); journal.flush()
                    with db.transaction():
                        with db.cursor().copy(sql) as copy:
                            copy.write(data)
                        boundary = db.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone()[0]
                    ack = time.monotonic()
                    journal.write(json.dumps(dict(kind='ack', table=name, end_offset=end, rows=count, boundary_lsn=boundary,
                             elapsed_seconds=ack-started, copy_and_commit_ms=(ack-before)*1000))+'\n')
                    journal.flush()
                    entry['rows'] += count; entry['transactions'] += 1
                    report['committed_rows'] += count; report['committed_transactions'] += 1
                    report['last_ack_lsn'] = boundary; report.pop('inflight')
                entry['elapsed_seconds'] = time.monotonic()-table_start
                assert entry['rows'] == expected[name+'.dat']['rows'], name
                if name=='customer':
                    key, country = country_control(args.dataset/'data/customer.dat', table['columns'])
                    assert db.execute('SELECT c_birth_country FROM customer WHERE c_customer_sk=%s', (key,)).fetchone()[0] == country
                    report['unicode_control'] = dict(customer_key=key, birth_country=country)
                    cell.check('latin1_generator_country_preserved_as_postgresql_unicode')
                print('LOADED', name, entry['rows'], flush=True); checkpoint()
            # Last changed transaction's marker is before COMMIT; a covering
            # publication can only include it once the transaction is complete.
            report['source_rows'] = {t['name']: db.execute(psycopg.sql.SQL('SELECT count(*) FROM {}').format(
                psycopg.sql.Identifier(t['name']))).fetchone()[0] for t in manifest['tables']}
        report['load_seconds'] = time.monotonic()-started-report['load_started_seconds']
        report['stage'] = 'drain'; checkpoint(); drain_start = time.monotonic()
        while True:
            sample(True)
            if lsn(cell.current()['descriptor']['manifest']['source']['lsn']) >= lsn(report['last_ack_lsn']):
                break
            time.sleep(.5)
        report['drain_seconds'] = time.monotonic()-drain_start
        report['publication'] = cell.current()
        assert report['committed_rows'] == generation['business_rows']
        assert all(report['source_rows'][t['name']] == expected[t['name']+'.dat']['rows'] for t in manifest['tables'])
        cell.check(f'all_{args.workload}_rows_committed_and_published_boundary_reached')
        report.update(status='PASS', stage='loaded_requires_exact_verification_and_queries')
    except BaseException as error:
        report['status'] = 'FAIL'
        report['error'] = str(error)
        report['error_type'] = type(error).__name__
        raise
    finally:
        if (root/'control.sock').exists():
            try:
                if cap:
                    report['final_policy'] = cell.policy(); report['final_capture'] = cell.status(cap)
                cell.stop(); report['stopped'] = True
            except Exception as error:
                report['cleanup_error'] = str(error); report['status'] = 'FAIL'
        checkpoint()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('release', 'inputs', 'dataset', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--workload', choices=('sf1', 'sf100'), default='sf1')
    p.add_argument('--timeout', type=int)
    p.add_argument('--max-unpublished-rows',type=int,
                   help='0: unrestricted pilot; otherwise pause COPY outside transactions at this row window')
    args=p.parse_args()
    bounds = workload(args.workload)['load_bounds']
    if args.timeout is None: args.timeout = bounds['timeout_seconds']
    if not 0 < args.timeout <= bounds['timeout_seconds']:
        p.error('timeout must be positive and within workload deadline')
    if args.max_unpublished_rows is None:
        args.max_unpublished_rows = bounds['default_max_unpublished_rows']
    if args.max_unpublished_rows and not ROWS<=args.max_unpublished_rows<=131072:
        p.error('row window must be 0 or between 1,024 and 131,072')
    run(args)
