"""Private, bounded, single-writer transaction journal. No acknowledgment authority in RAM."""
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
from .journal import CommitUncertain, Owner

MAX_MESSAGE = 1024 * 1024
MAX_TRANSACTION = 4 * 1024 * 1024
MAX_BATCH = 16 * 1024 * 1024
MAX_GROUP_TRANSACTIONS = 128
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
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or meta.st_nlink != 1:
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
    def __init__(self, directory, identity, limit=512*1024*1024, journal_mode='wal'):
        self.backend: Owner | None=None
        try:self.open(directory,identity,limit,journal_mode)
        except BaseException:
            self.close();raise

    def open(self, directory, identity, limit, journal_mode):
        from .sqlite_journal import SQLiteJournal
        self.identity=identity;self.last_data_lsn=0
        self.backend=SQLiteJournal(directory,{'identity':identity},limit,journal_mode)
        self.root=self.backend.root;self.path=self.backend.path
        if self.get('identity')!=identity:raise CaptureError('spool_identity_mismatch')
        self.verify();self.backend.activate();self.backend.after_write()

    @property
    def limit(self):return self.backend.limit

    @limit.setter
    def limit(self, value):self.backend.limit=value

    def close(self):
        if self.backend is not None:self.backend.close()

    def get(self, key):return self.backend.get(key)
    def set(self, key, value):self.backend.set(key,value)
    def storage_progress(self):return self.backend.progress()
    def physical(self):return self.backend.physical()

    def establish(self, start, schema):
        previous=self.get('start')
        if previous is not None:
            if previous!=start or self.get('schema')!=schema: raise CaptureError('source_generation_changed')
            return
        self.backend.establish(dict(start=start,captured=start,schema=schema,bytes=0))

    @property
    def captured(self):
        return self.get('captured')

    def verify(self):
        self.backend.verify_storage()
        prefix=self.prefix()
        previous=prefix['lsn']
        total=0;barrier=prefix['barrier'];self.last_data_lsn=prefix['last_data_lsn']
        for end,prior,commit,payload,digest in self.backend.records():
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
        backlog=self.backend.backlog_bytes(key)
        first=None
        if self.last_data_lsn>after:
            for row in self.backend.payloads(key):
                if self.has_data(row[0]):first=row;break
        def stamp(row):
            if row is None:return None
            tail=list(frames(row[0]))[-1]
            if len(tail)!=26 or tail[:1]!=b'C':raise CaptureError('invalid_commit')
            return 946684800000+struct.unpack('!q',tail[-8:])[0]//1000
        barrier=self.get('barrier')
        receipt=self.backend.payload(f"{lsn(barrier['end_lsn']):016x}") if barrier else None
        return dict(published_lsn=published,backlog_bytes=backlog,oldest_commit_at_ms=stamp(first),
                    last_data_lsn=pg_lsn(self.last_data_lsn),barrier_commit_at_ms=self.get('barrier_at_ms') if receipt is None else stamp(receipt))

    def append(self, commit, end, payload):
        return bool(self.append_many([(commit,end,payload)])['transactions'])

    def append_many(self, transactions):
        """Commit a bounded ordered group, preserving each transaction/replay anchor.

        The owner lock excludes other writers. Validate the entire group before
        BEGIN; no feedback or in-memory cursor is authorized by a partial insert.
        """
        if not transactions or len(transactions)>MAX_GROUP_TRANSACTIONS:
            raise CaptureError('group_budget')
        if sum(len(tx[2]) for tx in transactions)>MAX_TRANSACTION:
            raise CaptureError('group_budget')
        if self.get('identity')!=self.identity:raise CaptureError('spool_identity_mismatch')
        previous=self.captured
        if previous is None:raise CaptureError('spool_not_established')
        records=[];total=0;last=None;barrier=self.get('barrier');barrier_at=self.get('barrier_at_ms');last_data=self.last_data_lsn
        for commit,end,payload in transactions:
            if len(payload)>MAX_TRANSACTION:raise CaptureError('transaction_budget')
            if last is not None and end<=last:raise CaptureError('noncontiguous_commit_order')
            last=end;digest=hashlib.sha256(payload).hexdigest()
            if end<=previous:
                row=self.backend.replay_anchor(f'{end:016x}')
                if row!=(f'{commit:016x}',digest):raise CaptureError('replay_mismatch')
                continue
            if not previous<=commit<end:raise CaptureError('noncontiguous_commit_order')
            next_barrier=self.barrier_for(payload,end,barrier)
            if next_barrier!=barrier:barrier_at=commit_time(payload)
            barrier=next_barrier
            if self.has_data(payload):last_data=end
            records.append((f'{end:016x}',f'{previous:016x}',f'{commit:016x}',payload,digest))
            previous=end;total+=len(payload)
        result=dict(transactions=len(records),payload_bytes=total,captured_lsn=previous)
        if not records:return result
        total_bytes=(self.get('bytes') or 0)+total
        metadata=dict(captured=previous,bytes=total_bytes)
        if barrier is not None:metadata.update(barrier=barrier,barrier_at_ms=barrier_at)
        try:self.backend.append_group(records,metadata)
        except CommitUncertain as uncertain:
            # Reopened storage is not acknowledgment authority until identity,
            # complete durable chain and every proposed record verify again.
            if self.get('identity')!=self.identity:raise CaptureError('spool_identity_mismatch') from uncertain
            self.verify()
            if self.captured!=previous or self.get('bytes')!=total_bytes:raise uncertain.cause
            for end,prior,commit,payload,digest in records:
                if self.backend.record_anchor(end)!=(prior,commit,digest):
                    raise CaptureError('spool_corrupt') from uncertain
        self.last_data_lsn=last_data
        fault('after_spool_commit')
        self.backend.after_write()
        return result

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
        rows=self.backend.prune_candidates(f'{cut:016x}')
        reclaimed=sum(len(row[3]) for row in rows)
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
        # Older spools did not save the barrier timestamp separately. The
        # exclusive owner preserves this state while the backend commits pruning.
        current=self.get('barrier');barrier_at=None
        if current and self.get('barrier_at_ms') is None:
            row=self.backend.payload(f"{lsn(current['end_lsn']):016x}")
            if row:barrier_at=commit_time(row[0])
        return self.backend.prune_group(rows[-1][0],value,reclaimed,barrier_at)

    def transactions(self, after):
        # Streaming iterator for SY03; consumer is responsible for holding the generation lease.
        for end,payload,digest in self.backend.transactions(f'{after:016x}'):
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
