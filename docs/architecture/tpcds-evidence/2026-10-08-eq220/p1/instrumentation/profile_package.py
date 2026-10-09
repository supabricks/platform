#!/usr/bin/env python3
"""Create a separate diagnostic package, breaking hardlinks before any edit."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def build(base,dest,binary,io_library):
    assert not dest.exists()
    original=sha(base/'release.json');manifest=json.loads((base/'release.json').read_text())
    subprocess.run(['cp','-al',str(base),str(dest)],check=True)
    changes={}
    def replace(relative,data,executable=False):
        p=dest/relative;before=sha(p) if p.exists() else None
        if p.exists():p.unlink()
        p.write_bytes(data);p.chmod(0o755 if executable else 0o644)
        manifest['files'][relative]=dict(executable=executable,sha256=sha(p));changes[relative]=dict(before=before,after=sha(p))
    replace('bin/supabricks',binary.read_bytes(),True)
    replace('python/analytics/profile_io.so',io_library.read_bytes())
    launcher=(base/'python/analytics/python').read_text()
    marker='exec "$directory/../runtime/bin/python3.12"'

    probe = '\n'.join([
        'profile_parent=$(dirname -- "${@: -1}")',
        'for profile_depth in 1 2 3 4 5 6; do',
        '  if [[ -f "$profile_parent/sync-profile/enabled" ]]; then',
        '    export LD_PRELOAD="$directory/profile_io.so"',
        '    break',
        '  fi',
        '  profile_parent=$(dirname -- "$profile_parent")',
        'done', ''])
    assert launcher.count(marker)==1
    replace('python/analytics/python',launcher.replace(marker,probe+marker).encode(),True)
    replace('python/analytics/worker_profile.py',Path(__file__).with_name('worker_profile.py').read_bytes())
    for file,role in [('capture_worker.py','capture'),('incremental_worker.py','incremental'),('export.py','export')]:
        p=base/'python/analytics'/file;s=p.read_text();lines=s.splitlines(keepends=True)
        lines.insert(2,'import worker_profile\n');s=''.join(lines)
        # Install after declarations, before entry. No exception behavior or retry changes.
        marker="if __name__=='__main__':" if "if __name__=='__main__':" in s else "if __name__ == '__main__':"
        assert s.count(marker)==1
        s=s.replace(marker,"worker_profile.install(globals(), '"+role+"')\n\n"+marker)
        replace('python/analytics/'+file,s.encode(),manifest['files']['python/analytics/'+file]['executable'])
    p=dest/'release.json';p.unlink();p.write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    assert sha(base/'release.json')==original
    for rel,entry in changes.items():
        if entry['before']:assert sha(base/rel)==entry['before']
    (dest.parent/(dest.name+'-instrumentation.json')).write_text(json.dumps(dict(baseline_manifest=original,profile_manifest=sha(p),changes=changes),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--binary',type=Path,required=True);p.add_argument('--io-library',type=Path,required=True);a=p.parse_args();build(a.base,a.output,a.binary,a.io_library)
