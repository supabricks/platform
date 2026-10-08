import collections,decimal,hashlib,itertools,json,math,struct,time
from pathlib import Path
import pyarrow.dataset as ds
from deltalake import DeltaTable
root=Path('/load');d=json.loads((root/'result.json').read_text())['publication']['descriptor'];tables={t['name']:t for t in d['manifest']['tables']}
def table(name):
 t=tables[name];return DeltaTable(str(root/'state'/d['generation']/t['path']),version=int(t['version'])).to_pyarrow_dataset()
rows=[json.loads(l) for l in Path('/reference/queries/q39a.rows.jsonl').read_text().splitlines()]
keys={tuple(map(int,[r[0],r[1],r[2]])) for r in rows}|{tuple(map(int,[r[5],r[6],r[7]])) for r in rows}
dates=table('date_dim').to_table(columns=['d_date_sk','d_moy'],filter=(ds.field('d_year')==2001)&ds.field('d_moy').isin([1,2])).to_pylist();months={r['d_date_sk']:r['d_moy'] for r in dates}
assert len(months)==len(dates), 'date keys must be unique before the inventory join'
groups=collections.defaultdict(list)
for batch in table('inventory').to_batches(columns=['inv_date_sk','inv_item_sk','inv_warehouse_sk','inv_quantity_on_hand'],filter=ds.field('inv_date_sk').isin(list(months))):
 for r in batch.to_pylist():
  k=(r['inv_warehouse_sk'],r['inv_item_sk'],months[r['inv_date_sk']])
  groups[k].append([r['inv_date_sk'],r['inv_quantity_on_hand']])
assert keys <= set(groups)
items=table('item').to_table(columns=['i_item_sk']).column(0).to_pylist();warehouses=table('warehouse').to_table(columns=['w_warehouse_sk']).column(0).to_pylist()
assert len(set(items))==len(items) and len(set(warehouses))==len(warehouses)
assert {k[0] for k in groups} <= set(warehouses) and {k[1] for k in groups} <= set(items)
out=dict(load_receipt_sha256=hashlib.sha256((root/'result.json').read_bytes()).hexdigest(),dimension_key_counts=dict(item=len(items),warehouse=len(warehouses),date_window=len(dates)),epoch_id=json.loads((root/'result.json').read_text())['publication']['epoch_id'] if 'epoch_id' in json.loads((root/'result.json').read_text())['publication'] else None,fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),groups=[dict(key=k,rows=sorted(v)) for k,v in sorted(groups.items())])
Path('/reports/all-groups.json').write_text(json.dumps(out,indent=2)+'\n');print('groups',len(groups),'rows',sum(map(len,groups.values())))
