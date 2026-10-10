"""Build source-bound EQ230 diagnostic package; no hooks in ordinary releases."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def build(base,dest,proof):
    repo=Path(__file__).resolve().parents[2]
    def git(*args):return subprocess.check_output(['git',*args],cwd=repo,text=True).strip()
    if git('status','--porcelain'):raise ValueError('commit complete diagnostic source first')
    revision=git('rev-parse','HEAD')
    if dest.exists() or proof.exists():raise ValueError('fresh output paths required')
    command=['cargo','build','--release','--locked','-p','supabricks-local','--features','sync-profile']
    with proof.with_suffix('.build.log').open('x') as log:subprocess.run(command,cwd=repo,stdout=log,stderr=subprocess.STDOUT,check=True)
    if git('status','--porcelain') or git('rev-parse','HEAD')!=revision:raise ValueError('source changed during build')
    original=sha(base/'release.json');manifest=json.loads((base/'release.json').read_text());changes={}
    subprocess.run(['cp','-al',str(base),str(dest)],check=True)
    def replace(relative,data,executable=False):
        path=dest/relative;before=sha(path) if path.exists() else None
        if path.exists():path.unlink()
        path.write_bytes(data);path.chmod(0o755 if executable else 0o644)
        manifest['files'][relative]=dict(executable=executable,sha256=sha(path));changes[relative]=dict(before=before,after=sha(path))
    replace('bin/supabricks',(repo/'target/release/supabricks').read_bytes(),True)
    replace('python/analytics/batch_profile.py',Path(__file__).with_name('batch_profile.py').read_bytes())
    for file,role in [('capture_worker.py','capture'),('incremental_worker.py','incremental')]:
        path=base/'python/analytics'/file;source=path.read_text()
        # Do not stack another profiler or silently instrument a stale worker.
        assert path.read_bytes()==subprocess.check_output(['git','show',revision+':python/analytics/'+file],cwd=repo)
        assert 'worker_profile.install' not in source and 'batch_profile.install' not in source
        marker="if __name__=='__main__':";assert source.count(marker)==1
        source=source.replace(marker,f"import batch_profile\nbatch_profile.install(globals(), '{role}')\n\n"+marker)
        replace('python/analytics/'+file,source.encode(),manifest['files']['python/analytics/'+file]['executable'])
    manifest['version']=dest.name
    path=dest/'release.json';path.unlink();path.write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    assert sha(base/'release.json')==original
    for rel,entry in changes.items():
        if entry['before']:assert sha(base/rel)==entry['before']
    verified=json.loads(subprocess.check_output([str(dest/'bin/supabricks'),'installation','verify'],text=True))
    proof.write_text(json.dumps(dict(status='PASS',source=revision,source_dirty=False,command=command,base_release_identity=original,
        candidate_release_identity=sha(path),signed_release=False,experimental_sync_lookahead=False,
        profiler_sha256=sha(Path(__file__).with_name('batch_profile.py')),changes=changes,installation_verification=verified),indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('base','destination','proof'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();build(args.base.resolve(),args.destination.resolve(),args.proof.resolve())
