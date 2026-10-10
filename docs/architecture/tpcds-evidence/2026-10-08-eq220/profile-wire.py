"""Replay cloned durable pgoutput through the installed strict capture decoder."""
import gc,hashlib,json,socket,struct,sys,threading,time
from pathlib import Path
release=Path(sys.argv[1]).resolve();source=Path(sys.argv[2]);output=Path(sys.argv[3]);output.mkdir(mode=0o700)
sys.path.insert(0,str(release/'python/analytics'))
from incremental.storage import journal
from capture.protocol import Decoder, Wire
from capture.spool import frames
config=json.loads(next((source/'analytics/apply-workers').glob('*/input.json')).read_text());config.pop('journal_access',None)
config['spool']='/data2/supabricks-eq/eq220/plan-spool-02/spool.sqlite3';config['deadline_ms']=int(time.time()*1000)+120000
schema,transactions,_,_=journal(config)
relations=[]
for oid,(namespace,name,identity,columns) in schema.items():
    body=b'R'+struct.pack('!I',int(oid))+namespace.encode()+b'\0'+name.encode()+b'\0'+identity.encode()+struct.pack('!H',len(columns))
    for flags,name,typ,mod in columns:body+=struct.pack('!B',flags)+name.encode()+b'\0'+struct.pack('!Ii',typ,mod)
    relations.append(body)
packets=[frame for _,payload in transactions for frame in frames(payload)]
rows=sum(p[:1] in (b'I',b'U',b'D') for p in packets)
wire_bytes=b''.join(b'd'+struct.pack('!I',len(frame)+29)+b'w'+struct.pack('!QQq',1,2,0)+frame for frame in relations+packets)
class CountedSocket:
    def __init__(self, sock):self.sock=sock;self.reads=0
    def fileno(self):return self.sock.fileno()
    def recv(self,n):self.reads+=1;return self.sock.recv(n)
def decode():
    decoder=Decoder(schema,'diagnostic-schema-fence','supabricks.barrier.'+config['identity']['generation'])
    left,right=socket.socketpair();wire=object.__new__(Wire);wire.socket=CountedSocket(left)
    failures=[]
    def send():
        try:
            for start in range(0,len(wire_bytes),65536):right.sendall(wire_bytes[start:start+65536])
        except Exception as exc:failures.append(repr(exc))
        finally:right.close()
    producer=threading.Thread(target=send)
    result=[];count=0;started=time.monotonic();producer.start()
    try:
        while count<len(relations)+len(packets):
            received=wire.receive(timeout=.01)
            if received is None:continue
            tag,end,payload=received;assert tag=='data' and end==2;count+=1
            commit=decoder.feed(payload)
            if commit:result.append((commit[1],commit[2]))
        elapsed=time.monotonic()-started
    finally:left.close();producer.join()
    assert not failures and decoder.pending is None
    return result,elapsed,wire.socket.reads
metrics=[];reads=[]
for repeat in range(3):
    gc.collect();result,elapsed,read_count=decode();metrics.append(elapsed);reads.append(read_count)
    assert result==transactions
    del result
(output/'result.json').write_text(json.dumps(dict(status='PASS',scope='Isolated socketpair transport and strict capture decoding from cloned journal; no PostgreSQL/fsync/publication throughput claim',release_identity=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),transactions=len(transactions),rows=rows,frames=len(packets),wire_bytes=len(wire_bytes),payload_sha256=hashlib.sha256(b''.join(packets)).hexdigest(),unprofiled_seconds=metrics,recv_calls=reads,exact_encoded_transactions=True),indent=2)+'\n')
