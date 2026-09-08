#!/usr/bin/env python3
"""A01 worker: read one private frozen compute into an unpublished generation."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from decimal import Decimal

import psycopg
from psycopg import sql
import pyarrow as pa
from deltalake import DeltaTable, WriterProperties, write_deltalake
from qualify import environment

ROW_BYTES = 256 * 1024
BATCH_BYTES = 8 * 1024 * 1024
BATCH_ROWS = 4096
# delta-rs retains file actions while reopening an append target. Bound this
# metadata too, not just row/Arrow buffers, across the entire generation.
MAX_BATCHES = 1024
FREE_RESERVE = 64 * 1024 * 1024


class Rejected(Exception):
    pass


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(value, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def arrow_type(oid, typmod):
    simple = {16: pa.bool_(), 21: pa.int16(), 23: pa.int32(), 20: pa.int64(),
              25: pa.string(), 1043: pa.string(), 1082: pa.date32(),
              1114: pa.timestamp('us'), 1184: pa.timestamp('us', tz='UTC')}
    if oid in simple:
        return simple[oid]
    if oid == 1700 and typmod >= 4:
        precision = ((typmod - 4) >> 16) & 65535
        scale = (typmod - 4) & 2047
        scale = scale - 2048 if scale >= 1024 else scale
        if 1 <= precision <= 38 and 0 <= scale <= precision:
            return pa.decimal128(precision, scale)
    raise Rejected(f'unsupported PostgreSQL type OID/typmod: {oid}/{typmod}')


def disk_bytes(root):
    total = 0
    for path in root.rglob('*'):
        if path.is_symlink():
            raise Rejected('unexpected symlink in generation')
        if path.is_file():
            total += path.stat().st_size
    return total


def boundary(config, root, reserve=0):
    if time.time() * 1000 >= config['deadline_ms']:
        raise Rejected('export deadline exceeded')
    used = disk_bytes(root)
    if used + reserve > config['limits']['max_bytes']:
        raise Rejected('export output budget exceeded')
    space = os.statvfs(root)
    if space.f_bavail * space.f_frsize < FREE_RESERVE + reserve:
        raise Rejected('insufficient free space for export')
    return used


def discover(conn):
    relations = conn.execute("""
        SELECT c.oid,n.nspname,c.relname,c.relkind,c.relpersistence,c.relrowsecurity,
               c.relispartition, EXISTS(SELECT 1 FROM pg_depend d WHERE
                 d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'),
               EXISTS(SELECT 1 FROM pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid),
               c.relowner=(SELECT oid FROM pg_roles WHERE rolname='cloud_admin')
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname NOT IN ('information_schema', '_supabricks')
          AND c.relkind IN ('r','p','f','m','v','S') ORDER BY c.oid LIMIT 257
    """).fetchall()
    if len(relations) > 256:
        raise Rejected('at most 256 user relations per export')
    tables, omitted = [], []
    for oid, namespace, name, kind, persistence, rls, partition, extension, inherits, engine_owner in relations:
        # compute_ctl updates these after branching. They are control-plane
        # state, not application data at the captured source boundary.
        if (namespace, name) in (('public', 'health_check'), ('neon_migration', 'migration_id')):
            if not engine_owner:
                raise Rejected('application ownership collides with an engine control relation')
            omitted.append({'schema': namespace, 'name': name, 'reason': 'engine control relation'})
            continue
        if extension or kind in ('v', 'S'):
            omitted.append({'schema': namespace, 'name': name, 'reason': 'extension-owned' if extension else 'view/sequence; table data only'})
            continue
        if kind != 'r' or persistence != 'p' or rls or partition or inherits:
            raise Rejected(f'unsupported relation: {namespace}.{name} (ordinary permanent tables without RLS/inheritance only)')
        columns = conn.execute("""
            SELECT a.attname,a.atttypid,a.atttypmod,a.attnotnull,a.attcollation,
                   co.collname,co.collprovider,co.collisdeterministic
            FROM pg_attribute a LEFT JOIN pg_collation co ON co.oid=a.attcollation
            WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY a.attnum LIMIT 129
        """, (oid,)).fetchall()
        if not 1 <= len(columns) <= 128:
            raise Rejected(f'table {oid} requires 1–128 columns')
        fields, metadata, names = [], [], set()
        for column, type_oid, typmod, notnull, collation, colname, provider, deterministic in columns:
            if column.casefold() in names or any(c in column for c in ' ,;{}()\n\t='):
                raise Rejected(f'table {oid} has unsupported Delta column names')
            names.add(column.casefold())
            if collation not in (0, 100):
                raise Rejected(f'table {oid} has an explicit/non-default collation')
            dtype = arrow_type(type_oid, typmod)
            fields.append(pa.field(column, dtype, nullable=not notnull))
            metadata.append({'name': column, 'type_oid': type_oid, 'typmod': typmod,
                             'nullable': not notnull, 'arrow_type': str(dtype),
                             'collation': colname, 'collation_provider': provider,
                             'collation_deterministic': deterministic})
        tables.append((oid, namespace, name, pa.schema(fields), metadata))
    if len(tables) > 128:
        raise Rejected('at most 128 exported tables')
    return tables, omitted


def query(namespace, name, columns):
    # Bound a row on the server BEFORE libpq/Python materialize it. octet_length
    # sees uncompressed text length, unlike pg_column_size on compressed TOAST.
    lengths = [sql.SQL('coalesce(octet_length({}),0)::bigint').format(sql.Identifier(c['name']))
               if c['type_oid'] in (25, 1043) else sql.SQL('32::bigint') for c in columns]
    size = sql.SQL(' + ').join(lengths)
    safe = sql.SQL('({}) <= {}').format(size, sql.Literal(ROW_BYTES))
    values = [sql.SQL('CASE WHEN {} THEN {} ELSE NULL END').format(safe, sql.Identifier(c['name'])) for c in columns]
    return sql.SQL('SELECT {},{} FROM {}.{}').format(safe, sql.SQL(',').join(values), sql.Identifier(namespace), sql.Identifier(name))


def export(config):
    target, versions = environment()
    root = Path(config['output'])
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    boundary(config, root)
    started = time.monotonic()
    options = ('-c default_transaction_read_only=on -c default_transaction_isolation=repeatable\\ read '
               '-c search_path=pg_catalog -c client_encoding=UTF8 -c TimeZone=UTC -c DateStyle=ISO,YMD '
               '-c statement_timeout=30000 -c lock_timeout=2000 -c row_security=off')
    tables_report = []
    total_batches = 0
    with psycopg.connect(host='127.0.0.1', port=config['port'], dbname='postgres',
                        user=config['username'], password=config['password'],
                        application_name=f"supabricks-export-{config['id']}",
                        options=options, connect_timeout=5, autocommit=True) as conn:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        actual = conn.execute("SELECT current_setting('neon.tenant_id'),current_setting('neon.timeline_id'),pg_backend_pid(),pg_current_snapshot()::text,(SELECT oid FROM pg_database WHERE datname=current_database())").fetchone()
        if actual[:2] != (config['source']['tenant_id'], config['source']['export_timeline_id']):
            raise Rejected('compute identity differs from pinned export branch')
        observed_at_ms = round(time.time() * 1000)
        tables, omitted = discover(conn)
        # Every catalog read and table scan above/below shares this transaction.
        for oid, namespace, name, schema, columns in tables:
            path = root / str(oid)
            rows, batches, peak = 0, 0, 0
            pending, pending_bytes = [], 0

            def flush():
                nonlocal rows, batches, peak, pending, pending_bytes, total_batches
                if total_batches >= MAX_BATCHES:
                    raise Rejected('export exceeds 1024 Delta batches; metadata budget exhausted')
                batch = pa.Table.from_pylist(pending, schema=schema)
                # One unpartitioned bounded batch per Delta commit. Reserve ample
                # metadata/encoding space BEFORE writing; confirm actual use after.
                reserve = 4 * batch.nbytes + 4 * 1024 * 1024
                boundary(config, root, reserve)
                write_deltalake(path, batch, mode='append' if batches else 'error',
                    # delta-rs 1.6.3 emits lossy floating-point decimal min/max
                    # statistics. Sail can substitute those for actual values
                    # or prune valid rows. Keep exact Parquet values authoritative.
                    configuration={'delta.dataSkippingNumIndexedCols': '0'},
                    writer_properties=WriterProperties(compression='UNCOMPRESSED',
                        max_row_group_size=BATCH_ROWS, data_page_size_limit=64 * 1024),
                    target_file_size=64 * 1024 * 1024)
                boundary(config, root)
                rows += len(pending)
                batches += 1
                total_batches += 1
                peak = max(peak, batch.nbytes)
                pending, pending_bytes = [], 0

            with conn.cursor(name=f'export_{oid}') as cursor:
                cursor.execute(query(namespace, name, columns))
                while chunk := cursor.fetchmany(32):
                    boundary(config, root)
                    for valid, *values in chunk:
                        if not valid:
                            raise Rejected(f'table {oid} contains a row larger than 256 KiB')
                        for value in values:
                            if isinstance(value, Decimal) and not value.is_finite():
                                raise Rejected(f'table {oid} contains non-finite numeric')
                        size = sum(len(v.encode('utf-8')) if isinstance(v, str) else 32 for v in values)
                        if pending and (len(pending) >= BATCH_ROWS or pending_bytes + size > BATCH_BYTES):
                            flush()
                        pending.append(dict(zip(schema.names, values)))
                        pending_bytes += size
                if pending or not batches:
                    flush()
            tables_report.append({'oid': oid, 'schema': namespace, 'name': name,
                'path': str(oid), 'version': DeltaTable(path).version(), 'columns': columns,
                'rows': rows, 'batches': batches, 'max_arrow_batch_bytes': peak})
        scans = conn.execute('SELECT relid,seq_scan,seq_tup_read,idx_scan,idx_tup_fetch FROM pg_stat_xact_user_tables ORDER BY relid').fetchall()
        conn.execute('COMMIT')
    manifest = {'format_version': 1, 'status': 'files_complete', 'published': False,
        'id': config['id'], 'source': config['source'], 'database': 'postgres',
        'database_oid': actual[4], 'observed_at_ms': observed_at_ms, 'backend_pid': actual[2], 'transaction_snapshot': actual[3],
        'transaction': 'REPEATABLE READ READ ONLY', 'target': target, 'versions': versions,
        'tables': tables_report, 'omitted_relations': omitted,
        'text_semantics': 'UTF-8 values preserved; PostgreSQL collation/order/equality semantics are not reproduced',
        'export_compute_scans': [{'oid': r[0], 'seq_scan': r[1], 'seq_tup_read': r[2], 'idx_scan': r[3], 'idx_tup_fetch': r[4]} for r in scans],
        'elapsed_seconds': round(time.monotonic()-started, 3), 'files': []}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as f:
                while chunk := f.read(1024 * 1024):
                    digest.update(chunk)
                os.fsync(f.fileno())
            manifest['files'].append({'path': str(path.relative_to(root)), 'bytes': path.stat().st_size, 'sha256': digest.hexdigest()})
    boundary(config, root, len(json.dumps(manifest).encode()) * 2 + 4096)
    atomic_json(root / 'manifest.json', manifest)
    for path in [*root.rglob('*'), root]:
        if path.is_dir():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    used = boundary(config, root)
    return {'status': 'files_complete', 'published': False, 'id': config['id'],
            'source': config['source'], 'manifest': str(root / 'manifest.json'),
            'tables': len(tables_report), 'rows': sum(t['rows'] for t in tables_report),
            'bytes': used, 'elapsed_seconds': manifest['elapsed_seconds']}


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    try:
        result = export(config)
    except Rejected as error:
        result = {'status': 'failed', 'code': 'unsupported_or_budget', 'message': str(error)}
    except psycopg.DataError:
        result = {'status': 'failed', 'code': 'unsupported_value',
                  'message': 'value cannot be decoded in the supported finite range (including infinite/out-of-range dates and timestamps)'}
    except psycopg.Error as error:
        result = {'status': 'failed', 'code': 'postgres', 'sqlstate': error.sqlstate}
    except Exception as error:
        result = {'status': 'failed', 'code': type(error).__name__}
    atomic_json(config['report'], result)
    return 0 if result['status'] == 'files_complete' else 1


if __name__ == '__main__':
    sys.exit(main())
