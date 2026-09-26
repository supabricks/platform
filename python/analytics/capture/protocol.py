"""Strict pgoutput v1 complete-transaction parser and bounded replication framing."""
import socket
import select
import struct
import time
from .spool import CaptureError, MAX_MESSAGE, MAX_TRANSACTION, fault, pg_lsn


class Reader:
    def __init__(self, data):
        if len(data)>MAX_MESSAGE: raise CaptureError('message_budget')
        self.data=data;self.offset=0
    def take(self,n):
        if n<0 or self.offset+n>len(self.data): raise CaptureError('invalid_pgoutput')
        value=self.data[self.offset:self.offset+n];self.offset+=n;return value
    def number(self,fmt): return struct.unpack('!'+fmt,self.take(struct.calcsize('!'+fmt)))[0]
    def string(self):
        end=self.data.find(b'\0',self.offset)
        if end<0: raise CaptureError('invalid_pgoutput')
        try: return self.take(end-self.offset+1)[:-1].decode('utf-8')
        except UnicodeError: raise CaptureError('invalid_pgoutput') from None
    def tuple(self,count):
        if self.number('H')!=count: raise CaptureError('schema_changed')
        for _ in range(count):
            kind=self.take(1)
            if kind==b't':
                try:self.take(self.number('I')).decode('utf-8')
                except UnicodeError:raise CaptureError('invalid_pgoutput') from None
            elif kind not in (b'n',b'u'):raise CaptureError('unsupported_tuple')
    def finish(self):
        if self.offset!=len(self.data):raise CaptureError('invalid_pgoutput')


def barrier_message(flags,prefix,content,expected):
    import uuid
    if flags!=1 or expected is None or prefix!=expected or len(content)!=36:raise CaptureError('unsupported_message')
    try:
        run=content.decode('ascii')
        if str(uuid.UUID(run))!=run:raise ValueError()
    except (ValueError,UnicodeError):raise CaptureError('invalid_barrier') from None
    return run


class Decoder:
    def __init__(self,schema,fence,barrier_prefix=None):
        self.expected=schema;self.fence=fence;self.barrier_prefix=barrier_prefix;self.relations=set();self.pending=None
    def feed(self,payload):
        r=Reader(payload);tag=r.take(1);result=None;store=True
        if tag==b'B':
            if self.pending is not None:raise CaptureError('invalid_transaction')
            final,stamp,xid=r.number('Q'),r.number('q'),r.number('I')
            self.pending=dict(final=final,bytes=0,frames=[])
        elif tag==b'R':
            oid=str(r.number('I'));namespace,name=r.string(),r.string();identity=r.take(1).decode();count=r.number('H')
            if count>128:raise CaptureError('schema_changed')
            cols=[[r.number('B'),r.string(),r.number('I'),r.number('i')] for _ in range(count)]
            if self.expected.get(oid)!=[namespace,name,identity,cols]:raise CaptureError('schema_changed')
            self.relations.add(oid);store=False
        elif tag in (b'I',b'U',b'D'):
            if self.pending is None:raise CaptureError('invalid_transaction')
            oid=str(r.number('I'))
            if oid not in self.relations:raise CaptureError('unknown_relation')
            count=len(self.expected[oid][3]);marker=r.take(1)
            if tag!=b'I' and marker in (b'K',b'O'):
                r.tuple(count)
                if tag==b'U':marker=r.take(1)
            elif tag==b'D':raise CaptureError('missing_replica_identity')
            if tag!=b'D':
                if marker!=b'N':raise CaptureError('invalid_tuple')
                r.tuple(count)
        elif tag==b'M':
            flags=r.number('B');r.number('Q');prefix=r.string();content=r.take(r.number('I'))
            if flags!=1 or self.pending is None:raise CaptureError('unsupported_message')
            if prefix==self.fence:raise CaptureError('schema_changed')
            barrier_message(flags,prefix,content,self.barrier_prefix)
        elif tag==b'C':
            if self.pending is None or r.number('B')!=0:raise CaptureError('invalid_transaction')
            commit,end,stamp=r.number('Q'),r.number('Q'),r.number('q')
            if commit!=self.pending['final'] or end<=commit:raise CaptureError('invalid_commit')
            result=(commit,end)
        else:raise CaptureError('unsupported_pgoutput')
        r.finish()
        if self.pending is not None:
            self.pending['bytes']+=len(payload)+4
            if self.pending['bytes']>MAX_TRANSACTION:raise CaptureError('transaction_budget')
            # Relation messages can be repeated after reconnect. Excluding them makes
            # the transaction checksum stable; the immutable schema is in spool metadata.
            if store:self.pending['frames'].append(struct.pack('!I',len(payload))+payload)
        if result:
            commit,end=result;data=b''.join(self.pending['frames']);self.pending=None
            return commit,end,data
        return None


class Wire:
    def __init__(self,directory,port):
        self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.socket.settimeout(3)
        self.socket.connect(f'{directory}/.s.PGSQL.{port}')
        params=b'user\0cloud_admin\0database\0postgres\0replication\0database\0application_name\0supabricks-capture\0client_encoding\0UTF8\0options\0-c logical_decoding_work_mem=1024 -c temp_file_limit=65536 -c log_statement=none -c log_min_error_statement=panic\0\0'
        body=struct.pack('!I',196608)+params
        self.socket.sendall(struct.pack('!I',len(body)+4)+body)
        while True:
            tag,data=self.packet()
            if tag==b'R' and data!=struct.pack('!I',0):raise CaptureError('capture_authentication_profile')
            if tag==b'E':raise CaptureError('replication_startup_failed')
            if tag==b'Z':break
            if tag not in (b'R',b'S',b'K',b'N'):raise CaptureError('replication_protocol')
    def close(self):self.socket.close()
    def exact(self,size):
        chunks=bytearray()
        while len(chunks)<size:
            part=self.socket.recv(size-len(chunks))
            if not part:raise ConnectionError('replication_disconnected')
            chunks.extend(part)
        return bytes(chunks)
    def packet(self):
        tag=self.exact(1);size=struct.unpack('!I',self.exact(4))[0]-4
        # Reject the wire length before libpq/Python allocates an arbitrary row.
        if size<0 or size>MAX_MESSAGE+25:raise CaptureError('message_budget')
        return tag,self.exact(size)
    def start(self,slot,publication,captured):
        # Names originate only in validated platform UUIDs, never user SQL.
        if not all(c in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in slot+publication):raise CaptureError('resource_identity')
        command=f"START_REPLICATION SLOT {slot} LOGICAL {pg_lsn(captured)} (proto_version '1', publication_names '{publication}', messages 'true')"
        body=command.encode()+b'\0';self.socket.sendall(b'Q'+struct.pack('!I',len(body)+4)+body)
        while True:
            tag,data=self.packet()
            if tag==b'W':return
            if tag not in (b'N',b'S'):raise CaptureError('replication_start_failed')
    def feedback(self,captured,request=False):
        fault('before_source_ack')
        stamp=int((time.time()-946684800)*1000000)
        body=b'r'+struct.pack('!QQQqB',captured,captured,0,stamp,int(request))
        self.socket.sendall(b'd'+struct.pack('!I',len(body)+4)+body)
        fault('after_source_ack')
    def stream_packet(self, timeout):
        # Keep a partial frame across deadlines. A readable socket does not
        # promise that its whole packet is available; exact() could wait 3 s.
        if not hasattr(self,'stream_buffer'):self.stream_buffer=bytearray()
        deadline=time.monotonic()+timeout
        while True:
            size=5
            if len(self.stream_buffer)>=5:
                payload_size=struct.unpack('!I',self.stream_buffer[1:5])[0]-4
                if payload_size<0 or payload_size>MAX_MESSAGE+25:raise CaptureError('message_budget')
                size=5+payload_size
                if len(self.stream_buffer)==size:
                    tag=bytes(self.stream_buffer[:1]);data=bytes(self.stream_buffer[5:]);self.stream_buffer.clear()
                    return tag,data
            remaining=max(0,deadline-time.monotonic())
            if not select.select([self.socket],[],[],remaining)[0]:return None
            part=self.socket.recv(size-len(self.stream_buffer))
            if not part:raise ConnectionError('replication_disconnected')
            self.stream_buffer.extend(part)
            if time.monotonic()>=deadline:return None

    def receive(self, timeout=None):
        packet=self.packet() if timeout is None else self.stream_packet(timeout)
        if packet is None:return None
        tag,data=packet
        if tag!=b'd' or not data:raise CaptureError('replication_stream_failed')
        if data[0:1]==b'w' and len(data)>=25:return 'data',struct.unpack('!Q',data[9:17])[0],data[25:]
        if data[0:1]==b'k' and len(data)==18:return 'keepalive',struct.unpack('!Q',data[1:9])[0],data[17]
        raise CaptureError('replication_protocol')
