import json,sys,time,statistics,resource
from pathlib import Path
release=Path(sys.argv[1]);state=Path(sys.argv[2]);output=Path(sys.argv[3])
sys.path.insert(0,str(release/'python/analytics'))
from incremental.storage import verify_previous,inventory
result=next((state/'analytics/apply-work').glob('*/result.json'))
descriptor=json.loads(result.read_text())['descriptor'];root=state/descriptor['generation']
metrics=[]
for index in range(3):
    a=time.monotonic();verify_previous(root,descriptor);b=time.monotonic()
    actual=inventory(root,descriptor['manifest']['tables']);c=time.monotonic()
    assert actual==descriptor['manifest']['files']
    metrics.append(dict(verify_previous_seconds=b-a,inventory_seconds=c-b))
output.write_text(json.dumps(dict(scope='Read-only hot-cache hash microbenchmark on paused state; not end-to-end speedup',iterations=metrics,bytes_per_pass=sum(f['bytes'] for f in actual),file_count=len(actual),peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),indent=2)+'\n')
