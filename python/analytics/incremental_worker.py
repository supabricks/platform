#!/usr/bin/env python3
"""Daemon-owned SY03 batch applier. SQLite publication remains the sole authority."""
import copy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.fs as fs
from deltalake import DeltaTable, CommitProperties, PostCommitHookProperties, WriterProperties
from capture.spool import CaptureError, atomic, canonical, fault, pg_lsn
from incremental.rows import changes, overlay, MAX_ROWS, MAX_VALUES
from incremental.storage import read_json, initialize, journal, boundary, durable, verify_previous, inventory


def quote(name):return '"'+name.replace('"','""')+'"'


def schema_for(table,path):
    # Reuse the exact existing Arrow schema, including nullability and decimals.
    return table.to_pyarrow_dataset(filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem())).schema


def plan(config,root,previous):
    schema,transactions,end,input_bytes=journal(config)
    operations=[]
    for end,payload in transactions:
        operations.extend(changes(payload,schema,end))
        if len(operations)>MAX_ROWS:raise CaptureError('apply_row_budget')
    touched={}
    for oid,tag,old,new,row in operations:
        touched.setdefault(oid,set()).add(old)
        if new is not None:touched[oid].add(new)
    existing={};size=0
    for table in previous['manifest']['tables']:
        oid=str(table['oid'])
        if oid not in touched:continue
        profile=schema[oid];columns=profile[3];pk=next(c[1] for c in columns if c[0]==1)
        path=root/table['path'];delta=DeltaTable(str(path),version=table['version'])
        dataset=delta.to_pyarrow_dataset(filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem()))
        rows={}
        for batch in dataset.scanner(filter=ds.field(pk).isin(sorted(touched[oid])),batch_size=32).to_batches():
            boundary(root,config['deadline_ms'])
            size+=batch.nbytes
            if size>MAX_VALUES:raise CaptureError('apply_value_budget')
            for row in batch.to_pylist():
                key=row[pk]
                if key in rows:raise CaptureError('duplicate_source_key')
                rows[key]=[row[c[1]] for c in columns]
                if len(rows)>MAX_ROWS:raise CaptureError('apply_row_budget')
        existing[oid]=rows
    final=overlay(operations,existing,schema)
    output=[]
    for table in previous['manifest']['tables']:
        oid=str(table['oid'])
        if oid not in final:continue
        # Decimal is serialized exactly as text, then restored under the pinned schema.
        rows=[[key,None if row is None else [str(v) if isinstance(v,Decimal) else v for v in row]] for key,row in sorted(final[oid].items())]
        output.append(dict(oid=oid,before=table['version'],rows=rows,columns=schema[oid][3],row_delta=sum(int(row is not None)-int(existing[oid].get(key) is not None) for key,row in final[oid].items())))
    result=dict(run_id=config['id'],identity=config['identity'],previous_epoch=previous['epoch_id'],
                after_lsn=config['after_lsn'],target_lsn=config['target_lsn'],end_lsn=pg_lsn(end),input_bytes=input_bytes,tables=output)
    if len(canonical(result))>64*1024*1024:raise CaptureError('apply_plan_budget')
    return result


def commit_metrics(path,version,metrics):
    # Report physical amplification, including on replay; operation metrics alone
    # expose row/file counts but omit the bytes written by this Delta commit.
    added=0
    with (path/'_delta_log'/f'{version:020}.json').open() as stream:
        while line:=stream.readline(2*1024*1024+1):
            if len(line)>2*1024*1024:raise CaptureError('delta_metadata_budget')
            added+=json.loads(line).get('add',{}).get('size',0)
    return dict(metrics,new_parquet_bytes=added,
        retained_parquet_bytes=sum(p.stat().st_size for p in path.glob('*.parquet')))


def apply_table(config,root,table,planned,checksum):
    path=root/table['path'];delta=DeltaTable(str(path));before=planned['before']
    marker=dict(sb_run=config['id'],sb_plan=checksum)
    if delta.version()==before+1:
        record=delta.history(1)[0]
        if any(record.get(k)!=v for k,v in marker.items()):raise CaptureError('foreign_delta_commit')
        # A committed Delta log after SIGKILL may precede the worker receipt.
        durable(path)
        return delta.version(),commit_metrics(path,delta.version(),dict(record.get('operationMetrics',{}),replayed=True))
    if delta.version()!=before:raise CaptureError('foreign_delta_version')
    if before>=1023:raise CaptureError('delta_version_budget')
    columns=planned['columns'];pk=next(c[1] for c in columns if c[0]==1)
    arrow=schema_for(delta,path);delete='__supabricks_delete'
    while delete in arrow.names:delete+='x'
    records=[]
    for key,values in planned['rows']:
        row={c[1]:None for c in columns} if values is None else {c[1]:Decimal(v) if c[2]==1700 and v is not None else v for c,v in zip(columns,values)}
        row[pk]=key;row[delete]=values is None;records.append(row)
    # Delete source rows may have NULL placeholders for non-key NOT NULL columns;
    # matched-delete removes them before any target insert/update.
    input_schema=pa.schema([pa.field(f.name,f.type,nullable=True) for f in arrow]+[pa.field(delete,pa.bool_())])
    source=pa.Table.from_pylist(records,schema=input_schema)
    if source.nbytes>MAX_VALUES:raise CaptureError('apply_value_budget')
    table_bytes=sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
    boundary(root,config['deadline_ms'],extra=table_bytes+4*source.nbytes+4*1024*1024)
    expressions={quote(c[1]):'source.'+quote(c[1]) for c in columns}
    d='source.'+quote(delete)
    metrics=delta.merge(source,'target.'+quote(pk)+' = source.'+quote(pk),source_alias='source',target_alias='target',
        streamed_exec=False,max_spill_size=64*1024*1024,max_temp_directory_size=128*1024*1024,
        writer_properties=WriterProperties(compression='UNCOMPRESSED',max_row_group_size=1024),
        commit_properties=CommitProperties(custom_metadata=marker,max_commit_retries=0),
        post_commithook_properties=PostCommitHookProperties(create_checkpoint=False,cleanup_expired_logs=False))\
        .when_matched_delete(predicate=d).when_matched_update(expressions,predicate='NOT '+d)\
        .when_not_matched_insert(expressions,predicate='NOT '+d).execute()
    fault('after_table_commit')
    durable(path);boundary(root,config['deadline_ms'])
    return delta.version(),commit_metrics(path,delta.version(),metrics)


def run(config):
    os.umask(0o077)
    root=initialize(config);work=Path(config['workspace'])
    if config['previous'] is None:
        baseline=read_json(config['bootstrap_manifest'])
        tables=copy.deepcopy(baseline['tables'])
        for table in tables:table['path']='tables/'+str(table['oid'])
        manifest=copy.deepcopy(baseline);manifest.update(format_version=2,id=config['id'],tables=tables,files=inventory(root,tables))
        end=config['bootstrap_lsn'];metrics=[];input_bytes=0
    else:
        previous=config['previous'];verify_previous(root,previous)
        plan_path=work/'plan.json'
        if plan_path.exists():
            prepared=read_json(plan_path,64*1024*1024)
            if any(prepared[k]!=config[k] for k in ('identity','after_lsn','target_lsn')) or prepared['run_id']!=config['id'] or prepared['previous_epoch']!=previous['epoch_id']:raise CaptureError('apply_plan_identity')
        else:
            prepared=plan(config,root,previous);atomic(plan_path,prepared)
        fault('after_apply_plan')
        checksum=hashlib.sha256(canonical(prepared)).hexdigest()
        tables=copy.deepcopy(previous['manifest']['tables']);metrics=[]
        for table in tables:
            selected=next((t for t in prepared['tables'] if t['oid']==str(table['oid'])),None)
            if selected:
                table['version'],metric=apply_table(config,root,table,selected,checksum)
                metrics.append(dict(oid=table['oid'],metrics=metric))
                table['rows']+=selected['row_delta']
                if table['version']>=1024:raise CaptureError('delta_version_budget')
                fault('after_first_table')
        end=prepared['end_lsn'];input_bytes=prepared['input_bytes']
        manifest=copy.deepcopy(previous['manifest']);manifest.update(id=config['id'],tables=tables,files=inventory(root,tables))
    manifest['source']['lsn']=end;manifest['observed_at_ms']=int(time.time()*1000)
    manifest['capture_identity']=config['identity'];manifest['input_bytes']=input_bytes;manifest['apply_metrics']=metrics
    used=boundary(root,config['deadline_ms']);manifest['generation_bytes']=used
    descriptor=dict(format_version=2,installation_id=config['identity']['installation_id'],epoch_id=config['epoch_id'],
        ordinal=config['ordinal'],export_id=config['id'],source_revision=config['source_revision'],
        generation='analytics/incremental/'+config['identity']['generation'],manifest=manifest,
        manifest_sha256=hashlib.sha256(canonical(manifest)).hexdigest(),prepared_at_ms=int(time.time()*1000))
    if len(canonical(descriptor))>2*1024*1024:raise CaptureError('epoch_metadata_budget')
    durable(root)
    fault('before_epoch_receipt')
    atomic(work/'result.json',dict(state='ready',id=config['id'],worker_generation=config['worker_generation'],descriptor=descriptor))
    fault('after_epoch_receipt')


if __name__=='__main__':
    config=read_json(sys.argv[1],4*1024*1024)
    try:run(config)
    except Exception as error:
        code=error.code if isinstance(error,CaptureError) else 'incremental_worker_failed'
        atomic(Path(config['workspace'])/'result.json',dict(state='failed',id=config['id'],worker_generation=config['worker_generation'],error=code))
        sys.exit(1)
