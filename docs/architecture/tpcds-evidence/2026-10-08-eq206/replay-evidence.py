"""Replay strict and numerical checks using committed rows; no live stack needed."""
import argparse,gzip,json,sys
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--repo',type=Path,default=Path.cwd())
p.add_argument('--output',type=Path,required=True)
a=p.parse_args();repo=a.repo.resolve();out=a.output.resolve()
if out.exists():raise ValueError('fresh replay output directory required')
evidence=repo/'docs/architecture/tpcds-evidence/2026-10-08-eq206'
reference=repo/'docs/architecture/tpcds-evidence/2026-10-07-eq02/reference-02'
sys.path.insert(0,str(repo/'e2e/tpcds'))
from q39_review import run
for label,source in [('reference',reference),('baseline',evidence/'baseline/product'),('candidate',evidence/'candidate/product')]:
 dest=out/label;dest.mkdir(parents=True);(dest/'queries').mkdir()
 (dest/'result.json').write_bytes((source/'result.json').read_bytes())
 for path in (source/'queries').glob('*.rows.jsonl.gz'):
  (dest/'queries'/path.stem).write_bytes(gzip.decompress(path.read_bytes()))
for mode in ['baseline','candidate']:
 result=run(out/mode,out/'reference',evidence/'diagnostics/all-groups.json.gz',evidence/mode/'comparison.json',out/(mode+'-review'))
 print(mode,result['status'])
 # Live and archive replay must produce exactly the same review, including hashes.
 assert json.loads(json.dumps(result))==json.loads((evidence/mode/'numerical-review/review.json').read_text())
print('Archive replay matches both retained numerical reviews exactly')
