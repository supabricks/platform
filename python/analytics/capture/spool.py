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
        self.last_data_lsn = 0
        self.limit = limit
        self.path = self.root/'spool.sqlite3'
        existed = self.path.exists()
        os.close(private_file(self.path))
        if self.path.stat().st_size > limit:
            raise CaptureError('spool_budget')
        self.db = sqlite3.connect(self.path, isolation_level=None)
        if not existed: self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
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
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    @property
    def captured(self):
        return self.get('captured')

    def verify(self):
        if self.db.execute('PRAGMA quick_check').fetchone() != ('ok',): raise CaptureError('spool_corrupt')
        prefix=self.prefix()
        previous=prefix['lsn']
        if self.db.execute('SELECT 1 FROM transactions WHERE length(payload)>? LIMIT 1',(MAX_TRANSACTION,)).fetchone():raise CaptureError('spool_corrupt')
        total=0;barrier=prefix['barrier'];self.last_data_lsn=prefix['last_data_lsn']
        for end,prior,commit,payload,digest in self.db.execute('SELECT end_lsn,previous_lsn,commit_lsn,payload,sha256 FROM transactions ORDER BY seq'):
            end,prior,commit=int(end,16),int(prior,16),int(commit,16)
            if previous is None or prior!=previous or not previous<end or not commit<end or len(payload)>MAX_TRANSACTION or hashlib.sha256(payload).hexdigest()!=digest:
                raise CaptureError('spool_corrupt')
            barrier=self.barrier_for(payload,end,barrier)
            if self.has_data(payload):self.last_data_lsn=end
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

    def has_data(self,payload):
        return self.identity.get('decoder_version')==2 and any(f[:1] in (b'I',b'U',b'D') for f in frames(payload))

    def progress(self,published):
        if self.identity.get('decoder_version')!=2:return None
        after=lsn(published) if published else 0
        key=f'{after:016x}'
        backlog=self.db.execute('SELECT coalesce(sum(length(payload)),0) FROM transactions WHERE end_lsn>?',(key,)).fetchone()[0]
        first=None
        if self.last_data_lsn>after:
            for row in self.db.execute('SELECT payload FROM transactions WHERE end_lsn>? ORDER BY end_lsn',(key,)):
                if self.has_data(row[0]):first=row;break
        def stamp(row):
            if row is None:return None
            tail=list(frames(row[0]))[-1]
            if len(tail)!=26 or tail[:1]!=b'C':raise CaptureError('invalid_commit')
            return 946684800000+struct.unpack('!q',tail[-8:])[0]//1000
        barrier=self.get('barrier')
        receipt=self.db.execute('SELECT payload FROM transactions WHERE end_lsn=?',(f"{lsn(barrier['end_lsn']):016x}",)).fetchone() if barrier else None
        return dict(published_lsn=published,backlog_bytes=backlog,oldest_commit_at_ms=stamp(first),
                    last_data_lsn=pg_lsn(self.last_data_lsn),barrier_commit_at_ms=self.get('barrier_at_ms') if receipt is None else stamp(receipt))

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
        used=self.path.stat().st_size-self.db.execute('PRAGMA freelist_count').fetchone()[0]*self.db.execute('PRAGMA page_size').fetchone()[0]
        space=os.statvfs(self.root)
        if used+2*len(payload)+65536>self.limit or space.f_bavail*space.f_frsize<RESERVE+2*len(payload)+65536:
            raise CaptureError('spool_budget')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO transactions(end_lsn,previous_lsn,commit_lsn,payload,sha256) VALUES (?,?,?,?,?)',
                (f'{end:016x}',f'{previous:016x}',f'{commit:016x}',payload,digest))
            self.set('captured',end);self.set('bytes',(self.get('bytes') or 0)+len(payload))
            barrier=self.barrier_for(payload,end,self.get('barrier'))
            if barrier is not None:
                if barrier!=self.get('barrier'):self.set('barrier_at_ms',commit_time(payload))
                self.set('barrier',barrier)
            fault('before_spool_commit')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise
        if self.has_data(payload):self.last_data_lsn=end
        fault('after_spool_commit')
        return True

    def prefix(self):
        return checked_prefix(self.get('pruned_prefix'),self.identity,self.get('start'),self.captured)

    def prune(self, published):
        """Only the daemon's committed epoch cursor authorizes prefix deletion.

        Keep the newest transaction at/below the cut for reconnect duplicate
        verification. A bounded SQLite transaction moves the anchor and removes
        rows together; readers retain their SQLite snapshot throughout.
        """
        if published is None or self.captured is None:return 0
        cut=lsn(published)
        # A frozen bootstrap may include WAL beyond the last captured commit
        # (including an idle source). It is publication authority, not an ack.
        if cut>self.captured and published!=(self.get('bootstrap') or {}).get('lsn'):
            raise CaptureError('published_cursor_ahead')
        if cut<self.prefix()['lsn']:raise CaptureError('published_cursor_regressed')
        rows=[];reclaimed=0
        # Exclude the reconnect anchor in SQL, and cap memory as well as rows.
        cursor=self.db.execute('SELECT seq,end_lsn,previous_lsn,payload,sha256 FROM transactions WHERE end_lsn<(SELECT max(end_lsn) FROM transactions WHERE end_lsn<=?) ORDER BY seq LIMIT 256',(f'{cut:016x}',))
        for row in cursor:
            if reclaimed+len(row[3])>MAX_BATCH:break
            rows.append(row);reclaimed+=len(row[3])
        cursor.close()
        if len(rows)<256 and reclaimed<1024*1024:return 0
        prefix=self.prefix();barrier=prefix['barrier'];last_data=prefix['last_data_lsn'];previous=prefix['lsn']
        for _,end,prior,payload,checksum in rows:
            end=int(end,16)
            if int(prior,16)!=previous or hashlib.sha256(payload).hexdigest()!=checksum:raise CaptureError('spool_corrupt')
            barrier=self.barrier_for(payload,end,barrier)
            if self.has_data(payload):last_data=end
            previous=end
        value=dict(lsn=previous,barrier=barrier,last_data_lsn=last_data,identity_sha256=hashlib.sha256(canonical(self.identity)).hexdigest())
        value['sha256']=hashlib.sha256(canonical(value)).hexdigest()
        timeout=self.db.execute('PRAGMA busy_timeout').fetchone()[0]
        self.db.execute(f'PRAGMA busy_timeout={min(timeout,50)}')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            # Older spools did not save the barrier timestamp separately.
            current=self.get('barrier')
            if current and self.get('barrier_at_ms') is None:
                row=self.db.execute('SELECT payload FROM transactions WHERE end_lsn=?',(f"{lsn(current['end_lsn']):016x}",)).fetchone()
                if row:self.set('barrier_at_ms',commit_time(row[0]))
            self.db.execute('DELETE FROM transactions WHERE seq<=?',(rows[-1][0],))
            self.set('pruned_prefix',value);self.set('bytes',self.get('bytes')-reclaimed)
            fault('before_spool_prune_commit')
            self.db.execute('COMMIT')
        except sqlite3.OperationalError as error:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            if getattr(error,'sqlite_errorcode',None) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):return 0
            raise
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise
        finally:self.db.execute(f'PRAGMA busy_timeout={timeout}')
        fault('after_spool_prune_commit')
        # Existing v1 spools reuse their free pages; new spools can also return
        # free tail pages without a second full-size VACUUM copy.
        self.db.execute(f'PRAGMA busy_timeout={min(timeout,50)}')
        try:self.db.execute('PRAGMA incremental_vacuum(64)')
        except sqlite3.OperationalError as error:
            if getattr(error,'sqlite_errorcode',None) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):raise
        finally:self.db.execute(f'PRAGMA busy_timeout={timeout}')
        fault('after_spool_prune_vacuum')
        return reclaimed

    def transactions(self, after):
        # Streaming iterator for SY03; consumer is responsible for holding the generation lease.
        for end,payload,digest in self.db.execute('SELECT end_lsn,payload,sha256 FROM transactions WHERE end_lsn>? ORDER BY seq',(f'{after:016x}',)):
            if hashlib.sha256(payload).hexdigest()!=digest: raise CaptureError('spool_corrupt')
            yield int(end,16),payload


def commit_time(payload):
    tail=list(frames(payload))[-1]
    if len(tail)!=26 or tail[:1]!=b'C':raise CaptureError('invalid_commit')
    return 946684800000+struct.unpack('!q',tail[-8:])[0]//1000


def checked_prefix(prefix,identity,start,captured):
    if prefix is None:return dict(lsn=start,barrier=None,last_data_lsn=0)
    try:
        checksum=prefix.get('sha256')
        value={k:v for k,v in prefix.items() if k!='sha256'}
        if (set(value)!={'lsn','barrier','last_data_lsn','identity_sha256'}
            or checksum!=hashlib.sha256(canonical(value)).hexdigest()
            or value['identity_sha256']!=hashlib.sha256(canonical(identity)).hexdigest()
            or type(value['lsn']) is not int or type(value['last_data_lsn']) is not int
            or not start<=value['lsn']<=captured or not 0<=value['last_data_lsn']<=value['lsn']):raise ValueError()
        barrier=value['barrier']
        if barrier is not None and (set(barrier)!={'run_id','end_lsn'} or not isinstance(barrier['run_id'],str)
            or not start<lsn(barrier['end_lsn'])<=value['lsn']):raise ValueError()
        return value
    except (AttributeError,TypeError,KeyError,ValueError):raise CaptureError('spool_corrupt') from None


def frames(payload):
    offset=0
    while offset<len(payload):
        if offset+4>len(payload): raise CaptureError('spool_corrupt')
        size=struct.unpack_from('!I',payload,offset)[0];offset+=4
        if size>MAX_MESSAGE or offset+size>len(payload): raise CaptureError('spool_corrupt')
        yield payload[offset:offset+size];offset+=size
