"""Private, one-reader journal transport. Only capture opens the backend.

One service thread, one active snapshot, listen backlog one, no application queue.
Requests expire on the original monotonic deadline, including time in the socket
backlog. Snapshots close before any response writes. A final fenced completion
marker is required before the client can return bytes to materialization.
"""
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import select
import socket
import sqlite3
import stat
import struct
import threading
import time
import uuid
from .journal import ReadBusy
from .ranges import read_range as materialize
from .spool import CaptureError, canonical, lsn, MAX_TRANSACTION, MAX_BATCH

MAX_REQUEST=32*1024
MAX_HEADER=2*1024*1024+4096
MAX_RESPONSE=24*1024*1024
MAX_RECORDS=65536
READ_SECONDS=3.0
PRIVATE_READ_ATTEMPTS=8
ERRORS=frozenset(('journal_read_busy','journal_read_deadline','journal_read_cancelled',
    'journal_owner_protocol','journal_owner_budget','journal_owner_fenced','spool_io',
    'unsafe_journal_owner_path','spool_identity_mismatch','source_history_lost',
    'spool_corrupt','spool_history_gap','spool_metadata_budget','spool_budget',
    'unsafe_spool_path','unsafe_spool_directory','invalid_pruned_prefix'))
RECORD=struct.Struct('!QI32s')
FIELDS=('id','attempt','epoch_id','identity','worker_generation','bootstrap_lsn',
        'after_lsn','target_lsn','deadline_ms','journal_access')


def request_for(config, *, suffix=False):
    request={key:config[key] for key in FIELDS}
    if 'preparation' in config:request['preparation']=config['preparation']
    if suffix:
        request['suffix']=1
        request['after_lsn']=config['prepared_read_after']
    return request


def private_directory(path):
    meta=path.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid!=os.getuid() or meta.st_mode & 0o077:
        raise CaptureError('unsafe_journal_owner_path')


def private_json(path, limit, check=lambda:None):
    deadline=time.monotonic()+READ_SECONDS
    def validate(meta):
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid()
            or meta.st_mode & 0o077 or meta.st_nlink not in (0,1)
            or meta.st_size>limit):
            raise CaptureError('unsafe_journal_owner_path')
    for _ in range(PRIVATE_READ_ATTEMPTS):
        check()  # Request retries keep the original channel/cancellation budget.
        if time.monotonic()>=deadline:raise ReadBusy()
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as source:
            meta=os.fstat(source.fileno());validate(meta)
            # Atomic replacement unlinks the opened version. Discard it; it
            # must never supply authority, even if its contents are identical.
            if meta.st_nlink==0:continue
            data=source.read(limit+1)
            meta=os.fstat(source.fileno());validate(meta)
            current=path.lstat();validate(current)
            if meta.st_nlink==0 or current.st_nlink==0 or (meta.st_dev,meta.st_ino)!=(current.st_dev,current.st_ino):continue
        check()
        if time.monotonic()>=deadline:raise ReadBusy()
        if len(data)>limit:raise CaptureError('journal_owner_budget')
        return json.loads(data)
    raise ReadBusy()


class Channel:
    def __init__(self, connection, deadline, stopped=lambda:False):
        self.connection=connection;self.deadline=deadline;self.stopped=stopped
        self.received=0;self.sent=0

    def check(self):
        if self.stopped():raise CaptureError('journal_read_cancelled')
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise CaptureError('journal_read_deadline')
        self.connection.settimeout(min(.1,remaining))

    def receive(self, count):
        # Caller validates each allocation before reading it.
        chunks=bytearray()
        while len(chunks)<count:
            self.check()
            try:part=self.connection.recv(min(65536,count-len(chunks)))
            except socket.timeout:continue
            if not part:raise CaptureError('journal_owner_incomplete')
            chunks.extend(part);self.received+=len(part)
        return bytes(chunks)

    def send(self, value):
        remaining=memoryview(value)
        while remaining:
            self.check()
            try:count=self.connection.send(remaining[:65536])
            except socket.timeout:continue
            if not count:raise CaptureError('journal_owner_incomplete')
            self.sent+=count;remaining=remaining[count:]

    def frame(self, limit):
        size=struct.unpack('!I',self.receive(4))[0]
        if size>limit:raise CaptureError('journal_owner_budget')
        return self.receive(size)

    def cancelled(self):
        if self.stopped():return True
        # Any extra client bytes violate the single-request protocol. EOF means
        # the reader cancelled. Neither can prolong a database snapshot.
        return bool(select.select([self.connection],[],[],0)[0])

    def reader_check(self):
        self.check()
        if self.cancelled():raise CaptureError('journal_read_cancelled')


class Owner:
    def __init__(self, control, backend):
        self.control=Path(control);self.root=self.control.parents[2];self.backend=backend
        config=private_json(self.control,65536)
        self.identity=config['identity'];self.generation=config['worker_generation']
        self.endpoint=Path(config['journal_access']['endpoint'])
        expected=self.root/'tmp'/self.control.parent.name/'journal.sock'
        if self.endpoint!=expected or len(os.fsencode(self.endpoint))>103:
            raise CaptureError('unsafe_journal_owner_path')
        for directory in (self.root,self.root/'tmp',self.control.parent.parent,self.control.parent):private_directory(directory)
        self.endpoint.parent.mkdir(mode=0o700,exist_ok=True);private_directory(self.endpoint.parent)
        # The caller holds the backend's exclusive owner lock before reclaiming
        # a stale socket. Never unlink a symlink, regular file or foreign socket.
        try:
            meta=self.endpoint.lstat()
            if not stat.S_ISSOCK(meta.st_mode) or meta.st_uid!=os.getuid() or meta.st_mode & 0o077:
                raise CaptureError('unsafe_journal_owner_path')
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
                probe.settimeout(.1)
                try:probe.connect(str(self.endpoint))
                except OSError as error:
                    if error.errno!=errno.ECONNREFUSED:raise CaptureError('unsafe_journal_owner_path') from None
                else:raise CaptureError('journal_owner_already_running')
            self.endpoint.unlink()
        except FileNotFoundError:pass
        self.stopped=threading.Event();self.failed=False
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.endpoint));self.endpoint.chmod(0o600)
            self.socket_identity=self.endpoint.stat().st_ino
            self.listener.listen(1);self.listener.settimeout(.1)
            self.thread=threading.Thread(target=self.serve,name='journal-owner',daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close();raise

    def authorize(self, request, check=lambda:None):
        expected=set(FIELDS)|({'preparation'} if 'preparation' in request else set())|({'suffix'} if 'suffix' in request else set())
        if set(request)!=expected:raise CaptureError('journal_owner_fenced')
        if 'preparation' in request and request['preparation']!=1:raise CaptureError('journal_owner_fenced')
        if 'suffix' in request and (request['suffix']!=1 or 'preparation' in request):raise CaptureError('journal_owner_fenced')
        if str(uuid.UUID(request['id']))!=request['id'] or type(request['attempt']) is not int or not 1<=request['attempt']<=3:
            raise CaptureError('journal_owner_fenced')
        config=private_json(self.control,65536,check)
        if (config['identity']!=self.identity or config['worker_generation']!=self.generation
            or config['desired']!='running' or request['identity']!=self.identity
            or request['worker_generation']!=self.generation
            or request['journal_access']!=config['journal_access']):
            raise CaptureError('journal_owner_fenced')
        work=self.root/'analytics'/('prepare-work' if 'preparation' in request else 'apply-work')/request['id']
        for directory in (work.parent.parent,work.parent,work):private_directory(directory)
        issued=private_json(work/'input.json',4*1024*1024,check)
        if canonical(request_for(issued,suffix='suffix' in request))!=canonical(request):raise CaptureError('journal_owner_fenced')
        if time.time()*1000>=request['deadline_ms']:raise CaptureError('journal_read_deadline')
        after=lsn(request['after_lsn']);target=lsn(request['target_lsn'])
        if not 0<=after<=target<2**64:raise CaptureError('journal_owner_fenced')

    def handle(self, connection):
        channel=Channel(connection,time.monotonic()+READ_SECONDS,self.stopped.is_set)
        response_started=False
        try:
            envelope=json.loads(channel.frame(MAX_REQUEST))
            if set(envelope)!= {'version','request','deadline'} or envelope['version']!=1:
                raise CaptureError('journal_owner_protocol')
            deadline=envelope['deadline']
            if not isinstance(deadline,(int,float)) or not math.isfinite(deadline):
                raise CaptureError('journal_owner_protocol')
            channel.deadline=min(channel.deadline,deadline)
            request=envelope['request'];self.authorize(request,channel.reader_check);channel.reader_check()
            channel.deadline=min(channel.deadline,time.monotonic()+max(0,(request['deadline_ms']-time.time()*1000)/1000))
            start=time.monotonic()
            with self.backend.snapshot(channel.deadline,.1,channel.cancelled) as view:
                schema,records,end,used=materialize(view,request,channel.deadline,channel.reader_check,MAX_RECORDS)
            read_ms=(time.monotonic()-start)*1000
            # Materialized data is bounded; no database pin survives serialization
            # or a slow receiver. The writer continues on its original thread.
            start=time.monotonic();encoded=bytearray()
            for cut,payload in records:
                channel.reader_check()
                encoded.extend(RECORD.pack(cut,len(payload),hashlib.sha256(payload).digest()))
                encoded.extend(payload)
            encode_ms=(time.monotonic()-start)*1000
            header=canonical(dict(version=1,request_sha256=hashlib.sha256(canonical(request)).hexdigest(),
                schema=schema,count=len(records),end=end,used=used,owner_read_ms=read_ms,owner_encode_ms=encode_ms))
            if len(header)>MAX_HEADER or 8+len(header)+len(encoded)>MAX_RESPONSE:
                raise CaptureError('journal_owner_budget')
            self.authorize(request,channel.reader_check);channel.reader_check()
            response_started=True;channel.send(struct.pack('!I',len(header))+header);channel.send(encoded)
            self.authorize(request,channel.reader_check);channel.reader_check();channel.send(b'DONE')
        except (CaptureError,ReadBusy,OSError,sqlite3.Error,ValueError,KeyError,TypeError,OverflowError,RecursionError) as error:
            # Fixed diagnostics only; never serialize an exception with source
            # data, paths, SQL or credentials. Partial responses cannot succeed.
            code=('journal_read_busy' if isinstance(error,ReadBusy) else error.code if isinstance(error,CaptureError)
                  else 'spool_io' if isinstance(error,sqlite3.Error) else 'journal_owner_fenced')
            if code not in ERRORS:code='spool_io'
            if not response_started:
                data=canonical(dict(version=1,error=code))
                try:channel.send(struct.pack('!I',len(data))+data)
                except (CaptureError,OSError):pass

    def serve(self):
        try:
            while not self.stopped.is_set():
                try:connection,_=self.listener.accept()
                except socket.timeout:continue
                with connection:self.handle(connection)
        except BaseException:
            if not self.stopped.is_set():self.failed=True

    def check(self):
        if self.failed or not self.thread.is_alive():raise CaptureError('journal_owner_failed')

    def close(self):
        self.stopped.set();self.thread.join(READ_SECONDS+.5)
        self.listener.close()
        if self.thread.is_alive():raise CaptureError('journal_owner_failed')
        try:
            if self.endpoint.lstat().st_ino==self.socket_identity:self.endpoint.unlink()
        except FileNotFoundError:pass


def read_range(request, deadline, stats=None):
    """Return only a complete, request-bound response; never fall back to SQLite."""
    started=time.monotonic();endpoint=Path(request['journal_access']['endpoint'])
    data=canonical(dict(version=1,request=request,deadline=deadline))
    if len(data)>MAX_REQUEST:raise CaptureError('journal_owner_budget')
    try:
        private_directory(endpoint.parent)
        meta=endpoint.lstat()
        if not stat.S_ISSOCK(meta.st_mode) or meta.st_uid!=os.getuid() or meta.st_mode & 0o077:
            raise CaptureError('unsafe_journal_owner_path')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            channel=Channel(connection,deadline);channel.check();connection.connect(str(endpoint))
            channel.send(struct.pack('!I',len(data))+data)
            header=json.loads(channel.frame(MAX_HEADER))
            if not isinstance(header,dict) or header.get('version')!=1:raise CaptureError('journal_owner_protocol')
            if 'error' in header:
                if not isinstance(header['error'],str) or header['error'] not in ERRORS:raise CaptureError('journal_owner_protocol')
                if header['error']=='journal_read_busy':raise ReadBusy()
                raise CaptureError(header['error'])
            if header['request_sha256']!=hashlib.sha256(canonical(request)).hexdigest():
                raise CaptureError('journal_owner_fenced')
            count=header['count'];used=header['used'];end=header['end']
            if type(count) is not int or not 0<=count<=MAX_RECORDS or type(used) is not int or not 0<=used<=MAX_BATCH:
                raise CaptureError('journal_owner_budget')
            records=[];total=0;last=lsn(request['after_lsn']);target=lsn(request['target_lsn'])
            for _ in range(count):
                cut,size,checksum=RECORD.unpack(channel.receive(RECORD.size))
                if size>MAX_TRANSACTION or total+size>used or channel.received+size+4>MAX_RESPONSE:
                    raise CaptureError('journal_owner_budget')
                payload=channel.receive(size)
                if not last<cut<=target or hashlib.sha256(payload).digest()!=checksum:
                    raise CaptureError('spool_corrupt')
                records.append((cut,payload));last=cut;total+=size
            if total!=used or last!=end or (not records and target>last):raise CaptureError('journal_owner_protocol')
            if channel.receive(4)!=b'DONE':raise CaptureError('journal_owner_protocol')
            channel.check()
            if stats is not None:
                stats.update(request_bytes=channel.sent,response_bytes=channel.received,
                    owner_read_ms=header['owner_read_ms'],owner_encode_ms=header['owner_encode_ms'],
                    roundtrip_ms=(time.monotonic()-started)*1000)
            return header['schema'],records,end,used
    except CaptureError as error:
        if error.code in ('journal_owner_incomplete','journal_read_deadline','journal_read_cancelled'):
            # No bytes have escaped this read-only function. Drop partial data
            # and use the existing frozen-request pre-apply deferral budget.
            raise ReadBusy() from None
        raise
    except OSError as error:
        if error.errno in (errno.ENOENT,errno.ECONNREFUSED,errno.EAGAIN,errno.ECONNRESET,errno.EPIPE) or isinstance(error,socket.timeout):
            raise ReadBusy() from None
        raise
    except (ValueError,KeyError,TypeError,struct.error,RecursionError):
        raise CaptureError('journal_owner_protocol') from None
