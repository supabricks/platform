"""Direct SQLite implementation of the capture journal storage contract.

On-disk schema, statement ordering inside transactions, WAL/FULL settings,
exclusive writer lock, shared reader leases and checkpoint thresholds are unchanged.
"""
from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from .spool import CaptureError, private_file, canonical, fault, MAX_TRANSACTION, MAX_GROUP_TRANSACTIONS, MAX_BATCH
from .journal import CommitUncertain, ReadBusy

BUSY_CODES=frozenset((sqlite3.SQLITE_BUSY,261,517,773))


class SQLiteJournal:
    def __init__(self, directory, initial_metadata, limit, journal_mode):
        self.lock=self.db=None
        try:self.open(directory,initial_metadata,limit,journal_mode)
        except BaseException:
            self.close();raise

    def open(self, directory, initial_metadata, limit, journal_mode):
        self.root = Path(directory)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Persist the directory entry before this journal can authorize feedback.
        parent_fd = os.open(self.root.parent, os.O_RDONLY)
        try: os.fsync(parent_fd)
        finally: os.close(parent_fd)
        meta = self.root.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise CaptureError('unsafe_spool_directory')
        self.lock = private_file(self.root/'owner.lock')
        try: fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CaptureError('spool_already_owned') from None
        self.limit = limit
        self.path = self.root/'spool.sqlite3'
        existed = self.path.exists()
        from .wal import Journal, private_sizes, fixed_sqlite
        sizes = private_sizes(self.path)
        if sum(sizes.values()) > limit:raise CaptureError('spool_budget')
        if not fixed_sqlite():
            raise CaptureError('sqlite_wal_unqualified')
        self.journal = Journal(self, journal_mode)
        os.close(private_file(self.path))
        if self.path.stat().st_size > limit:raise CaptureError('spool_budget')
        self.db = sqlite3.connect(self.path, isolation_level=None)
        if not existed: self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
        self.journal.configure()
        if not existed:
            self.journal.before_write()
            self.db.executescript('BEGIN IMMEDIATE; CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); '
                'CREATE TABLE transactions(seq INTEGER PRIMARY KEY,end_lsn TEXT NOT NULL UNIQUE,previous_lsn TEXT NOT NULL,commit_lsn TEXT NOT NULL,payload BLOB NOT NULL,sha256 TEXT NOT NULL); COMMIT;')
            for key,value in initial_metadata.items():self.set(key,value)
            directory_fd=os.open(self.root,os.O_RDONLY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)

    def close(self):
        if self.db is not None:
            self.db.close(); self.db = None
        if self.lock is not None:
            os.close(self.lock); self.lock = None

    def get(self, key):
        row=self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        implicit = not self.db.in_transaction
        if implicit:self.journal.before_write()
        self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,canonical(value).decode()))
        if implicit:self.journal.after_write()

    def establish(self, values):
        self.journal.before_write()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for key,value in values.items():self.set(key,value)
            self.db.execute('COMMIT')
            self.journal.after_write()
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def snapshot(self, deadline, busy_seconds, cancelled=lambda:False):
        # Invoked only inside the owner process. A reader connection belongs to
        # the one bounded service thread; the write connection stays on capture.
        return snapshot(self.path,deadline,busy_seconds,cancelled)

    def activate(self):self.journal.activate()
    def after_write(self):self.journal.after_write()
    def progress(self):return self.journal.progress()
    def physical(self):return self.journal.physical()

    def verify_storage(self):
        if self.db.execute('PRAGMA quick_check').fetchone()!=('ok',):raise CaptureError('spool_corrupt')
        if self.db.execute('SELECT 1 FROM transactions WHERE length(payload)>? LIMIT 1',(MAX_TRANSACTION,)).fetchone():raise CaptureError('spool_corrupt')

    def records(self):
        return self.db.execute('SELECT end_lsn,previous_lsn,commit_lsn,payload,sha256 FROM transactions ORDER BY seq')

    def replay_anchor(self, end):
        return self.db.execute('SELECT commit_lsn,sha256 FROM transactions WHERE end_lsn=?',(end,)).fetchone()

    def record_anchor(self, end):
        return self.db.execute('SELECT previous_lsn,commit_lsn,sha256 FROM transactions WHERE end_lsn=?',(end,)).fetchone()

    def backlog_bytes(self, after):
        return self.db.execute('SELECT coalesce(sum(length(payload)),0) FROM transactions WHERE end_lsn>?',(after,)).fetchone()[0]

    def payloads(self, after):
        return self.db.execute('SELECT payload FROM transactions WHERE end_lsn>? ORDER BY end_lsn',(after,))

    def payload(self, end):
        return self.db.execute('SELECT payload FROM transactions WHERE end_lsn=?',(end,)).fetchone()

    def transactions(self, after):
        return self.db.execute('SELECT end_lsn,payload,sha256 FROM transactions WHERE end_lsn>? ORDER BY seq',(after,))

    def append_group(self, records, metadata):
        total=sum(len(row[3]) for row in records)
        if not records or len(records)>MAX_GROUP_TRANSACTIONS or total>MAX_TRANSACTION:
            raise CaptureError('group_budget')
        self.journal.before_append(total+len(records)*16384+65536)
        self.journal.before_write()
        fault('before_spool_group')
        self.db.execute('BEGIN IMMEDIATE')
        committing=False
        try:
            self.db.executemany('INSERT INTO transactions(end_lsn,previous_lsn,commit_lsn,payload,sha256) VALUES (?,?,?,?,?)',records)
            for key,value in metadata.items():self.set(key,value)
            fault('before_spool_commit');committing=True
            self.db.execute('COMMIT')
        except BaseException as error:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK');raise
            if not committing or not isinstance(error,sqlite3.Error):raise
            self.db.close();self.db=sqlite3.connect(self.path,isolation_level=None)
            self.journal.configure()
            if self.db.execute('PRAGMA journal_mode').fetchone()[0]!=self.journal.mode:
                raise CaptureError('spool_journal_mode') from error
            raise CommitUncertain(error) from error

    def prune_candidates(self, through):
        rows=[];used=0
        cursor=self.db.execute('SELECT seq,end_lsn,previous_lsn,payload,sha256 FROM transactions WHERE end_lsn<(SELECT max(end_lsn) FROM transactions WHERE end_lsn<=?) ORDER BY seq LIMIT 256',(through,))
        try:
            for row in cursor:
                if used+len(row[3])>MAX_BATCH:break
                rows.append(row);used+=len(row[3])
        finally:cursor.close()
        return rows

    def prune_group(self, token, prefix, reclaimed, barrier_at):
        timeout=self.db.execute('PRAGMA busy_timeout').fetchone()[0]
        self.db.execute(f'PRAGMA busy_timeout={min(timeout,50)}')
        try:
            self.journal.before_write();self.db.execute('BEGIN IMMEDIATE')
            if barrier_at is not None:self.set('barrier_at_ms',barrier_at)
            self.db.execute('DELETE FROM transactions WHERE seq<=?',(token,))
            self.set('pruned_prefix',prefix);self.set('bytes',self.get('bytes')-reclaimed)
            fault('before_spool_prune_commit');self.db.execute('COMMIT')
        except sqlite3.OperationalError as error:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            if getattr(error,'sqlite_errorcode',None) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):return 0
            raise
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise
        finally:self.db.execute(f'PRAGMA busy_timeout={timeout}')
        fault('after_spool_prune_commit');self.journal.after_write()
        self.db.execute(f'PRAGMA busy_timeout={min(timeout,50)}')
        try:
            self.journal.before_write();self.db.execute('PRAGMA incremental_vacuum(64)').fetchall();self.journal.after_write()
        except sqlite3.OperationalError as error:
            if getattr(error,'sqlite_errorcode',None) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):raise
        finally:self.db.execute(f'PRAGMA busy_timeout={timeout}')
        fault('after_spool_prune_vacuum')
        return reclaimed


class SQLiteSnapshot:
    def __init__(self, path, deadline, busy_seconds, cancelled=lambda:False):
        self.cleanup=ExitStack();self.deadline=deadline;self.busy_seconds=busy_seconds;self.cancelled=cancelled
        try:
            path=Path(path)
            if path.is_symlink() or path.stat().st_size>512*1024*1024:raise CaptureError('spool_budget')
            from .wal import reader_lease
            self.cleanup.callback(os.close,reader_lease(path))
            self.db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
            self.cleanup.callback(self.db.close)
            self.db.set_progress_handler(lambda:int(time.monotonic()>=deadline or cancelled()),1000)
            self.execute('BEGIN')
        except BaseException:
            self.close();raise

    def execute(self, sql, parameters=()):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise CaptureError('journal_read_deadline')
        if self.cancelled():raise CaptureError('journal_read_cancelled')
        timeout=self.db.execute('PRAGMA busy_timeout='+str(int(min(self.busy_seconds,remaining)*1000)))
        self.cleanup.callback(timeout.close)
        cursor=self.db.execute(sql,parameters);self.cleanup.callback(cursor.close)
        return cursor

    def metadata(self):
        meta={}
        for k,v,size in self.execute('SELECT key,CASE WHEN length(value)<=2097152 THEN value ELSE NULL END,length(value) FROM metadata LIMIT 33'):
            if len(meta)>=32 or size>2097152:raise CaptureError('spool_metadata_budget')
            meta[k]=json.loads(v)
        return meta

    def records(self, after, through):
        return self.execute('SELECT end_lsn,previous_lsn,commit_lsn,length(payload),CASE WHEN length(payload)<=4194304 THEN payload ELSE NULL END,sha256 FROM transactions WHERE end_lsn>? AND end_lsn<=? ORDER BY seq',(after,through))

    def close(self):self.cleanup.close()


@contextmanager
def snapshot(path, deadline, busy_seconds, cancelled=lambda:False):
    try:
        view=SQLiteSnapshot(path,deadline,busy_seconds,cancelled)
        try:yield view
        finally:view.close()
    except sqlite3.OperationalError as error:
        code=getattr(error,'sqlite_errorcode',None)
        if code in BUSY_CODES:raise ReadBusy() from error
        if code==sqlite3.SQLITE_INTERRUPT and cancelled():raise CaptureError('journal_read_cancelled') from None
        if code==sqlite3.SQLITE_INTERRUPT and time.monotonic()>=deadline:
            raise CaptureError('journal_read_deadline') from None
        raise
