"""Run inside the shipped analytical Python; only consumes synthetic SY00 fixture data."""
import json
from decimal import Decimal
from pathlib import Path
import os
import sys
import time

import pyarrow as pa
import pyarrow.fs as fs
from deltalake import DeltaTable, write_deltalake

SCHEMA=pa.schema([('id',pa.int32()),('amount',pa.decimal128(38,8)),('note',pa.string())])


def arrow(rows):
    return pa.Table.from_pylist([dict(id=int(row[0]),amount=Decimal(row[1]) if row[1] is not None else None,
                                    note=row[2]) for row in rows],schema=SCHEMA)


def read(root,version):
    table=DeltaTable(str(root),version=version).to_pyarrow_table(
        filesystem=fs.SubTreeFileSystem(str(root),fs.LocalFileSystem()))
    return sorted([[str(r['id']),str(r['amount']) if r['amount'] is not None else None,r['note']]
                   for r in table.to_pylist()],key=lambda r:int(r[0]))


def atomic(path,value):
    temp=path.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    temp.replace(path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def main():
    fixture=json.loads(Path(sys.argv[1]).read_text());root=Path(sys.argv[2]);root.mkdir()
    metrics={'status':'FAIL','tables':[], 'publication':'versioned Delta roots with atomic epoch map; prototype only'}
    initial={}
    for table in fixture['tables']:
        location=root/table['name']
        started=time.monotonic()
        write_deltalake(str(location),arrow(table['before']),configuration={'delta.dataSkippingNumIndexedCols':'0'})
        initial[table['name']]=0
        metrics['tables'].append({'name':table['name'],'bootstrap_ms':round((time.monotonic()-started)*1000,3)})
    atomic(root/'epoch.json',initial)
    versions={}
    for table,measurement in zip(fixture['tables'],metrics['tables']):
        location=root/table['name'];before={r[0]:r for r in table['before']};after={r[0]:r for r in table['after']}
        changed=[r for key,r in after.items() if before.get(key)!=r]
        deleted=[int(key) for key in before.keys()-after.keys()]
        original_files={str(p):p.stat().st_size for p in location.rglob('*.parquet')}
        started=time.monotonic()
        delta=DeltaTable(str(location))
        if changed:
            delta.merge(arrow(changed),'target.id = source.id',source_alias='source',target_alias='target')\
                .when_matched_update_all().when_not_matched_insert_all().execute()
        if deleted:
            delta.delete('id IN ('+','.join(map(str,deleted))+')')
        versions[table['name']]=delta.version()
        measurement.update(apply_ms=round((time.monotonic()-started)*1000,3),changed_rows=len(changed),deleted_rows=len(deleted),
            before_bytes=sum(original_files.values()),new_parquet_bytes=sum(p.stat().st_size for p in location.rglob('*.parquet') if str(p) not in original_files))
        assert read(location,versions[table['name']])==sorted(table['after'],key=lambda r:int(r[0]))
        # A partially written new group is not visible through the old map.
        persisted=json.loads((root/'epoch.json').read_text())
        assert persisted==initial
        for old in fixture['tables']:
            assert read(root/old['name'],persisted[old['name']])==sorted(old['before'],key=lambda r:int(r[0]))
    atomic(root/'epoch.json',versions)
    for table in fixture['tables']:
        assert read(root/table['name'],versions[table['name']])==sorted(table['after'],key=lambda r:int(r[0]))
        assert read(root/table['name'],0)==sorted(table['before'],key=lambda r:int(r[0]))
    metrics.update(status='PASS',initial_versions=initial,published_versions=versions)
    print(json.dumps(metrics))


if __name__=='__main__':main()
