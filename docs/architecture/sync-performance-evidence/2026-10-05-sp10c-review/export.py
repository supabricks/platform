import gzip,hashlib,json,shutil,tarfile
from pathlib import Path
repo=Path.cwd();out=repo/'docs/architecture/sync-performance-evidence/2026-10-05-sp10c-review';stage=repo/'build/sp10c-final-review/export';stage.mkdir(exist_ok=True)
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def sanitized(v):
 if isinstance(v,str):return v.replace(str(repo),'<platform>')
 if isinstance(v,list):return [sanitized(x) for x in v]
 if isinstance(v,dict):return {sanitized(k):sanitized(x) for k,x in v.items()}
 return v
def write(p,v):p.write_text(json.dumps(v,indent=2,sort_keys=True)+'\n')
def pack(name,folder):
 files=sorted(p for p in folder.rglob('*') if p.is_file() and p.name!='SHA256SUMS')
 # Outer checksums also cover inner checksum manifests.
 allfiles=sorted(p for p in folder.rglob('*') if p.is_file() and p!=folder/'SHA256SUMS')
 (folder/'SHA256SUMS').write_text(''.join(sha(p)+'  '+str(p.relative_to(folder))+'\n' for p in allfiles))
 dest=out/(name+'.tar.gz')
 with dest.open('wb') as raw,gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as gz,tarfile.open(fileobj=gz,mode='w') as tar:
  for p in sorted(folder.rglob('*')):
   if not p.is_file():continue
   info=tar.gettarinfo(str(p),str(p.relative_to(folder)));info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0
   with p.open('rb') as f:tar.addfile(info,f)
 print(name,dest.stat().st_size,flush=True)
for name in ('observer-bridge','engine-comparison','product-comparison'):
 source=repo/'build/sp10c-20261003/sequence-02'/name;target=stage/name;shutil.copytree(source/'archives',target)
 for fn in ('backend-component','lifecycle','predecessor-observer-controls','candidate-observer-controls'):
  receipts=json.loads((source/fn/'receipts.json').read_text());(target/fn).mkdir()
  for r in receipts:
   folder=target/fn/r['directory'];folder.mkdir()
   r['original_sha256']=r['sha256'].copy()
   for file,digest in r['original_sha256'].items():
    p=source/fn/r['directory']/file;assert sha(p)==digest
    write(folder/file,sanitized(json.loads(p.read_text())))
    r['sha256'][file]=sha(folder/file)
  write(target/fn/'receipts.json',sanitized(receipts))
  host=source/fn/'host'
  if host.exists():
   (target/fn/'host').mkdir()
   for p in host.glob('*.jsonl'):(target/fn/'host'/(p.name+'.gz')).write_bytes(gzip.compress(p.read_bytes(),mtime=0))
 pack(name,target)
for name in ('history-comparison','maintenance-comparison'):
 target=stage/name;shutil.copytree(repo/'build/issue157-20261005/qualification-01'/(name+'-archive'),target);pack(name,target)
source=repo/'build/issue157-20261005/qualification-01/sustained';target=stage/'sustained';target.mkdir()
r=json.loads((source/'receipt.json').read_text());r['original_sha256']=r['sha256'].copy()
for name,digest in r['original_sha256'].items():
 p=source/name;assert sha(p)==digest
 if p.suffix=='.json':write(target/name,sanitized(json.loads(p.read_text())))
 else:shutil.copy2(p,target/name)
 r['sha256'][name]=sha(target/name)
write(target/'receipt.json',sanitized(r));pack('sustained',target)
