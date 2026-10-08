"""Replay cloned durable pgoutput through the installed strict capture decoder."""
import cProfile,gc,hashlib,json,pstats,struct,sys,time
from pathlib import Path
release=Path(sys.argv[1]).resolve();source=Path(sys.argv[2]);output=Path(sys.argv[3]);output.mkdir(mode=0o700)
sys.path.insert(0,str(release/'python/analytics'))
from incremental.storage import journal
from capture.protocol import Decoder
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
def decode():
    decoder=Decoder(schema,'diagnostic-schema-fence','supabricks.barrier.'+config['identity']['generation'])
    for frame in relations:assert decoder.feed(frame) is None
    result=[]
    for frame in packets:
        commit=decoder.feed(frame)
        if commit:result.append((commit[1],commit[2]))
    assert decoder.pending is None
    return result
metrics=[]
for repeat in range(3):
    gc.collect();start=time.monotonic();result=decode();metrics.append(time.monotonic()-start)
    assert result==transactions
    del result
prof=cProfile.Profile();result=prof.runcall(decode);assert result==transactions
prof.dump_stats(str(output/'capture.prof'))
with (output/'capture.txt').open('w') as stream:pstats.Stats(prof,stream=stream).strip_dirs().sort_stats('cumulative').print_stats(45)
(output/'result.json').write_text(json.dumps(dict(status='PASS',scope='Isolated strict capture decoding from a cloned journal; no socket/fsync/publication throughput claim',release_identity=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),transactions=len(transactions),rows=rows,frames=len(packets),payload_sha256=hashlib.sha256(b''.join(packets)).hexdigest(),unprofiled_seconds=metrics,exact_encoded_transactions=True),indent=2)+'\n')
