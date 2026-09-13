#!/usr/bin/env python3
"""Owned multi-format staging/COPY/receipt worker. SQLite belongs exclusively to the daemon."""
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
import pyarrow.parquet as arrow_parquet
from psycopg.types.json import Jsonb

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
    inspection = inspect_source(config, config['part'])
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
                inspection=inspect_source(config, config['path']))


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
            'double':'double precision', 'date':'date', 'timestamp':'timestamp', 'timestamp_tz':'timestamptz', 'jsonb':'jsonb'}.get(t['kind'])
    if name is None:
        raise Rejected('unsupported_csv_type')
    return sql.SQL(name)


# Added beside the existing CSV reader; all formats share staging and COPY receipts.
JSON_BYTES = 10 * 1024**2
MAX_DEPTH = 64
MISSING = object()


def json_text(value, depth=0):
    """Encode exact JSON numbers without converting Decimal through float."""
    if depth > MAX_DEPTH:
        raise Rejected('json_depth_limit')
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, Decimal)):
        if isinstance(value, Decimal) and not value.is_finite():
            raise Rejected('nonfinite_json_number')
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Rejected('nonfinite_json_number')
        return json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        if '\0' in value:
            raise Rejected('nul_in_field')
        value.encode('utf-8')  # Reject lone Unicode surrogates.
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, list):
        return '[' + ','.join(json_text(v, depth+1) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(json_text(k, depth+1)+':'+json_text(v, depth+1) for k,v in value.items()) + '}'
    raise Rejected('unsupported_nested_type')


def decode_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise Rejected('duplicate_json_key')
            result[key] = value
        return result
    def constant(_):
        raise Rejected('nonfinite_json_number')
    value = json.loads(raw, parse_float=Decimal, object_pairs_hook=pairs, parse_constant=constant)
    json_text(value)  # Apply depth, Unicode and NUL limits to every value.
    return value


def json_records(config, path, format):
    with regular(path) as source:
        if format == 'json_lines':
            first = True
            while raw := source.readline(RECORD_BYTES + 1):
                boundary(config)
                if len(raw) > RECORD_BYTES:
                    raise Rejected('record_limit')
                value = decode_json(raw.decode('utf-8-sig' if first else 'utf-8'))
                first = False
                if not isinstance(value, dict):
                    raise Rejected('json_object_required')
                yield value
        else:
            if os.fstat(source.fileno()).st_size > JSON_BYTES:
                raise Rejected('json_source_limit')
            raw = source.read(JSON_BYTES + 1)
            if len(raw) > JSON_BYTES:
                raise Rejected('json_source_limit')
            boundary(config)
            value = decode_json(raw.decode('utf-8-sig'))
            if format == 'json_document':
                yield {'$': value}
            elif format == 'json_array':
                if not isinstance(value, list):
                    raise Rejected('json_array_required_use_document_mode')
                for item in value:
                    boundary(config)
                    if not isinstance(item, dict):
                        raise Rejected('json_object_required')
                    yield item
            else:
                raise Rejected('unsupported_format')


def parquet_type(t, nested=False):
    if pa.types.is_null(t): return dict(kind='jsonb')
    if pa.types.is_boolean(t): return dict(kind='boolean')
    if pa.types.is_string(t) or pa.types.is_large_string(t): return dict(kind='text')
    if pa.types.is_integer(t):
        if pa.types.is_unsigned_integer(t):
            if t.bit_width == 64: return dict(kind='decimal', precision=20, scale=0)
            bits = t.bit_width * 2
        else: bits = t.bit_width
        return dict(kind='smallint' if bits <= 16 else 'integer' if bits <= 32 else 'bigint')
    if pa.types.is_floating(t): return dict(kind='double')
    if pa.types.is_decimal(t):
        if t.precision > 38 or not 0 <= t.scale <= t.precision:
            raise Rejected('unsupported_parquet_decimal')
        return dict(kind='decimal',precision=t.precision,scale=t.scale)
    if not nested and pa.types.is_date(t): return dict(kind='date')
    if not nested and pa.types.is_timestamp(t): return dict(kind='timestamp_tz' if t.tz else 'timestamp')
    if pa.types.is_struct(t):
        if len(set(t.names)) != len(t): raise Rejected('duplicate_parquet_field')
        for field in t: parquet_type(field.type, True)
        return dict(kind='jsonb')
    if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t):
        parquet_type(t.value_type, True)
        return dict(kind='jsonb')
    raise Rejected('unsupported_parquet_type')


def parquet_open(source):
    # Bound footer parsing and decoded row groups before requesting any batches.
    size = os.fstat(source.fileno()).st_size
    if size < 12: raise Rejected('invalid_parquet_footer')
    source.seek(-8, 2)
    footer = source.read(8)
    if footer[4:] != b'PAR1' or not 0 < int.from_bytes(footer[:4],'little') <= min(8*1024**2, size-12):
        raise Rejected('parquet_footer_limit')
    source.seek(0)
    reader = arrow_parquet.ParquetFile(source, thrift_string_size_limit=1024**2, thrift_container_size_limit=65536, page_checksum_verification=True)
    schema = reader.schema_arrow
    if not 1 <= len(schema) <= 256 or len(set(schema.names)) != len(schema):
        raise Rejected('column_limit_or_duplicate')
    for field in schema: parquet_type(field.type)
    decoded = sum(reader.metadata.row_group(i).total_byte_size for i in range(reader.metadata.num_row_groups))
    if decoded > DECODED_BYTES: raise Rejected('decoded_limit')
    return reader


def parquet_records(config, path):
    try:
        with regular(path) as source:
            reader = parquet_open(source)
            for batch in reader.iter_batches(batch_size=128, use_threads=False):
                boundary(config)
                if batch.nbytes > DECODED_BYTES: raise Rejected('decoded_limit')
                # PostgreSQL timestamps have microsecond precision. Safe casts reject
                # nonzero nanoseconds rather than truncating them via datetime.
                fields = [pa.field(f.name, pa.timestamp('us',tz=f.type.tz),nullable=f.nullable)
                          if pa.types.is_timestamp(f.type) else f for f in batch.schema]
                batch = batch.cast(pa.schema(fields), safe=True)
                for i in range(batch.num_rows):
                    yield {str(n):batch.column(n)[i].as_py() for n in range(batch.num_columns)}
    except (OSError, pa.ArrowException) as error:
        raise Rejected('invalid_parquet') from error


def typed_records(config, path, format):
    values = parquet_records(config,path) if format == 'parquet' else json_records(config,path,format)
    decoded = 0
    for record in values:
        boundary(config)
        if len(record) > 256 or any(not k or len(k.encode()) > 1024 or '\0' in k for k in record):
            raise Rejected('json_key_or_column_limit')
        size = sum(len(display(v).encode()) + len(k.encode()) for k,v in record.items())
        if size > (JSON_BYTES if format == 'json_document' else RECORD_BYTES):
            raise Rejected('record_limit')
        decoded += size
        if decoded > DECODED_BYTES: raise Rejected('decoded_limit')
        yield record, decoded


def display(value):
    if isinstance(value,(dt.datetime, dt.date)): return value.isoformat()
    if isinstance(value,str):
        json_text(value)
        return value
    return json_text(value)


def json_type(values):
    kinds = {type(v) for v in values if v is not None and v is not MISSING}
    if kinds == {str}: return dict(kind='text')
    if kinds == {bool}: return dict(kind='boolean')
    if kinds and kinds <= {int, Decimal}:
        nums = [Decimal(v) for v in values if v is not None and v is not MISSING]
        scale = max(max(0,-v.as_tuple().exponent) for v in nums)
        if kinds == {int} and all(-(2**63) <= v < 2**63 for v in nums): return dict(kind='bigint')
        if scale <= 38 and all(v.is_zero() or v.adjusted() < 38-scale for v in nums):
            return dict(kind='decimal',precision=38,scale=scale)
    return dict(kind='jsonb')


def column_names(headers):
    used = set()
    for n, header in enumerate(headers):
        candidate = header or f'column_{n+1}'
        while len(candidate.encode()) > 55: candidate = candidate[:-1]
        base, suffix = candidate, 2
        while candidate in used:
            candidate=f'{base}_{suffix}'; suffix+=1
        used.add(candidate)
        yield candidate


def inspect_source(config, path):
    options = config['options']
    format = options.get('format','csv')
    if format == 'csv': return inspect_csv(config,path)
    sample, keys, source_schema = [], [], None
    if format == 'parquet':
        with regular(path) as source:
            reader = parquet_open(source)
            schema = reader.schema_arrow
            keys = [str(i) for i in range(len(schema))]
            types = [parquet_type(f.type) for f in schema]
            headers = schema.names
            source_schema = [dict(input=str(i), name=f.name, arrow_type=str(f.type), nullable=f.nullable) for i,f in enumerate(schema)]
    for record,_ in typed_records(config,path,format):
        sample.append(record)
        if format != 'parquet':
            for key in record:
                if key not in keys: keys.append(key)
        if len(keys) > 256: raise Rejected('column_limit')
        if len(sample) >= 100 or sum(len(display(v).encode()) for r in sample for v in r.values()) > 120*1024: break
    if not keys: raise Rejected('empty_schema')
    if format != 'parquet':
        types = [dict(kind='jsonb') if format == 'json_document' else json_type([r.get(k,MISSING) for r in sample]) for k in keys]
        headers = ['document'] if format == 'json_document' else keys
    columns = [dict(input=k,name=name,data_type=t,nullable=True) for k,name,t in zip(keys,column_names(headers),types)]
    mapping = dict(version=1,format=format,delimiter=',',header=True,null_strings=[],columns=columns)
    rows = []
    result = dict(mapping=mapping,rows=rows,sample_only=True,source_schema=source_schema)
    for record in sample:
        row = [None if k not in record or (record[k] is None and (t['kind'] != 'jsonb' or format == 'parquet')) else json_text(record[k]) if t['kind'] == 'jsonb' else display(record[k]) for k,t in zip(keys,types)]
        rows.append(row)
        if len(json.dumps(result,ensure_ascii=True).encode()) > 240*1024:
            rows.pop(); break
    return result


def convert_typed(value, column, format):
    if value is MISSING or (value is None and (column['data_type']['kind'] != 'jsonb' or format == 'parquet')):
        return convert(None,column)
    kind = column['data_type']['kind']
    if kind == 'jsonb': return Jsonb(value,dumps=json_text)
    if value is None: return convert(None,column)
    # A typed input may not silently become a string, number or boolean.
    allowed = {'text':(str,), 'boolean':(bool,), 'smallint':(int,), 'integer':(int,), 'bigint':(int,),
               'decimal':(int,Decimal), 'double':(int,float,Decimal), 'date':(str,dt.date),
               'timestamp':(str,dt.datetime), 'timestamp_tz':(str,dt.datetime)}
    if type(value) not in allowed.get(kind,()): raise Rejected('source_type_mismatch')
    return convert(display(value),column)


def load_rows(config, path, mapping):
    if mapping['format'] == 'csv':
        width = len(first_record(path,mapping['delimiter']))
        indices = [int(c['input']) for c in mapping['columns']]
        if len(indices) != width or sorted(indices) != list(range(width)):
            raise Rejected('mapping_requires_each_input_column_once')
        for values,decoded in rows(config,path,mapping):
            yield [convert(values[i],c) for i,c in zip(indices,mapping['columns'])],decoded
        return
    inputs = [c['input'] for c in mapping['columns']]
    if len(set(inputs)) != len(inputs): raise Rejected('duplicate_input')
    format = mapping['format']
    if format == 'json_document' and (inputs != ['$'] or mapping['columns'][0]['data_type']['kind'] != 'jsonb'):
        raise Rejected('document_requires_jsonb')
    if format == 'parquet':
        with regular(path) as source:
            count = len(parquet_open(source).schema_arrow)
        if set(inputs) != {str(i) for i in range(count)}: raise Rejected('mapping_requires_each_input_column_once')
    for record,decoded in typed_records(config,path,format):
        if set(record) - set(inputs): raise Rejected('unmapped_json_key')
        yield [convert_typed(record.get(c['input'],MISSING),c,format) for c in mapping['columns']], decoded


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


def load_source(config):
    load = config['load']
    mapping = load['mapping']
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
            for values, decoded in load_rows(config,config['source'],mapping):
                parsed += 1
                copy.write_row(values)
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
        result = {'stage':stage,'uploaded':inspect_uploaded,'preview':inspect_uploaded,'load':load_source,'reconcile':reconcile}[config['mode']](config)
        atomic(Path(config['workspace'])/'result.json',dict(ok=True,value=result,metrics=dict(peak_rss_bytes=max(peak,psutil.Process().memory_info().rss),duration_ms=int((time.monotonic()-started)*1000))))
    except BaseException as error:
        # Never put parser messages, source data, identifiers or connection URIs
        # in logs. Retain only stable error categories and safe row counters.
        code = str(error) if isinstance(error,Rejected) else (
            'invalid_source' if isinstance(error,(pa.ArrowException,csv.Error,UnicodeError,json.JSONDecodeError,RecursionError)) else
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
