from pathlib import Path
import argparse,gzip,hashlib,json,subprocess,sys
parser=argparse.ArgumentParser(description='Recover the exact historical SP04 payload; no unknown hashes are admitted.')
parser.add_argument('--workspace',type=Path,required=True)
parser.add_argument('--repo',type=Path,required=True)
args=parser.parse_args()
root=args.workspace.resolve()
repo=args.repo.resolve()
proofroot=repo/'docs/architecture/sync-performance-evidence'
base=root/'exact-base-release/supabricks'
dest=root/'accepted-runtime'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert json.loads((base/'release.json').read_text())['provenance']['platform_commit']=='8f6ff8461e7f6a18c06f8f51db82c22bc870de76'
b=gzip.decompress((proofroot/'2026-09-25-sp01/packages/b6a0b4b/runtime-inventory.json.gz').read_bytes())
expected=json.loads(b)
proofs=['2026-09-26-sp02/packages/candidate-proof.json','2026-09-26-sp03a/packages/candidate-proof.json','2026-09-26-sp03b/packages/candidate/overlay-proof.json','2026-09-26-sp04/packages/candidate.json']
for name in proofs:
    proof=json.loads((proofroot/name).read_text())
    assert hashlib.sha256(b).hexdigest()==proof['base_release_sha256']
    for f,c in proof['changes'].items():
        assert expected['files'].get(f,{}).get('sha256')==c['before'],f
        expected['files'][f]={'executable':expected['files'].get(f,{}).get('executable',False),'sha256':c['after']}
    b=(json.dumps(expected,indent=2,sort_keys=True)+'\n').encode()
    assert hashlib.sha256(b).hexdigest()==proof['release_sha256']
(root/'expected-release.json').write_bytes(b)
base_manifest=json.loads((base/'release.json').read_text())
print('Verifying upstream payload...',flush=True)
for name,v in base_manifest['files'].items():assert sha(base/name)==v['sha256'],name
assert sha(repo/'target/release/supabricks')==expected['files']['bin/supabricks']['sha256']
assert sha(root/'profile_io.so')==expected['files']['python/analytics/profile_io.so']['sha256']
sys.path.insert(0,str(root/'sp04-harness/e2e/native/performance'))
import profile_package
profile_package.build(base,dest,repo/'target/release/supabricks',root/'profile_io.so')
changes=[]
for name,entry in expected['files'].items():
 p=dest/name
 if p.exists() and sha(p)==entry['sha256']:continue
 if name.endswith('.pyc'):continue
 if name=='provenance/sqlite-policy.json':data=(root/'sp04-harness/components/sqlite-policy.json').read_bytes()
 elif name.startswith('python/analytics/') and name.endswith('.py'):
  src=root/'sp04-harness'/name
  data=src.read_bytes()
  if name.endswith(('capture_worker.py','incremental_worker.py','export.py')):
   role={'capture_worker.py':'capture','incremental_worker.py':'incremental','export.py':'export'}[p.name]
   s=data.decode();lines=s.splitlines(keepends=True);lines.insert(2,'import worker_profile\n');s=''.join(lines)
   marker="if __name__=='__main__':" if "if __name__=='__main__':" in s else "if __name__ == '__main__':"
   data=s.replace(marker,"worker_profile.install(globals(), '"+role+"')\n\n"+marker).encode()
 else:raise RuntimeError('Unsupported mismatch '+name)
 assert hashlib.sha256(data).hexdigest()==entry['sha256'],name
 p.unlink(missing_ok=True);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);p.chmod(0o755 if entry['executable'] else 0o644);changes.append(name)
for name,entry in expected['files'].items():
 p=dest/name
 if not name.endswith('.pyc') or (p.exists() and sha(p)==entry['sha256']):continue
 source=p.parent.parent/(p.name.split('.cpython-')[0]+'.py')
 p.unlink(missing_ok=True)
 subprocess.run([str(dest/'python/analytics/python'),'-B','-c',"import py_compile,sys;py_compile.compile(sys.argv[1],dfile=sys.argv[2],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)",str(source),str(source.relative_to(dest))],check=True)
 assert sha(p)==entry['sha256'],name
 changes.append(name)
# This is the historically committed inventory, never generated from unknown bytes.
(dest/'release.json').unlink();(dest/'release.json').write_bytes((root/'expected-release.json').read_bytes())
for name,v in expected['files'].items():
 assert sha(dest/name)==v['sha256'],name
 assert bool((dest/name).stat().st_mode&0o111)==v['executable'],name
extra=[str(p.relative_to(dest)) for p in dest.rglob('*') if p.is_file() and str(p.relative_to(dest)) not in expected['files'] and p.name!='release.json']
assert not extra,extra
receipt=dict(status='passed',release_identity=sha(dest/'release.json'),binary_sha256=sha(dest/'bin/supabricks'),verified_files=len(expected['files']),upstream_archive_sha256=sha(root/'exact-base-release/supabricks-v0.1.0-alpha.36-linux-x86_64.tar.gz'),upstream_inventory_sha256=sha(base/'release.json'),runtime_revision='e10d5152f5681af7c233701e0fd75c100cbf4c6c',harness_revision='be4701cce5694ed00349ab3db9b577592965d7d7',method='Recovered exact historical inventory from committed SP01 inventory and SP02/SP03a/SP03b/SP04 overlay proofs; verified every physical file and executable bit. Native binary survived byte-identically; IO probe rebuilt byte-identically; changed Python and checked-hash caches reconstructed from frozen SP04 sources.',changes_after_instrumentation=changes,signed_release=False)
(root/'package-recovery.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
