"""Private, bounded, single-writer transaction journal. No acknowledgment authority in RAM."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct

MAX_MESSAGE = 1024 * 1024
MAX_TRANSACTION = 4 * 1024 * 1024
MAX_BATCH = 16 * 1024 * 1024
RESERVE = 64 * 1024 * 1024

class CaptureError(Exception):
    """Only the fixed code, never source values or credentials, crosses worker status."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fault(point):
    # The daemon clears the worker environment. Only direct qualification workers set this.
    if os.environ.get('SUPABRICKS_CAPTURE_FAILPOINT') == point:
        os._exit(86)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def lsn(value):
    high, low = value.split('/')
    return (int(high, 16) << 32) + int(low, 16)


def pg_lsn(value):
    return f'{value >> 32:X}/{value & 0xffffffff:X}'


def private_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    meta = os.fstat(fd)
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
        os.close(fd)
        raise CaptureError('unsafe_spool_path')
    return fd


def atomic(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    fd = private_file(temp)
    try:
        os.ftruncate(fd, 0)  # A crash may have left a longer previous temporary file.
        with os.fdopen(fd, 'wb') as out:
            out.write(canonical(value)); out.flush(); os.fsync(out.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temp.exists(): temp.unlink()


class Spool:
    def __init__(self, directory, identity, limit=512*1024*1024):
        self.lock = self.db = None
        try: self.open(directory, identity, limit)
        except BaseException:
            self.close()
            raise

    def open(self, directory, identity, limit):
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
        self.identity = identity
        self.limit = limit
        self.path = self.root/'spool.sqlite3'
        existed = self.path.exists()
        os.close(private_file(self.path))
        if self.path.stat().st_size > limit:
            raise CaptureError('spool_budget')
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute(f'PRAGMA max_page_count={limit//4096}')
        if not existed:
            self.db.executescript('BEGIN IMMEDIATE; CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); '
                'CREATE TABLE transactions(seq INTEGER PRIMARY KEY,end_lsn TEXT NOT NULL UNIQUE,previous_lsn TEXT NOT NULL,commit_lsn TEXT NOT NULL,payload BLOB NOT NULL,sha256 TEXT NOT NULL); COMMIT;')
            self.set('identity', identity)
            directory_fd=os.open(self.root,os.O_RDONLY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
        if self.get('identity') != identity:
            raise CaptureError('spool_identity_mismatch')
        self.verify()

    def close(self):
        if self.db is not None:
            self.db.close(); self.db = None
        if self.lock is not None:
            os.close(self.lock); self.lock = None

    def get(self, key):
        row=self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,canonical(value).decode()))

    def establish(self, start, schema):
        previous=self.get('start')
        if previous is not None:
            if previous!=start or self.get('schema')!=schema: raise CaptureError('source_generation_changed')
            return
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.set('start',start);self.set('captured',start);self.set('schema',schema);self.set('bytes',0)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise

    @property
    def captured(self):
        return self.get('captured')

    def verify(self):
        if self.db.execute('PRAGMA quick_check').fetchone() != ('ok',): raise CaptureError('spool_corrupt')
        previous=self.get('start')
        if self.db.execute('SELECT 1 FROM transactions WHERE length(payload)>? LIMIT 1',(MAX_TRANSACTION,)).fetchone():raise CaptureError('spool_corrupt')
        total=0;barrier=None
        for end,prior,commit,payload,digest in self.db.execute('SELECT end_lsn,previous_lsn,commit_lsn,payload,sha256 FROM transactions ORDER BY seq'):
            end,prior,commit=int(end,16),int(prior,16),int(commit,16)
            if previous is None or prior!=previous or not previous<end or not commit<end or len(payload)>MAX_TRANSACTION or hashlib.sha256(payload).hexdigest()!=digest:
                raise CaptureError('spool_corrupt')
            barrier=self.barrier_for(payload,end,barrier)
            previous=end;total+=len(payload)
            if total>self.limit: raise CaptureError('spool_budget')
        if previous!=self.captured or total!=(self.get('bytes') or 0) or barrier!=self.get('barrier'): raise CaptureError('spool_corrupt')

    def barrier_for(self,payload,end,previous):
        if self.identity.get('decoder_version')!=2:return previous
        from .protocol import Reader,barrier_message
        for frame in frames(payload):
            if frame[:1]!=b'M':continue
            r=Reader(frame);r.take(1);flags=r.number('B');r.number('Q');prefix=r.string();content=r.take(r.number('I'));r.finish()
            run=barrier_message(flags,prefix,content,'supabricks.barrier.'+self.identity['generation'])
            if previous is None or previous['run_id']!=run:previous=dict(run_id=run,end_lsn=pg_lsn(end))
        return previous

    def append(self, commit, end, payload):
        if len(payload)>MAX_TRANSACTION: raise CaptureError('transaction_budget')
        digest=hashlib.sha256(payload).hexdigest()
        previous=self.captured
        if previous is None: raise CaptureError('spool_not_established')
        if end<=previous:
            row=self.db.execute('SELECT sha256 FROM transactions WHERE end_lsn=?',(f'{end:016x}',)).fetchone()
            if row is None or row[0]!=digest: raise CaptureError('replay_mismatch')
            return False
        if not previous<=commit<end: raise CaptureError('noncontiguous_commit_order')
        # Reserve database pages, rollback journal and next complete transaction BEFORE writing.
        used=self.path.stat().st_size
        space=os.statvfs(self.root)
        if used+2*len(payload)+65536>self.limit or space.f_bavail*space.f_frsize<RESERVE+2*len(payload)+65536:
            raise CaptureError('spool_budget')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO transactions(end_lsn,previous_lsn,commit_lsn,payload,sha256) VALUES (?,?,?,?,?)',
                (f'{end:016x}',f'{previous:016x}',f'{commit:016x}',payload,digest))
            self.set('captured',end);self.set('bytes',(self.get('bytes') or 0)+len(payload))
            barrier=self.barrier_for(payload,end,self.get('barrier'))
            if barrier is not None:self.set('barrier',barrier)
            fault('before_spool_commit')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise
        fault('after_spool_commit')
        return True

    def transactions(self, after):
        # Streaming iterator for SY03; consumer is responsible for holding the generation lease.
        for end,payload,digest in self.db.execute('SELECT end_lsn,payload,sha256 FROM transactions WHERE end_lsn>? ORDER BY seq',(f'{after:016x}',)):
            if hashlib.sha256(payload).hexdigest()!=digest: raise CaptureError('spool_corrupt')
            yield int(end,16),payload


def frames(payload):
    offset=0
    while offset<len(payload):
        if offset+4>len(payload): raise CaptureError('spool_corrupt')
        size=struct.unpack_from('!I',payload,offset)[0];offset+=4
        if size>MAX_MESSAGE or offset+size>len(payload): raise CaptureError('spool_corrupt')
        yield payload[offset:offset+size];offset+=size
