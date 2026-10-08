"""Source-bound native/worker engineering package for incremental storage admission."""
import argparse
import json
from pathlib import Path
import subprocess
from inputs import sha
from package import overlay

WORKERS=['incremental_worker.py','incremental/storage.py','incremental/planning.py',
         'incremental/maintenance.py','incremental/reuse.py','incremental/rows.py',
         'capture/protocol.py']


def package(base,destination,proof):
    repo=Path(__file__).resolve().parents[2]
    def git(*args):return subprocess.check_output(['git',*args],cwd=repo,text=True).strip()
    if git('status','--porcelain'):raise ValueError('commit the complete candidate before packaging')
    revision=git('rev-parse','HEAD')
    if destination.exists() or proof.exists():raise ValueError('fresh candidate paths required')
    proof.parent.mkdir(parents=True,exist_ok=True)
    command=['cargo','build','--release','--locked','-p','supabricks-local']
    log=proof.with_suffix('.build.log')
    with log.open('x') as stream:subprocess.run(command,cwd=repo,stdout=stream,stderr=subprocess.STDOUT,check=True)
    if git('status','--porcelain') or git('rev-parse','HEAD')!=revision:raise ValueError('source changed during build')
    python_proof=proof.with_suffix('.python.json')
    destination.parent.mkdir(parents=True,exist_ok=True)
    overlay(base,destination,repo,python_proof,WORKERS)
    manifest=json.loads((destination/'release.json').read_text())
    original=json.loads((base/'release.json').read_text())
    binary=destination/'bin/supabricks'
    binary.unlink();binary.write_bytes((repo/'target/release/supabricks').read_bytes());binary.chmod(0o755)
    manifest['files']['bin/supabricks']={'sha256':sha(binary),'executable':True}
    manifest['version']=destination.name
    path=destination/'release.json';path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    # overlay already verified the original manifest and safely detached all
    # changed hardlinks. Bind every worker to the exact committed source too.
    for name in WORKERS:
        committed=subprocess.check_output(['git','show',revision+':python/analytics/'+name],cwd=repo)
        if (destination/'python/analytics'/name).read_bytes()!=committed:raise ValueError('worker differs from committed source')
    verified=json.loads(subprocess.check_output([str(binary),'installation','verify'],text=True))
    if not verified['verified'] or verified['identity']!=sha(path):raise ValueError('candidate verification failed')
    if sha(base/'bin/supabricks')!=original['files']['bin/supabricks']['sha256']:raise ValueError('base binary changed')
    report=dict(signed_release=False,platform_revision=revision,source_dirty=False,command=command,
        cargo_lock_sha256=sha(repo/'Cargo.lock'),build_log_sha256=sha(log),
        rustc=subprocess.check_output(['rustc','--version'],text=True).strip(),
        base_release_identity=sha(base/'release.json'),candidate_release_identity=sha(path),
        python_overlay_sha256=sha(python_proof),installation_verification=verified,
        changed_files={name:entry for name,entry in manifest['files'].items() if original['files'].get(name)!=entry})
    proof.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('base','destination','proof'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    package(args.base.resolve(),args.destination.resolve(),args.proof.resolve())
