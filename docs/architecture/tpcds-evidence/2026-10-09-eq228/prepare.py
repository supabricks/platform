import itertools,json,sqlite3,sys,time,uuid,struct
from pathlib import Path
sys.path.insert(0,str(Path('python/analytics').resolve()))
from capture.spool import Spool,lsn,pg_lsn
source=Path('<evidence-root>/eq220/lgrow');out=Path('<evidence-root>/eq228/fixture02');out.mkdir(parents=True,mode=0o700)
r=json.loads((source/'result.json').read_text());prev=r['publication']['descriptor'];root=source/'state'/prev['generation'];owner=json.loads((root/'owner.json').read_text())
spoolpath=next((source/'state/capture').glob('*/spool/spool.sqlite3'))
with sqlite3.connect(f'file:{spoolpath}?mode=ro',uri=True) as db:
 schema=json.loads(db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0])
oid,table=next((k,v) for k,v in schema.items() if v[1]=='store_returns');offset=next(t['rows'] for t in r['tables'] if t['table']=='store_returns')
start=lsn(prev['manifest']['source']['lsn']);end=start
s=Spool(out/'spool',owner['identity']);s.establish(start,schema);s.set('bootstrap',dict(id=owner['bootstrap_id'],lsn=owner['bootstrap_lsn']))
def insert(line):
 vals=line.rstrip('\n').split('|')[:-1];assert len(vals)==len(table[3]);b=b'I'+struct.pack('!I',int(oid))+b'N'+struct.pack('!H',len(vals))
 for v in vals:
  value=v.encode();b+=b'n' if v=='' else b't'+struct.pack('!I',len(value))+value
 return b
with Path('<evidence-root>/sf100/gen-01/data/store_returns.dat').open(encoding='latin1') as f:
 rows=iter(itertools.islice(f,offset,offset+32768));count=0
 while chunk:=list(itertools.islice(rows,1024)):
  at=end+1;end+=2;messages=[b'B'+struct.pack('!QqI',at,0,123),*(insert(l) for l in chunk),b'C'+struct.pack('!BQQq',0,at,end,0)]
  s.append(at,end,b''.join(struct.pack('!I',len(p))+p for p in messages));count+=len(chunk)
s.close();assert count==32768
config=dict(id=str(uuid.uuid4()),epoch_id=str(uuid.uuid4()),ordinal=prev['ordinal']+1,source_revision=prev['source_revision'],worker_generation=1,attempt=1,identity=owner['identity'],bootstrap_id=owner['bootstrap_id'],bootstrap_lsn=owner['bootstrap_lsn'],bootstrap_manifest='unused',spool=str(out/'spool/spool.sqlite3'),previous=prev,storage_profile='large',after_lsn=pg_lsn(start),target_lsn=pg_lsn(end))
(out/'config.json').write_text(json.dumps(config));(out/'source.json').write_text(json.dumps(dict(root=str(root),rows=count,offset=offset,oid=oid,scope='Private component fixture using next actual SF100 rows; synthetic LSN framing; no source ACK or controller publication')))
print(out)
