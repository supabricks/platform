"""SP10c experimental journal; installed only in the frozen RocksDB candidate.

No production selector or migration. Existing SQLite storage is rejected. A single
raw iterator pins metadata and records together (rocksdict snapshot iteration is
not consistent: platform#149). No pickle, secondary DB, or WAL-disabled writes.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import struct
import time
import threading

from rocksdict import (Rdict, Options, WriteBatch, WriteOptions, ReadOptions,
                      BlockBasedOptions, Cache, WriteBufferManager, DBRecoveryMode,
                      DBCompressionType)
from capture.spool import (CaptureError, private_file, canonical, fault, atomic,
                           MAX_TRANSACTION, MAX_GROUP_TRANSACTIONS, MAX_BATCH, RESERVE)
from capture.wal import SpoolBackpressure
from capture.journal import ReadBusy

FORMAT={'backend':'rocksdb','version':1,'encoding':'lsn16-record-v1'}
HEADER=struct.Struct('!16s16s32s')
MIB=1024*1024


def decode(key,value):
    if len(key)!=18 or not key.startswith(b't:') or len(value)<HEADER.size:
        raise CaptureError('spool_corrupt')
    prior,commit,digest=HEADER.unpack_from(value)
    try:
        end=key[2:].decode('ascii');prior=prior.decode('ascii');commit=commit.decode('ascii')
        if any(f'{int(x,16):016x}'!=x for x in (end,prior,commit)):raise ValueError()
    except (ValueError,UnicodeError):raise CaptureError('spool_corrupt') from None
    return end,prior,commit,value[HEADER.size:],digest.hex()


class View:
    def __init__(self,db,deadline,cancelled):
        self.deadline,self.cancelled=deadline,cancelled
        opt=ReadOptions();opt.set_verify_checksums(True);opt.fill_cache(False)
        self.iterator=db.iter(opt)

    def check(self):
        if self.cancelled():raise CaptureError('journal_read_cancelled')
        if time.monotonic()>=self.deadline:raise CaptureError('journal_read_deadline')
        if self.iterator is None:raise CaptureError('journal_read_cancelled')

    def scan(self,after,through=None):
        self.check();self.iterator.seek(after)
        try:
            while self.iterator.valid():
                self.check();key=self.iterator.key()
                if through is not None and key>through:break
                yield key,self.iterator.value()
                self.iterator.next()
            self.iterator.status()  # Exhaustion must never hide a native I/O/corruption error.
        finally:pass

    def metadata(self):
        result={}
        for key,value in self.scan(b'm:',b'm;'):
            if not key.startswith(b'm:'):break
            if len(result)>=32 or len(value)>2*MIB:raise CaptureError('spool_metadata_budget')
            result[key[2:].decode('ascii')]=json.loads(value)
        return result

    def records(self,after,through):
        for key,value in self.scan(b't:'+after.encode(),b't:'+through.encode()):
            if key==b't:'+after.encode():continue
            end,prior,commit,payload,digest=decode(key,value)
            yield end,prior,commit,len(payload),payload if len(payload)<=MAX_TRANSACTION else None,digest

    def close(self):self.iterator=None


class RocksJournal:
    def __init__(self,directory,initial_metadata,limit,journal_mode):
        self.db=None;self.lock=None;self.reader=threading.Lock();self.pinned=False
        self.stats=dict(compactions=0,compaction_ms=0.0,sync_writes=0,sync_ms=0.0,
                        backpressure_events=0,backpressured=False)
        try:self.open(directory,initial_metadata,limit,journal_mode)
        except BaseException:self.close();raise

    def open(self,directory,initial_metadata,limit,journal_mode):
        if journal_mode!='wal':raise CaptureError('invalid_journal_mode')
        if not 16*MIB<=limit<=512*MIB:raise CaptureError('invalid_spool_budget')
        self.limit=limit;self.root=Path(directory);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise CaptureError('unsafe_spool_directory')
        self.lock=private_file(self.root/'owner.lock')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise CaptureError('spool_already_owned') from None
        if any(self.root.glob('spool.sqlite3*')):raise CaptureError('spool_backend_mismatch')
        self.path=self.root/'rocksdb';marker=self.root/'format.json'
        if marker.exists():
            fd=private_file(marker)
            try:
                if os.fstat(fd).st_size>1024:raise CaptureError('spool_backend_mismatch')
                with os.fdopen(os.dup(fd),'rb') as f:value=json.load(f)
                if value!=FORMAT:raise CaptureError('spool_backend_mismatch')
            finally:os.close(fd)
            if not self.path.is_dir():raise CaptureError('spool_corrupt')
        elif self.path.exists():raise CaptureError('spool_backend_mismatch')
        self.path.mkdir(mode=0o700,exist_ok=True);self.sizes()
        self.buffer=max(256*1024,min(4*MIB,limit//32))
        opt=Options(raw_mode=True);opt.create_if_missing(not marker.exists())
        opt.set_wal_recovery_mode(DBRecoveryMode.absolute_consistency())
        opt.set_write_buffer_size(self.buffer);opt.set_max_write_buffer_number(2)
        self.memory=WriteBufferManager(self.buffer*2,True);opt.set_write_buffer_manager(self.memory)
        self.cache=Cache(self.buffer*2);block=BlockBasedOptions();block.set_block_cache(self.cache)
        block.set_cache_index_and_filter_blocks(True);opt.set_block_based_table_factory(block)
        opt.set_max_total_wal_size(self.buffer*2);opt.set_max_background_jobs(2)
        opt.set_max_subcompactions(1);opt.set_target_file_size_base(self.buffer)
        opt.set_max_bytes_for_level_base(self.buffer*4);opt.set_max_open_files(64)
        opt.set_keep_log_file_num(2);opt.set_max_log_file_size(256*1024)
        opt.set_max_manifest_file_size(MIB);opt.set_manifest_preallocation_size(4096)
        opt.set_compression_type(DBCompressionType.none());opt.set_unordered_write(False)
        opt.set_stats_dump_period_sec(0)
        self.options=opt;self.db=Rdict(str(self.path),opt)
        self.write_options=WriteOptions();self.write_options.sync=True;self.write_options.disable_wal=False
        self.db.set_write_options(self.write_options)
        if not marker.exists():
            self.establish(initial_metadata);atomic(marker,FORMAT)
        fd=os.open(self.root.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        self.after_write()

    def close(self):
        if self.db is not None:self.db.close();self.db=None
        if self.lock is not None:os.close(self.lock);self.lock=None

    def sizes(self):
        result={}
        if self.path.is_symlink():raise CaptureError('unsafe_spool_path')
        for entry in self.path.iterdir():
            try:meta=entry.lstat()
            except FileNotFoundError:continue  # Background compaction retired this file.
            # Private parent is mandatory; RocksDB's native files are not chmodded
            # from the reader thread. Workers run under the established 077 umask.
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid() or meta.st_nlink!=1:
                raise CaptureError('unsafe_spool_path')
            result[entry.name]=meta.st_size
        return result

    def physical(self):return sum(self.sizes().values())
    def activate(self):pass
    def after_write(self):
        if self.physical()>self.limit:raise CaptureError('spool_budget')

    def reserve(self,added=0):
        # Conservative experiment envelope. Native compaction can temporarily
        # duplicate SSTs; resource qualification must measure that peak, not only
        # the post-write sample. This is not a filesystem hard quota.
        logical=self.get('bytes') or 0;physical=self.physical()
        space=os.statvfs(self.root)
        if logical+added>self.limit//8 or physical+2*self.buffer+added>self.limit*3//4 or space.f_bavail*space.f_frsize<RESERVE+self.limit:
            self.stats['backpressure_events']+=1;self.stats['backpressured']=True
            raise SpoolBackpressure()
        self.stats['backpressured']=False

    def write(self,batch):
        start=time.monotonic()
        # Any native exception is fatal, without feedback or retry. A subsequent
        # worker reopens and runs the complete Spool chain/identity verification.
        self.db.write(batch,self.write_options)
        self.stats['sync_writes']+=1;self.stats['sync_ms']+=(time.monotonic()-start)*1000

    def get(self,key):
        value=self.db.get(b'm:'+key.encode())
        if value is None:return None
        if len(value)>2*MIB:raise CaptureError('spool_metadata_budget')
        return json.loads(value)

    def metadata_batch(self,batch,values):
        for key,value in values.items():
            raw=canonical(value)
            if len(raw)>2*MIB:raise CaptureError('spool_metadata_budget')
            batch.put(b'm:'+key.encode(),raw)

    def set(self,key,value):self.establish({key:value})
    def establish(self,values):
        batch=WriteBatch(raw_mode=True);self.metadata_batch(batch,values);self.reserve();self.write(batch)

    @contextmanager
    def snapshot(self,deadline,busy_seconds,cancelled=lambda:False):
        if not self.reader.acquire(blocking=False):raise ReadBusy()
        view=None
        try:
            self.pinned=True;view=View(self.db,deadline,cancelled);yield view
        finally:
            if view:view.close()
            self.pinned=False;self.reader.release()

    def records(self,after=None):
        it=self.db.iter();it.seek(b't:'+(after.encode() if after else b''))
        try:
            while it.valid() and it.key().startswith(b't:'):
                yield decode(it.key(),it.value());it.next()
            it.status()
        finally:del it

    def verify_storage(self):
        with self.snapshot(time.monotonic()+30,0) as view:view.metadata()
        for row in self.records():
            if len(row[3])>MAX_TRANSACTION:raise CaptureError('spool_corrupt')

    def record_anchor(self,end):
        value=self.db.get(b't:'+end.encode())
        if value is None:return None
        row=decode(b't:'+end.encode(),value);return row[1],row[2],row[4]
    def replay_anchor(self,end):
        row=self.record_anchor(end);return row[1:] if row else None
    def payload(self,end):
        value=self.db.get(b't:'+end.encode())
        return (decode(b't:'+end.encode(),value)[3],) if value is not None else None
    def payloads(self,after):
        for end,prior,commit,payload,digest in self.records(after):
            if end>after:yield (payload,)
    def backlog_bytes(self,after):return sum(len(row[0]) for row in self.payloads(after))
    def transactions(self,after):
        for end,prior,commit,payload,digest in self.records(after):
            if end>after:yield end,payload,digest

    def append_group(self,records,metadata):
        total=sum(len(row[3]) for row in records)
        if not records or len(records)>MAX_GROUP_TRANSACTIONS or total>MAX_TRANSACTION:raise CaptureError('group_budget')
        self.reserve(total+len(records)*HEADER.size);fault('before_spool_group')
        batch=WriteBatch(raw_mode=True)
        for end,prior,commit,payload,digest in records:
            if self.db.get(b't:'+end.encode()) is not None:raise CaptureError('spool_corrupt')
            batch.put(b't:'+end.encode(),HEADER.pack(prior.encode(),commit.encode(),bytes.fromhex(digest))+payload)
        self.metadata_batch(batch,metadata);fault('before_spool_commit');self.write(batch)

    def prune_candidates(self,through):
        rows=[];used=0;pending=None
        for row in self.records():
            end,prior,commit,payload,digest=row
            if end>through:break
            if pending is not None:
                if len(rows)>=256 or used+len(pending[3])>MAX_BATCH:break
                rows.append(pending);used+=len(pending[3])
            pending=(end,end,prior,payload,digest)
        return rows

    def prune_group(self,token,prefix,reclaimed,barrier_at):
        batch=WriteBatch(raw_mode=True)
        # Explicit bounded tombstones; retained reconnect anchor is never deleted.
        count=0
        for end,*_ in self.records():
            if end>token:break
            count+=1
            if count>256:raise CaptureError('spool_corrupt')
            batch.delete(b't:'+end.encode())
        values=dict(pruned_prefix=prefix,bytes=self.get('bytes')-reclaimed)
        if barrier_at is not None:values['barrier_at_ms']=barrier_at
        self.metadata_batch(batch,values);fault('before_spool_prune_commit');self.write(batch)
        fault('after_spool_prune_commit');self.after_write();return reclaimed

    def compact(self):
        # Qualification hook, not an unbounded automatic write-path vacuum.
        if not self.reader.acquire(blocking=False):raise ReadBusy()
        try:
            start=time.monotonic();self.db.flush(True);self.db.compact_range(None,None)
            self.stats['compactions']+=1;self.stats['compaction_ms']+=(time.monotonic()-start)*1000
            self.after_write()
        finally:self.reader.release()

    def progress(self):
        sizes=self.sizes()
        return dict(self.stats,journal_mode='rocksdb',synchronous=2,wal_enabled=True,
                    physical_bytes=sum(sizes.values()),physical_limit=self.limit,
                    wal_bytes=sum(n for k,n in sizes.items() if k.endswith('.log')),
                    sst_bytes=sum(n for k,n in sizes.items() if k.endswith('.sst')),
                    memtable_bytes=self.db.property_int_value('rocksdb.cur-size-all-mem-tables'),
                    pending_compaction_bytes=self.db.property_int_value('rocksdb.estimate-pending-compaction-bytes'),
                    write_stopped=self.db.property_int_value('rocksdb.is-write-stopped'),
                    compaction_stats=self.db.property_value('rocksdb.cfstats-no-file-histogram')[:16384])
