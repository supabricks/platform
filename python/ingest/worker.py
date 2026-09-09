#!/usr/bin/env python3
"""Owned CSV staging/COPY/receipt worker. SQLite belongs exclusively to the daemon."""
import csv
import datetime as dt
from decimal import Decimal, localcontext, InvalidOperation
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time

import psycopg
from psycopg import sql
import psutil
import pyarrow as pa
import pyarrow.csv as arrow_csv

SOURCE_BYTES = 100 * 1024**2
DECODED_BYTES = 512 * 1024**2
RSS_BYTES = 512 * 1024**2
RECORD_BYTES = 1024**2
FREE_BYTES = 64 * 1024**2


class Rejected(Exception):
    pass


def atomic(path, value):
    raw = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()
    if len(raw) > 512 * 1024:
        raise Rejected('response_limit')
    temporary = path.with_suffix('.partial')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def boundary(config):
    if time.time() * 1000 >= config['deadline_ms']:
        raise Rejected('deadline')
    if psutil.Process().memory_info().rss > RSS_BYTES:
        raise Rejected('memory_limit')
    disk = os.statvfs(config['root'])
    if disk.f_bavail * disk.f_frsize < FREE_BYTES:
        raise Rejected('disk_reserve')


def regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    f = os.fdopen(fd, 'rb')
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        f.close()
        raise Rejected('regular_file_required')
    return f


def identity(meta):
    return meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns


def stage(config):
    with regular(config['path']) as original:
        before = os.fstat(original.fileno())
        if before.st_size > SOURCE_BYTES:
            raise Rejected('source_limit')
        disk = os.statvfs(config['root'])
        if disk.f_bavail * disk.f_frsize < FREE_BYTES + before.st_size:
            raise Rejected('disk_reserve')
        digest, size = hashlib.sha256(), 0
        fd = os.open(config['part'], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as target:
            while block := original.read(65536):
                boundary(config)
                size += len(block)
                if size > SOURCE_BYTES:
                    raise Rejected('source_limit')
                target.write(block)
                digest.update(block)
                progress(config, bytes_received=size)
            target.flush()
            os.fsync(target.fileno())
        if identity(before) != identity(os.fstat(original.fileno())) or size != before.st_size:
            raise Rejected('source_changed')
        # A rename/replacement during acquisition is also rejected. Subsequent
        # changes to the user's file cannot affect the private staged payload.
        if identity(before) != identity(os.stat(config['path'], follow_symlinks=False)):
            raise Rejected('source_changed')
    inspection = inspect_csv(config, config['part'])
    return dict(state='staged', bytes=size, sha256=digest.hexdigest(), inspection=inspection)


def inspect_uploaded(config):
    digest, size = hashlib.sha256(), 0
    with regular(config['path']) as stream:
        while block := stream.read(65536):
            boundary(config)
            size += len(block)
            if size > SOURCE_BYTES:
                raise Rejected('source_limit')
            digest.update(block)
    return dict(state='staged', bytes=size, sha256=digest.hexdigest(),
                inspection=inspect_csv(config, config['path']))


def first_record(path, delimiter):
    # Bound the stdlib header probe before Arrow allocates its column metadata.
    with regular(path) as raw:
        text = io.TextIOWrapper(raw, encoding='utf-8-sig', newline='')
        total = 0
        def lines():
            nonlocal total
            while True:
                line = text.readline(RECORD_BYTES + 1)
                if not line:
                    return
                total += len(line.encode('utf-8'))
                if total > RECORD_BYTES:
                    raise Rejected('record_limit')
                yield line
        csv.field_size_limit(RECORD_BYTES)
        row = next(csv.reader(lines(), delimiter=delimiter, strict=True), None)
        if not row or len(row) > 256:
            raise Rejected('column_limit')
        return row


def rows(config, path, options):
    count = len(first_record(path, options['delimiter']))
    names = ['f' + str(n) for n in range(count)]
    with regular(path) as source:
        reader = arrow_csv.open_csv(source,
            read_options=arrow_csv.ReadOptions(column_names=names, block_size=RECORD_BYTES, use_threads=False, encoding='utf8'),
            parse_options=arrow_csv.ParseOptions(delimiter=options['delimiter'], quote_char='"', double_quote=True,
                                                 newlines_in_values=True, ignore_empty_lines=False),
            convert_options=arrow_csv.ConvertOptions(column_types={n: pa.string() for n in names},
                null_values=options['null_strings'], strings_can_be_null=True, quoted_strings_can_be_null=False,
                check_utf8=True))
        decoded, first = 0, True
        for batch in reader:
            boundary(config)
            arrays = [batch.column(n) for n in range(count)]
            for index in range(batch.num_rows):
                values = [a[index].as_py() for a in arrays]
                size = sum(len(v.encode('utf-8')) if v is not None else 0 for v in values)
                if size > RECORD_BYTES:
                    raise Rejected('record_limit')
                decoded += size
                if decoded > DECODED_BYTES:
                    raise Rejected('decoded_limit')
                if first and options['header']:
                    first = False
                    continue
                first = False
                yield values, decoded


def inspect_csv(config, path):
    options = config['options']
    headers = first_record(path, options['delimiter'])
    used, columns = set(), []
    for n, header in enumerate(headers):
        candidate = (header if options['header'] else '') or f'column_{n+1}'
        candidate = candidate.replace('\0', '') or f'column_{n+1}'
        while len(candidate.encode()) > 55:
            candidate = candidate[:-1]
        base, suffix = candidate, 2
        while candidate in used:
            candidate = f'{base}_{suffix}'
            suffix += 1
        used.add(candidate)
        # Text is a lossless proposal: approval can opt into explicit conversion.
        columns.append(dict(input=str(n), name=candidate, data_type=dict(kind='text'), nullable=True))
    mapping = dict(version=1, format='csv', delimiter=options['delimiter'], header=options['header'],
                   null_strings=options['null_strings'], columns=columns)
    sample = []
    for values, _ in rows(config, path, options):
        if len(json.dumps(dict(mapping=mapping, rows=sample + [values]), ensure_ascii=True).encode()) > 240 * 1024:
            break
        sample.append(values)
        if len(sample) == 100:
            break
    return dict(mapping=mapping, rows=sample, sample_only=True)


def convert(value, column):
    if value is None:
        if not column['nullable']:
            raise Rejected('null_in_required_column')
        return None
    if '\0' in value:
        raise Rejected('nul_in_field')
    t = column['data_type']
    kind = t['kind']
    if kind == 'text':
        return value
    if kind == 'boolean':
        if value.lower() not in ('true', 'false'):
            raise Rejected('invalid_boolean')
        return value.lower() == 'true'
    if kind in ('smallint', 'integer', 'bigint'):
        if not re.fullmatch(r'[+-]?[0-9]+', value) or len(value) > 21:
            raise Rejected('invalid_integer')
        result = int(value)
        bits = {'smallint': 16, 'integer': 32, 'bigint': 64}[kind]
        if not -(2**(bits-1)) <= result < 2**(bits-1):
            raise Rejected('integer_overflow')
        return result
    if kind == 'decimal':
        if len(value) > 128:
            raise Rejected('decimal_overflow')
        with localcontext() as context:
            context.prec = 80
            result = Decimal(value)
            if not result.is_finite() or result != result.quantize(Decimal(1).scaleb(-t['scale'])) or abs(result) >= Decimal(10)**(t['precision']-t['scale']):
                raise Rejected('decimal_overflow_or_scale')
            return result
    if kind == 'double':
        result = float(value)
        if not math.isfinite(result):
            raise Rejected('nonfinite_double')
        return result
    if kind == 'date':
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise Rejected('invalid_date')
        return dt.date.fromisoformat(value)
    if kind in ('timestamp', 'timestamp_tz'):
        if not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', value) or re.search(r'\.\d{7}', value):
            raise Rejected('invalid_timestamp_precision')
        result = dt.datetime.fromisoformat(value)
        if (result.tzinfo is not None) != (kind == 'timestamp_tz'):
            raise Rejected('timestamp_timezone_mismatch')
        return result
    raise Rejected('unsupported_csv_type')


def pg_type(t):
    if t['kind'] == 'decimal':
        return sql.SQL('numeric({},{})').format(sql.Literal(t['precision']), sql.Literal(t['scale']))
    name = {'text':'text', 'boolean':'boolean', 'smallint':'smallint', 'integer':'integer', 'bigint':'bigint',
            'double':'double precision', 'date':'date', 'timestamp':'timestamp', 'timestamp_tz':'timestamptz'}.get(t['kind'])
    if name is None:
        raise Rejected('unsupported_csv_type')
    return sql.SQL(name)


def receipt_schema(conn, ddl=None):
    schema = conn.execute("SELECT oid FROM pg_namespace WHERE nspname='_supabricks'").fetchone()
    if schema is None:
        if ddl is None:
            return False
        conn.execute(ddl)
    expected = [('version','integer'),('origin','text'),('project_id','uuid'),('branch_id','uuid'),('job_id','uuid'),
                ('source_sha256','text'),('mapping_fingerprint','text'),('target_schema','text'),('target_table','text'),
                ('target_oid','oid'),('committed_rows','bigint')]
    columns = conn.execute("""SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
        FROM pg_attribute a WHERE a.attrelid=to_regclass('_supabricks.ingest_receipts') AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""").fetchall()
    if columns != [(name, kind, True) for name,kind in expected]:
        raise Rejected('receipt_schema_conflict')
    relation = conn.execute("""SELECT relkind,relpersistence,relrowsecurity,relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user),
        (SELECT count(*) FROM pg_trigger WHERE tgrelid=c.oid AND NOT tgisinternal)
        FROM pg_class c WHERE c.oid=to_regclass('_supabricks.ingest_receipts')""").fetchone()
    primary = conn.execute("""SELECT array_agg(a.attname ORDER BY k.ordinality) FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum,ordinality)
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.conrelid='_supabricks.ingest_receipts'::regclass AND c.contype='p'""").fetchone()[0]
    if relation != ('r','p',False,True,0) or primary != ['origin','project_id','branch_id','job_id']:
        raise Rejected('receipt_schema_conflict')
    return True


def connection(config):
    # Bootstrap logging policy with the daemon's private control credential, then
    # run every source/target statement as the ordinary database owner. The
    # backend remains owned control work during TTL application-role draining.
    conn = psycopg.connect(**config['connection'], connect_timeout=5,
        application_name='supabricks-ingest',
        options='-c statement_timeout=15000 -c lock_timeout=5000 -c search_path=pg_catalog -c client_encoding=UTF8 -c TimeZone=UTC -c log_statement=none -c log_min_error_statement=panic -c log_min_messages=panic')
    try:
        conn.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        conn.execute('SET ROLE supabricks_owner')
        return conn
    except BaseException:
        conn.close()
        raise


def lock(conn, config):
    # Serializes receipt reads with the original backend's transaction, including
    # a commit still in flight after the worker process was fenced.
    key = ':'.join([config['origin'], config['load']['project_id'], config['load']['branch_id'], config['job']])
    number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
    conn.execute('SELECT pg_advisory_xact_lock(%s)', (number,))


def target_oid(conn, load):
    row = conn.execute('SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s', (load['schema'],load['table'])).fetchone()
    return row[0] if row else None


def reconcile(config):
    load = config['load']
    with connection(config) as conn:
        lock(conn, config)
        exists = receipt_schema(conn)
        record = None
        if exists:
            record = conn.execute('SELECT version,origin,project_id::text,branch_id::text,job_id::text,source_sha256,mapping_fingerprint,target_schema,target_table,target_oid,committed_rows FROM _supabricks.ingest_receipts WHERE origin=%s AND project_id=%s AND branch_id=%s AND job_id=%s',
                (config['origin'],load['project_id'],load['branch_id'],config['job'])).fetchone()
        oid = target_oid(conn, load)
        if record is None:
            return dict(outcome='absent',target_exists=oid is not None)
        receipt = dict(zip(['version','origin','project_id','branch_id','job_id','source_sha256','mapping_fingerprint','schema','table','table_oid','committed_rows'], record))
        return dict(outcome='committed',receipt=receipt,target_oid=oid or 0)


_last_progress = 0

def progress(config, **value):
    global _last_progress
    if time.monotonic() - _last_progress >= 0.2:
        value['phase'] = config['mode']
        value['rss_bytes'] = psutil.Process().memory_info().rss
        atomic(Path(config['workspace']) / 'progress.json', value)
        _last_progress = time.monotonic()


def load_csv(config):
    load = config['load']
    mapping = load['mapping']
    if mapping['format'] != 'csv':
        raise Rejected('csv_only')
    with regular(config['source']) as f:
        before = os.fstat(f.fileno())
        if before.st_size > SOURCE_BYTES:
            raise Rejected('source_limit')
        digest = hashlib.sha256()
        while block := f.read(65536):
            boundary(config)
            digest.update(block)
        if digest.hexdigest() != load['source_sha256']:
            raise Rejected('staged_source_changed')
    width = len(first_record(config['source'],mapping['delimiter']))
    indices = [int(c['input']) for c in mapping['columns']]
    if len(indices) != width or sorted(indices) != list(range(width)):
        raise Rejected('mapping_requires_each_input_column_once')
    parsed, copied, decoded = 0, 0, 0
    with connection(config) as conn:
        conn.execute('SET statement_timeout=0')
        lock(conn,config)
        if receipt_schema(conn):
            existing=conn.execute('SELECT 1 FROM _supabricks.ingest_receipts WHERE origin=%s AND project_id=%s AND branch_id=%s AND job_id=%s',
                (config['origin'],load['project_id'],load['branch_id'],config['job'])).fetchone()
            if existing:
                return dict(state='finished')  # coordinator validates existing evidence
        if target_oid(conn,load) is not None:
            return dict(state='rejected_before_load',error='target_exists')
        receipt_schema(conn,config['receipt_ddl'])
        target = sql.Identifier(load['schema'],load['table'])
        fields = [sql.SQL('{} {} {}').format(sql.Identifier(c['name']),pg_type(c['data_type']),sql.SQL('' if c['nullable'] else 'NOT NULL')) for c in mapping['columns']]
        conn.execute(sql.SQL('CREATE TABLE {} ({})').format(target,sql.SQL(',').join(fields)))
        with conn.cursor().copy(sql.SQL('COPY {} ({}) FROM STDIN').format(target,sql.SQL(',').join(sql.Identifier(c['name']) for c in mapping['columns']))) as copy:
            for values, decoded in rows(config,config['source'],mapping):
                parsed += 1
                copy.write_row([convert(values[i],c) for i,c in zip(indices,mapping['columns'])])
                copied += 1
                if copied % 1024 == 0:
                    boundary(config)
                    progress(config,parsed_rows=parsed,copied_rows=copied,decoded_bytes=decoded)
        if identity(before) != identity(os.stat(config['source'],follow_symlinks=False)):
            raise Rejected('staged_source_changed')
        boundary(config)
        oid = target_oid(conn,load)
        conn.execute('INSERT INTO _supabricks.ingest_receipts VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (config['origin'],load['project_id'],load['branch_id'],config['job'],load['source_sha256'],config['fingerprint'],load['schema'],load['table'],oid,copied))
        atomic(Path(config['workspace'])/'progress.json',dict(phase='committing',parsed_rows=parsed,copied_rows=copied))
    return dict(state='finished',parsed_rows=parsed,copied_rows=copied,decoded_bytes=decoded)


def main(config):
    done = threading.Event()
    started=time.monotonic()
    peak=psutil.Process().memory_info().rss
    def watch():
        nonlocal peak
        while not done.wait(0.05):
            try:
                peak=max(peak,psutil.Process().memory_info().rss)
                boundary(config)
            except BaseException as error:
                try:
                    atomic(Path(config['workspace'])/'result.json',dict(ok=False,error=str(error) if isinstance(error,Rejected) else 'worker_watchdog',metrics=dict(peak_rss_bytes=peak)))
                finally:
                    os._exit(86)
    threading.Thread(target=watch,daemon=True).start()
    try:
        result = {'stage':stage,'uploaded':inspect_uploaded,'preview':inspect_uploaded,'load':load_csv,'reconcile':reconcile}[config['mode']](config)
        atomic(Path(config['workspace'])/'result.json',dict(ok=True,value=result,metrics=dict(peak_rss_bytes=max(peak,psutil.Process().memory_info().rss),duration_ms=int((time.monotonic()-started)*1000))))
    except BaseException as error:
        # Never put parser messages, source data, identifiers or connection URIs
        # in logs. Retain only stable error categories and safe row counters.
        code = str(error) if isinstance(error,Rejected) else (
            'invalid_csv' if isinstance(error,(pa.ArrowException,csv.Error,UnicodeError)) else
            'invalid_value' if isinstance(error,(ValueError,InvalidOperation,OverflowError)) else
            'postgres_' + (error.sqlstate or 'unavailable') if isinstance(error,psycopg.Error) else 'io_or_worker_failure')
        atomic(Path(config['workspace'])/'result.json',dict(ok=False,error=code))
    finally:
        done.set()


if __name__ == '__main__':
    os.umask(0o077)
    with open(sys.argv[2],'rb') as stream:
        raw = stream.read(65537)
    if len(raw)>65536:
        raise SystemExit(2)
    main(json.loads(raw))
