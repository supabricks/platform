"""EQ232 diagnostic COPY phase and storage observations; no transaction rewriting.

The frozen load harness gets a transparent connection proxy. Events contain only
counts, timings, fixed query labels and backend IDs; never SQL, DSNs or values.
"""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.request

LSN_SQL = 'SELECT pg_current_wal_insert_lsn()::text'
LIMIT = 64 * 1024**2


class Ledger:
    def __init__(self, path, limit=LIMIT):
        self.stream = Path(path).open('x'); Path(path).chmod(0o600)
        self.limit = limit; self.bytes = self.errors = self.dropped = 0
    def emit(self, value):
        try:
            wire = json.dumps(value, allow_nan=False, separators=(',', ':')) + '\n'
            size = len(wire.encode())
            if self.bytes + size > self.limit:
                self.dropped += 1; return
            self.stream.write(wire); self.stream.flush(); self.bytes += size
        except Exception:
            self.errors += 1
    def close(self):
        self.stream.close()
        return dict(bytes=self.bytes, errors=self.errors, dropped=self.dropped)


class TimedConnection:
    def __init__(self, db, ledger):
        self.db = db; self.ledger = ledger; self.pending = None
        self.backend_pid = db.info.backend_pid
        self.rows = self.transactions = 0; self.phase = 'outside_transaction'
    def __getattr__(self, name):
        return getattr(self.db, name)
    def __enter__(self):
        self.db.__enter__(); return self
    def __exit__(self, *args):
        return self.db.__exit__(*args)
    def timed(self, label, call):
        self.phase = label; start = time.perf_counter_ns()
        try:
            return call()
        finally:
            if self.pending is not None:
                self.pending[label + '_ns'] = self.pending.get(label + '_ns', 0) + time.perf_counter_ns() - start
            self.phase = 'between_calls'
    @contextmanager
    def transaction(self, *args, **kwargs):
        assert self.pending is None, 'diagnostic expects frozen non-nested COPY transactions'
        self.pending = dict(rows=0, encoded_bytes=0, start_ns=time.monotonic_ns(), start_unix_ns=time.time_ns())
        context = self.db.transaction(*args, **kwargs)
        success = False
        try:
            self.timed('begin', context.__enter__)
            try:
                yield self
            except BaseException as error:
                # Pass the original exception through the real transaction owner.
                suppress = self.timed('rollback', lambda: context.__exit__(type(error), error, error.__traceback__))
                if not suppress:
                    raise
            else:
                self.timed('commit', lambda: context.__exit__(None, None, None)); success = True
        finally:
            end = time.monotonic_ns()
            event = dict(self.pending, end_ns=end, end_unix_ns=time.time_ns(), success=success,
                         backend_pid=self.backend_pid, ordinal=self.transactions + 1)
            if success:
                self.rows += event['rows']; self.transactions += 1
            event['committed_rows'] = self.rows
            self.ledger.emit(event); self.pending = None; self.phase = 'outside_transaction'
    def execute(self, query, *args, **kwargs):
        if self.pending is not None and query == LSN_SQL:
            return self.timed('lsn_query', lambda: self.db.execute(query, *args, **kwargs))
        return self.db.execute(query, *args, **kwargs)
    def cursor(self, *args, **kwargs):
        return TimedCursor(self, self.db.cursor(*args, **kwargs))


class TimedCursor:
    def __init__(self, owner, cursor):
        self.owner = owner; self.cursor = cursor
    def __getattr__(self, name):
        return getattr(self.cursor, name)
    @contextmanager
    def copy(self, *args, **kwargs):
        context = self.cursor.copy(*args, **kwargs); owner = self.owner
        copy = owner.timed('copy_start', context.__enter__)
        try:
            yield TimedCopy(owner, copy)
        except BaseException as error:
            suppress = owner.timed('copy_abort', lambda: context.__exit__(type(error), error, error.__traceback__))
            if not suppress:
                raise
        else:
            owner.timed('copy_finish', lambda: context.__exit__(None, None, None))


class TimedCopy:
    def __init__(self, owner, copy):
        self.owner = owner; self.copy = copy
    def write(self, data):
        result = self.owner.timed('copy_write', lambda: self.copy.write(data))
        # Frozen text COPY escapes embedded newlines; no decoding or source values retained.
        self.owner.pending['rows'] += data.count(b'\n')
        self.owner.pending['encoded_bytes'] += len(data)
        return result


def prometheus(url):
    with urllib.request.urlopen(url, timeout=1) as response:
        data = response.read(4 * 1024**2 + 1)
    if len(data) > 4 * 1024**2:
        raise ValueError('metrics response ceiling')
    values = {}
    for line in data.decode().splitlines():
        match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})? ([^ ]+)(?: .*)?', line)
        if not match:
            continue
        name, labels, raw = match.groups()
        if name.endswith('_bucket') or not name.startswith(('pageserver_', 'safekeeper_', 'process_')):
            continue
        if not any(x in name.lower() for x in ('page', 'wal', 'flush', 'fsync', 'disk', 'write', 'read', 'cache', 'io_', 'cpu')):
            continue
        # Keep request-kind labels, never tenant/timeline/path identifiers.
        kept = re.findall(r'(?:^|,)(request_type|query_type|operation|le)="([a-zA-Z0-9_.+\-]+)"', labels or '')
        key = name + ''.join('|' + k + '=' + v for k, v in sorted(kept))
        value = float(raw)
        if math.isfinite(value):
            values[key] = values.get(key, 0) + value
    return values


class Observer:
    def __init__(self, cell, connect, root):
        self.cell = cell; self.connect = connect; self.root = Path(root)
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.ledger = Ledger(self.root / 'transactions.jsonl')
        self.samples = Ledger(self.root / 'storage.jsonl')
        self.writer = None; self.stop_event = threading.Event(); self.thread = None
        self.capabilities = {}; self.errors = []; self.sample_ns = 0; self.sample_count = 0; self.settings = {}
    def prepare(self):
        """Install diagnostic views before capture's DDL fence is established."""
        if hasattr(self, 'neon_schema'):
            return
        with self.connect() as setup:
            setup.execute('CREATE SCHEMA IF NOT EXISTS eq232_diagnostics')
            setup.execute('CREATE EXTENSION IF NOT EXISTS neon WITH SCHEMA eq232_diagnostics')
            self.neon_schema = setup.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='neon'").fetchone()[0]
    def source(self):
        db = self.connect()
        # A single connection owns this fixture's serial load; the sampler uses connect directly.
        if self.writer is None:
            db.execute("SET track_io_timing=on")
            db.execute("SET track_wal_io_timing=on")
            self.writer = TimedConnection(db, self.ledger)
            self.prepare()
            with self.connect() as setup:
                import psycopg
                setup.execute(psycopg.sql.SQL('SET search_path TO {}, public, pg_catalog').format(psycopg.sql.Identifier(self.neon_schema)))
                self.settings = dict(setup.execute("SELECT name,setting FROM pg_settings WHERE name IN ('wal_level','synchronous_commit','fsync','full_page_writes','shared_buffers','effective_cache_size','track_io_timing','track_wal_io_timing') OR name IN ('neon.file_cache_size_limit','neon.max_file_cache_size','neon.file_cache_chunk_size','neon.readahead_buffer_size','neon.store_prefetch_result_in_lfc')").fetchall())
                self.settings['writer_track_io_timing'] = db.execute('SHOW track_io_timing').fetchone()[0]
                self.settings['writer_track_wal_io_timing'] = db.execute('SHOW track_wal_io_timing').fetchone()[0]
            self.thread = threading.Thread(target=self.run, name='eq232-observer', daemon=True); self.thread.start()
            return self.writer
        return db
    def run(self):
        import psycopg
        try:
            with self.connect() as db:
                db.execute("SET statement_timeout='2s'")
                db.execute(psycopg.sql.SQL('SET search_path TO {}, public, pg_catalog').format(psycopg.sql.Identifier(self.neon_schema)))
                views = {name: db.execute('SELECT to_regclass(%s) IS NOT NULL', (name,)).fetchone()[0]
                         for name in ('neon_backend_perf_counters', 'neon_stat_file_cache')}
                self.capabilities = views
                last = 0
                while not self.stop_event.is_set():
                    start = time.monotonic_ns(); writer = self.writer
                    row = dict(at_ns=start, at_unix_ns=time.time_ns(), committed_rows=writer.rows,
                               phase=writer.phase, backend_pid=writer.backend_pid)
                    row['waits'] = db.execute('SELECT state,wait_event_type,wait_event FROM pg_stat_activity WHERE pid=%s', (row['backend_pid'],)).fetchall()
                    if time.monotonic() - last >= 2:
                        row['database'] = db.execute('SELECT xact_commit,blks_read,blks_hit,blk_read_time,blk_write_time,temp_bytes FROM pg_stat_database WHERE datname=current_database()').fetchone()
                        row['wal'] = [float(x) for x in db.execute('SELECT wal_records,wal_fpi,wal_bytes,wal_buffers_full,wal_write,wal_sync,wal_write_time,wal_sync_time FROM pg_stat_wal').fetchone()]
                        row['tables'] = db.execute("SELECT relname,heap_blks_read,heap_blks_hit,idx_blks_read,idx_blks_hit FROM pg_statio_user_tables WHERE schemaname='public' ORDER BY relname").fetchall()
                        row['io'] = db.execute('SELECT backend_type,object,context,reads,read_time,writes,write_time,writebacks,writeback_time,extends,extend_time,hits,evictions,reuses,fsyncs,fsync_time FROM pg_stat_io').fetchall()
                        if views['neon_backend_perf_counters']:
                            row['neon_backend'] = [(name, str(bucket), value) for name,bucket,value in db.execute("SELECT metric,bucket_le,value FROM neon_backend_perf_counters WHERE pid=%s AND metric NOT LIKE '%%_bucket'", (row['backend_pid'],)).fetchall()]
                        if views['neon_stat_file_cache']:
                            row['file_cache'] = [float(x) if x is not None else None for x in db.execute('SELECT * FROM neon_stat_file_cache').fetchone()]
                        row['storage'] = {role: prometheus(f"http://127.0.0.1:{self.cell.config['ports'][port]}/metrics")
                                          for role,port in [('pageserver','ps_http'),('safekeeper','sk_http')]}
                        last = time.monotonic()
                    self.samples.emit(row); self.sample_count += 1; self.sample_ns += time.monotonic_ns() - start
                    self.stop_event.wait(.02)
        except Exception as error:
            self.errors.append(type(error).__name__)
    def finish(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(10)
            if self.thread.is_alive():
                raise RuntimeError('source observer failed to stop')
        result = dict(transactions=self.ledger.close(), storage=self.samples.close(), errors=self.errors,
                      capabilities=self.capabilities, samples=self.sample_count, sample_wall_ns=self.sample_ns, settings=self.settings,
                      interval_seconds=.02, storage_interval_seconds=2,
                      note='Wait samples are statistical; cumulative IO includes all backends. Writer IO clocks enabled in both arms; observer overhead is not subtracted.')
        (self.root / 'observer.json').write_text(json.dumps(result, indent=2) + '\n')
        return result
