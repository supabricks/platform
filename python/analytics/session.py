"""One bounded Sail catalog pinned to one immutable Supabricks epoch.

The local OS user and raw Python clients are trusted. The managed SQL entry
point accepts read queries only. This is not a sandbox for hostile UDFs.
"""
import argparse
import json
import os
from pathlib import Path
import re
import threading
import time


def atomic_json(path, value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
    tmp = path.with_suffix('.tmp')
    with tmp.open('wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def read_sql(sql):
    """Conservative lexical gate, not a general SQL parser.

    Quoted identifiers/strings and nested comments cannot hide statement
    separators. Sail still parses the accepted single query normally.
    """
    tokens = []
    i = 0
    while i < len(sql):
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith('--', i):
            end = sql.find('\n', i + 2)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith('/*', i):
            depth = 1
            i += 2
            while i < len(sql) and depth:
                if sql.startswith('/*', i):
                    depth += 1
                    i += 2
                elif sql.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise ValueError('unterminated SQL comment')
        elif c in "'\"`":
            quote = c
            i += 1
            while i < len(sql):
                if sql[i] == '\\' and quote != '`':
                    i += 2
                elif sql[i] == quote:
                    i += 1
                    if i < len(sql) and sql[i] == quote:
                        i += 1
                    else:
                        break
                else:
                    i += 1
            else:
                raise ValueError('unterminated SQL literal or identifier')
            tokens.append('QUOTED')
        elif c == ';':
            raise ValueError('one query only; omit the statement separator')
        elif c.isalpha() or c == '_':
            match = re.match(r'[\w$]+', sql[i:])
            tokens.append(match[0].upper())
            i += len(match[0])
        else:
            i += 1
    forbidden = {'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'CREATE', 'DROP', 'ALTER',
                 'TRUNCATE', 'REPLACE', 'COPY', 'LOAD', 'CALL', 'SET', 'RESET',
                 'USE', 'CACHE', 'UNCACHE', 'REFRESH', 'VACUUM', 'OPTIMIZE',
                 'GRANT', 'REVOKE', 'INTO', 'TRANSFORM', 'SCRIPT', 'ADD', 'REMOVE'}
    if not tokens or tokens[0] not in {'SELECT', 'WITH', 'EXPLAIN'} or forbidden.intersection(tokens):
        raise ValueError('managed analytics SQL accepts SELECT/WITH/EXPLAIN read queries only')
    if tokens[0] == 'EXPLAIN' and not {'SELECT', 'WITH'}.intersection(tokens):
        raise ValueError('EXPLAIN requires a read query')
    return sql


def identifier(value):
    return '`' + value.replace('`', '``') + '`'


def literal(value):
    return "'" + value.replace('\\', '\\\\').replace("'", "''") + "'"


def query(spark, request, metadata):
    read_sql(request['sql'])
    frame = spark.sql(request['sql']).limit(request['max_rows'] + 1)
    result = {'id': request['id'], 'session_id': metadata['session_id'],
              'epoch_id': metadata['epoch_id'], 'state': 'complete',
              'columns': [{'name': f.name, 'type': f.dataType.simpleString()}
                          for f in frame.schema.fields],
              'rows': [], 'truncated': False, 'read_only': True}
    base_bytes = len(json.dumps(result).encode()) + 64
    if base_bytes > request['max_bytes']:
        raise ValueError('result schema exceeds byte budget')
    # limit bounds row count in the plan; the engine memory pool and RSS watchdog
    # also apply before serialization. No unbounded collect()/toPandas().
    for row in frame.toLocalIterator():
        values = [None if v is None else str(v) for v in row]
        size = len(json.dumps(values, ensure_ascii=True).encode()) + 2
        if len(result['rows']) >= request['max_rows'] or base_bytes + size > request['max_bytes']:
            result['truncated'] = True
            break
        result['rows'].append(values)
        base_bytes += size
    result['result_bytes'] = len(json.dumps(result).encode())
    for _ in range(3):
        result['result_bytes'] = len(json.dumps(result).encode())
    return result


def validate_decimal_statistics(generation, tables):
    for table in tables:
        decimals = {c['name'] for c in table['columns'] if c['type_oid'] == 1700}
        if not decimals:
            continue
        for version in range(table['version'] + 1):
            log = generation / table['path'] / '_delta_log' / f'{version:020}.json'
            with log.open() as lines:
                while line := lines.readline(2 * 1024**2 + 1):
                    if len(line) > 2 * 1024**2:
                        raise ValueError('Delta metadata exceeds session bootstrap budget')
                    add = json.loads(line).get('add', {})
                    stats = json.loads(add.get('stats') or '{}')
                    if any(decimals.intersection(stats.get(k, {})) for k in ('minValues', 'maxValues')):
                        raise ValueError('epoch contains unsafe decimal statistics; run analytics refresh to export a new safe epoch')


def run(config):
    os.environ['TZ'] = 'UTC'
    time.tzset()
    workspace = Path(config['workspace'])
    deadline = config['expires_at_ms']
    monotonic_deadline = time.monotonic() + max(0, min(3600, (deadline - time.time()*1000)/1000))
    os.environ.update({
        'SAIL_MODE': 'local',
        'SAIL_RUNTIME__MEMORY_POOL__TYPE': 'fair',
        'SAIL_RUNTIME__MEMORY_POOL__FAIR__MAX_SIZE': str(256 * 1024**2),
        'SAIL_RUNTIME__TEMPORARY_FILES__MAX_SIZE': str(256 * 1024**2),
        'SAIL_RUNTIME__TEMPORARY_FILES__PATHS': json.dumps([str(workspace / 'spill')]),
        'SAIL_SPARK__SESSION_TIMEOUT_SECS': '3600',
        'TOKIO_WORKER_THREADS': '2', 'RAYON_NUM_THREADS': '2',
        'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1',
        'RUST_LOG': 'error',
    })
    (workspace / 'spill').mkdir(exist_ok=True)
    import importlib.metadata
    for package, expected in {'pysail': '0.7.1', 'pyspark-client': '4.2.0',
                              'deltalake': '1.6.3', 'pyarrow': '25.0.1'}.items():
        if importlib.metadata.version(package) != expected:
            raise ValueError(f'{package} must match the qualified version {expected}; restore the locked environment')
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
    import psutil
    process = psutil.Process()
    def watchdog():
        while True:
            if time.monotonic() >= monotonic_deadline or time.time() * 1000 >= deadline or process.memory_info().rss > 1024**3:
                # A nonzero exit is visible to the owner. The daemon stops the
                # verified entire group before releasing the durable reference.
                atomic_json(workspace / 'failure.json', {'error': 'session lifetime or 1 GiB sampled RSS limit exceeded'})
                os._exit(75)
            time.sleep(.1)
    threading.Thread(target=watchdog, daemon=True).start()
    from pysail.spark import SparkConnectServer
    from pyspark.sql import SparkSession
    server = SparkConnectServer(ip='127.0.0.1', port=0)
    server.start()
    _, port = server.listening_address
    metadata = config['metadata']
    endpoint = f"sc://127.0.0.1:{port}/;user_id=supabricks;session_id={metadata['session_id']}"
    spark = SparkSession.builder.remote(endpoint).getOrCreate()
    descriptor = config['descriptor']
    root = Path(config['root']).resolve()
    generation = (root / descriptor['generation']).resolve()
    if not generation.is_relative_to(root / 'analytics' / 'generations'):
        raise ValueError('generation must be inside the analytical store')
    sail_generation = generation
    if config.get('sail_workspace'):
        # Sail 0.7.1's Delta prefix treats URL-encoded names as literal paths.
        # The daemon owns this ASCII alias until worker death, including recovery.
        alias = Path(config['sail_workspace'])
        if alias.resolve() != workspace.resolve():
            raise ValueError('analytical workspace alias changed')
        (workspace / 'snapshot').symlink_to(generation, target_is_directory=True)
        sail_generation = alias / 'snapshot'
    tables = descriptor['manifest']['tables']
    validate_decimal_statistics(generation, tables)
    schemas = {t['schema'] for t in tables}
    if len({s.lower() for s in schemas}) != len(schemas):
        raise ValueError('Postgres schemas collide under Spark case-insensitive name resolution')
    names = [(t['schema'].lower(), t['name'].lower()) for t in tables]
    if len(set(names)) != len(names) or any(len({c['name'].lower() for c in t['columns']}) != len(t['columns']) for t in tables):
        raise ValueError('Postgres table or column names collide under Spark case-insensitive name resolution')
    reserved = {'_supabricks', '_supabricks_source'}
    if any(t['schema'].lower() in reserved for t in tables):
        raise ValueError('source schema conflicts with reserved analytical metadata namespace')
    spark.conf.set('spark.sql.session.timeZone', 'UTC')
    spark.sql('CREATE DATABASE _supabricks_source').collect()
    spark.sql('CREATE DATABASE _supabricks').collect()
    for schema in sorted({t['schema'] for t in tables} | {'public'}):
        spark.sql(f'CREATE DATABASE IF NOT EXISTS {identifier(schema)}').collect()
    for table in tables:
        source = f"_supabricks_source.t_{table['oid']}"
        path = str(sail_generation / table['path'])
        spark.sql(f'CREATE TABLE {source} USING delta LOCATION {literal(path)}').collect()
        name = identifier(table['schema']) + '.' + identifier(table['name'])
        spark.sql(f"CREATE VIEW {name} AS SELECT * FROM {source} VERSION AS OF {int(table['version'])}").collect()
    fields = ', '.join(f'{literal(str(v)) if v is not None else "CAST(NULL AS STRING)"} AS {identifier(k)}'
                       for k, v in metadata.items() if k != 'source')
    fields += f", {literal(json.dumps(metadata, sort_keys=True))} AS metadata_json"
    spark.sql(f'CREATE VIEW _supabricks.epoch AS SELECT {fields}').collect()
    spark.sql('USE DATABASE public').collect()
    spark.conf.set('supabricks.epoch_id', metadata['epoch_id'])
    spark.sql('SELECT * FROM _supabricks.epoch').first()
    atomic_json(workspace / 'ready.json', {'session_id': metadata['session_id'], 'port': port})
    last = None
    while True:
        path = workspace / 'query.json'
        if path.exists():
            request = json.loads(path.read_text())
            if request['id'] != last:
                last = request['id']
                try:
                    result = query(spark, request, metadata)
                except Exception as error:
                    result = {'id': last, 'epoch_id': metadata['epoch_id'],
                              'session_id': metadata['session_id'], 'state': 'failed',
                              'error': f'{type(error).__name__}: {error}'[:4096]}
                atomic_json(workspace / 'result.json', result)
        time.sleep(.05)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.input.read_text())
    try:
        run(config)
    except Exception as error:
        atomic_json(Path(config['workspace']) / 'failure.json', {'error': f'{type(error).__name__}: {error}'[:4096]})
        raise
