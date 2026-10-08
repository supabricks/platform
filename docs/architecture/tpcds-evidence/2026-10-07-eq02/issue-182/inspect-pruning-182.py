import json,sys
from pathlib import Path
sys.path.insert(0,'/repo/build/eq02-20261007/row-prefix-runtime/python/analytics')
import incremental_worker as w
# The successful baseline diagnostic retains the exact planned insert keys in its new file.
d=json.load(open('/evidence/load-04/last-publication.json'))['descriptor']
t=next(t for t in d['manifest']['tables'] if t['name']=='customer_demographics')
path=Path('/state')/d['generation']/t['path']
delta=w.DeltaTable(str(path),version=t['version'])
dataset=delta.to_pyarrow_dataset(filesystem=w.fs.SubTreeFileSystem(str(path),w.fs.LocalFileSystem()))
# Actual key range and count are checked against the diagnostic's committed source rows.
result=w.DeltaTable('/diag/analytics/incremental/generation/'+t['path'])
newfiles=set(result.file_uris())-set(w.DeltaTable('/diag/analytics/incremental/generation/'+t['path'],version=t['version']).file_uris())
new=w.ds.dataset(sorted(newfiles),format='parquet').to_table()
column=next(c for c in t['columns'] if c['name']=='cd_demo_sk')
keys=set(new.column('cd_demo_sk').to_pylist())
columns=[[1,'cd_demo_sk',23,-1]]
base=w.key_filter(columns,keys);field=w.ds.field('cd_demo_sk');ranged=base & (field>=min(keys)) & (field<=max(keys))
counts={}
for name,predicate in [('none',None),('isin',base),('range',ranged)]:
 fragments=list(dataset.get_fragments(filter=predicate))
 groups=[group for fragment in fragments for group in fragment.split_by_row_group(filter=predicate)]
 counts[name]=dict(fragments=len(fragments),row_groups=len(groups),rows=sum(rg.num_rows for group in groups for rg in group.row_groups))
print(json.dumps(dict(table=t['name'],version=t['version'],key_count=len(keys),min_key=min(keys),max_key=max(keys),pruning=counts),indent=2))
