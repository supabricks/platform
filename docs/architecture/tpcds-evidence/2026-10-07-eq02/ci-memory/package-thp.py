import hashlib,json,subprocess,sys
from pathlib import Path
sys.path.insert(0,'install/native')
from analytics import digest,worker_launcher
base=Path('build/eq02-merge-review/current-unpack/supabricks').resolve()
dest=Path('build/eq02-merge-review/thp-candidate').resolve();assert not dest.exists()
verification=json.loads(subprocess.check_output([str(base/'bin/supabricks'),'installation','verify'],text=True));assert verification['verified']
identity=digest(base/'release.json');assert identity=='16c80431e8a31e0f462c1454d9a609414b5fd69d0f540831e147ecad62a7b515'
subprocess.run(['cp','-al',str(base),str(dest)],check=True)
name='python/analytics/python';p=dest/name;p.unlink();p.write_text(worker_launcher('linux-x86_64'));p.chmod(0o755)
manifest=json.loads((base/'release.json').read_text());manifest['files'][name]={'sha256':digest(p),'executable':True}
(dest/'release.json').unlink();(dest/'release.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
verified=json.loads(subprocess.check_output([str(dest/'bin/supabricks'),'installation','verify'],text=True));assert verified['verified']
proof=dict(scope='Engineering wrapper-only candidate; not a signed release qualification',base_release_identity=identity,release_identity=digest(dest/'release.json'),changed_payload_files=[name],source_sha256=digest(Path('install/native/analytics.py')),candidate_wrapper_sha256=digest(p),verification=verified)
Path('build/eq02-merge-review/thp-candidate.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof))
assert digest(base/'release.json')==identity
