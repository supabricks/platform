"""Recompute bounded observer costs after the controller has archived all trials."""
from pathlib import Path
import fcntl,hashlib,json,subprocess,sys,time
root=Path(__file__).resolve().parent;repo=root.parent.parent
lock=(root/'.review.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
analysis=repo/'e2e/native/performance/host_io_analysis.py'
paths=[analysis,analysis.with_name('host_io.py')]
identities={str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
for _ in range(4320):
    s=json.loads((root/'status.json').read_text())
    if s['status']=='stopped_for_investigation':raise SystemExit('controller stopped; no automatic outcome review')
    if s['status']=='measurements_complete_review_required':
        assert all(hashlib.sha256((repo/p).read_bytes()).hexdigest()==h for p,h in identities.items()),'offline analyzer changed'
        subprocess.run([sys.executable,str(analysis),str(root/'observer-controls-02'),'--output',str(root/'observer-review.json')],check=True)
        (root/'offline-review-receipt.json').write_text(json.dumps(dict(status='recomputed_review_required',analysis_files_sha256=identities,review_sha256=hashlib.sha256((root/'observer-review.json').read_bytes()).hexdigest()),indent=2)+'\n')
        print('Review recomputed; no automatic source-fix or performance claim',flush=True);break
    time.sleep(10)
else:raise SystemExit('review wait exceeded 12 hours; benchmark state unchanged')
