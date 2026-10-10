#!/usr/bin/env python3
"""Daemon-owned SY03 batch applier. SQLite publication remains the sole authority."""
import copy
from contextlib import nullcontext
from decimal import Decimal
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.fs as fs
from deltalake import DeltaTable, CommitProperties, PostCommitHookProperties, WriterProperties, write_deltalake
from capture.spool import CaptureError, atomic, canonical, fault, pg_lsn
from incremental.rows import overlay, key_columns, row_key, key_values, value, MAX_VALUES
from incremental.storage import JournalBusyDeferred, read_json, initialize, journal, boundary, durable, verify_previous, inventory, retained_boundary, validate_storage_profile, verified_digests
from incremental.maintenance import base
from incremental.planning import mutation_lease, PlanningBoundary
from incremental.preparation import Preparation, DecodedBatch, decode, row_limit
from incremental.lookahead import optional_consume, complete, hint

def quote(name):return '"'+name.replace('"','""')+'"'


def schema_for(table,path):
    # Reuse the exact existing Arrow schema, including nullability and decimals.
    return table.to_pyarrow_dataset(filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem())).schema


def key_filter(columns,keys):
    pk=key_columns(columns)
    if not keys:return ds.scalar(False)
    # Bounded expression depth: an OR of thousands of exact tuples can crash
    # Arrow's expression optimizer. These are pruning candidates, not identity.
    parts=[key_values(key,pk) for key in keys]
    terms=[]
    for n,i in enumerate(pk):
        values=sorted({v[n] for v in parts});field=ds.field(columns[i][1])
        # Large membership predicates alone need not prune Parquet row groups.
        # These redundant bounds let statistics reject nonoverlapping ranges.
        terms.append(field.isin(values) & (field>=values[0]) & (field<=values[-1]))
    predicate=terms[0]
    for term in terms[1:]:predicate=predicate & term
    return predicate


def key_batches(dataset,columns,keys):
    pk=key_columns(columns)
    # Keep native read-ahead bounded even when statistics cannot prune a scan.
    for batch in dataset.scanner(filter=key_filter(columns,keys),batch_size=32,
            batch_readahead=1,fragment_readahead=1,use_threads=False).to_batches():
        if len(pk)>1:
            # Reject Cartesian neighbors before row/value budgets or overlay.
            # Materialize only the key vectors, and at most one 32-row batch.
            parts=[batch.column(columns[i][1]).to_pylist() for i in pk]
            batch=batch.filter(pa.array([tuple(v) in keys for v in zip(*parts)],type=pa.bool_()))
        yield batch


def plan(config,root,previous,journal_data=None,lease=None):
    with PlanningBoundary(root,config['deadline_ms'],lease,config.get('storage_profile','compact')) as guard:
        return plan_rows(config,root,previous,journal_data,guard)


def plan_rows(config,root,previous,journal_data,guard):
    if isinstance(journal_data,Preparation):
        decoded=journal_data.result()
        guard.check()
    elif isinstance(journal_data,DecodedBatch):
        decoded=journal_data
        guard.check()
    else:
        decoded=decode(config,journal(config) if journal_data is None else journal_data,guard.check)
    hint(config,decoded)
    schema=decoded.schema;operations=decoded.operations;end=decoded.end;input_bytes=decoded.input_bytes
    limit=row_limit(config)
    touched={}
    for oid,tag,old,new,row in operations:
        touched.setdefault(oid,set()).add(old)
        if new is not None:touched[oid].add(new)
    existing={};size=0
    for table in previous['manifest']['tables']:
        oid=str(table['oid'])
        if oid not in touched:continue
        profile=schema[oid];columns=profile[3];pk=key_columns(columns)
        path=root/table['path'];delta=DeltaTable(str(path),version=table['version'])
        dataset=delta.to_pyarrow_dataset(filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem()))
        rows={}
        for batch in key_batches(dataset,columns,touched[oid]):
            guard.check()
            size+=batch.nbytes
            if size>MAX_VALUES:raise CaptureError('apply_value_budget')
            for row in batch.to_pylist():
                values=[row[c[1]] for c in columns];key=row_key(values,pk)
                if key in rows:raise CaptureError('duplicate_source_key')
                rows[key]=values
                if len(rows)>limit:raise CaptureError('apply_row_budget')
        existing[oid]=rows
    final=overlay(operations,existing,schema)
    output=[]
    for table in previous['manifest']['tables']:
        oid=str(table['oid'])
        if oid not in final:continue
        # Dates and exact decimals use lossless JSON text only in the private plan;
        # typed Arrow values are restored before writing Delta.
        rows=[[key,None if row is None else [str(v) if isinstance(v,(Decimal,date)) else v for v in row]] for key,row in sorted(final[oid].items())]
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


def apply_table(config,root,table,planned,checksum,sealed):
    profile=config.get('storage_profile','compact')
    path=root/table['path'];delta=DeltaTable(str(path));before=planned['before']
    marker=dict(sb_run=config['id'],sb_plan=checksum)
    if delta.version()==before+1:
        record=delta.history(1)[0]
        if any(record.get(k)!=v for k,v in marker.items()):raise CaptureError('foreign_delta_commit')
        # A committed Delta log after SIGKILL may precede the worker receipt.
        durable(path,sealed)
        return delta.version(),commit_metrics(path,delta.version(),dict(record.get('operationMetrics',{}),replayed=True,apply_kind=record.get('sb_apply','merge')))
    if delta.version()!=before:raise CaptureError('foreign_delta_version')
    if before>=1023:raise CaptureError('delta_version_budget')
    columns=planned['columns'];pk=key_columns(columns)
    arrow=schema_for(delta,path);delete='__supabricks_delete'
    while delete in arrow.names:delete+='x'
    records=[]
    for key,values in planned['rows']:
        row={c[1]:None for c in columns} if values is None else {c[1]:value(v,c) if c[2] in (1700,1082) and v is not None else v for c,v in zip(columns,values)}
        for i,v in zip(pk,key_values(key,pk)):row[columns[i][1]]=v
        row[delete]=values is None;records.append(row)
    # Delete source rows may have NULL placeholders for non-key NOT NULL columns;
    # matched-delete removes them before any target insert/update.
    input_schema=pa.schema([pa.field(f.name,f.type,nullable=True,metadata=f.metadata) for f in arrow]+[pa.field(delete,pa.bool_())])
    source=pa.Table.from_pylist(records,schema=input_schema)
    if source.nbytes>MAX_VALUES:raise CaptureError('apply_value_budget')
    # Each planned key contributes at most +1 to row_delta. Equality proves
    # every final row is new: planning looked up the exact keys in this pinned
    # version before sealing the plan. Missing proof retains the merge path.
    # Avoid buffering the entire unchanged target in Delta's merge barrier.
    append=bool(records) and planned.get('row_delta')==len(records) and all(not row[delete] for row in records)
    # Appends create new files; they cannot rewrite the unchanged target.
    table_bytes=0 if append else sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
    reservation=table_bytes+4*source.nbytes+4*1024*1024
    retained_boundary(root.parent,extra=reservation,profile=profile)
    boundary(root,config['deadline_ms'],extra=reservation,profile=profile)
    marker['sb_apply']='append' if append else 'merge'
    options=dict(writer_properties=WriterProperties(compression='UNCOMPRESSED',max_row_group_size=1024),
        commit_properties=CommitProperties(custom_metadata=marker,max_commit_retries=0),
        post_commithook_properties=PostCommitHookProperties(create_checkpoint=False,cleanup_expired_logs=False))
    if append:
        # Restore the exact target nullability/metadata after the merge source's
        # nullable tombstone placeholders and private delete column are removed.
        write_deltalake(delta,source.select(arrow.names).cast(arrow),mode='append',**options)
        delta.update_incremental()
        metrics=dict(delta.history(1)[0].get('operationMetrics',{}))
    else:
        expressions={quote(c[1]):'source.'+quote(c[1]) for c in columns}
        d='source.'+quote(delete)
        predicate=' AND '.join('target.'+quote(columns[i][1])+' = source.'+quote(columns[i][1]) for i in pk)
        metrics=delta.merge(source,predicate,source_alias='source',target_alias='target',
            streamed_exec=False,max_spill_size=64*1024*1024,max_temp_directory_size=128*1024*1024,**options)\
            .when_matched_delete(predicate=d).when_matched_update(expressions,predicate='NOT '+d)\
            .when_not_matched_insert(expressions,predicate='NOT '+d).execute()
    metrics['apply_kind']=marker['sb_apply']
    fault('after_table_commit')
    durable(path,sealed);boundary(root,config['deadline_ms'],profile=profile)
    return delta.version(),commit_metrics(path,delta.version(),metrics)


def run(config, *, prepare_overlap=False):
    validate_storage_profile(config)
    os.umask(0o077)
    work=Path(config['workspace']);plan_path=work/'plan.json'
    journal_data=None
    if config['previous'] is not None and not plan_path.exists():
        # A deferred read must precede initialization too: initialize may compact
        # Delta tables into a new generation. Existing plans use crash replay and
        # never re-enter this retryable boundary after partial table mutation.
        journal_data=optional_consume(config)
        if journal_data is None:journal_data=journal(config)
        else:journal_data=complete(config,journal_data)
    # Only the existing authorized range may prepare. Bootstrap and sealed-plan
    # crash replay do not decode or fetch journal data. The same process's RSS
    # ceiling covers both stages; there is no executor queue or extra process.
    # Experimental only: EQ220 K regressed the installed late cohort. The
    # daemon/execute path keeps serial preparation until a later measured slice
    # justifies overlap. Tests can exercise the boundary without a product knob.
    context=(Preparation(config,journal_data) if prepare_overlap and journal_data is not None and not isinstance(journal_data,DecodedBatch)
             and config.get('storage_profile','compact')=='large' else nullcontext(journal_data))
    with context as prepared:
        root=initialize(config)
        with mutation_lease(root) as lease:
            return run_owned(config,root,prepared,lease)


def run_owned(config,root,journal_data,lease):
    with verified_digests(lease,config):
        return run_verified(config,root,journal_data,lease)


def run_verified(config,root,journal_data,lease):
    profile=validate_storage_profile(config)
    work=Path(config['workspace']);plan_path=work/'plan.json'
    previous,compaction=base(config,root)
    sealed=frozenset()
    if config['previous'] is None:
        baseline=read_json(config['bootstrap_manifest'])
        tables=copy.deepcopy(baseline['tables'])
        for table in tables:table['path']='tables/'+str(table['oid'])
        manifest=copy.deepcopy(baseline);manifest.update(format_version=2,id=config['id'],tables=tables,files=inventory(root,tables))
        end=config['bootstrap_lsn'];metrics=[];input_bytes=0
    else:
        verify_previous(root,previous)
        # Only a published prefix of this exact storage generation is proof of
        # durability. A compacted root must flush its independently created files.
        if compaction is None:
            sealed=frozenset(root/entry['path'] for entry in previous['manifest']['files'])
        if plan_path.exists():
            prepared=read_json(plan_path,64*1024*1024)
            if any(prepared[k]!=config[k] for k in ('identity','after_lsn','target_lsn')) or prepared['run_id']!=config['id'] or prepared['previous_epoch']!=previous['epoch_id']:raise CaptureError('apply_plan_identity')
        else:
            prepared=plan(config,root,previous,journal_data,lease);atomic(plan_path,prepared)
        fault('after_apply_plan')
        checksum=hashlib.sha256(canonical(prepared)).hexdigest()
        tables=copy.deepcopy(previous['manifest']['tables']);metrics=[]
        for table in tables:
            selected=next((t for t in prepared['tables'] if t['oid']==str(table['oid'])),None)
            if selected:
                lease.check()
                table['version'],metric=apply_table(config,root,table,selected,checksum,sealed)
                metrics.append(dict(oid=table['oid'],metrics=metric))
                table['rows']+=selected['row_delta']
                if table['version']>=1024:raise CaptureError('delta_version_budget')
                fault('after_first_table')
        end=prepared['end_lsn'];input_bytes=prepared['input_bytes']
        manifest=copy.deepcopy(previous['manifest']);manifest.update(id=config['id'],tables=tables,files=inventory(root,tables))
    manifest['source']['lsn']=end;manifest['observed_at_ms']=int(time.time()*1000)
    manifest.pop('preparation',None)
    if config.get('_preparation') is not None:manifest['preparation']=config['_preparation']
    manifest['capture_identity']=config['identity'];manifest['input_bytes']=input_bytes;manifest['apply_metrics']=metrics
    manifest['storage_generation']=config.get('storage_generation')
    manifest['compaction']=compaction
    if profile!='compact':manifest['storage_profile']=profile
    manifest['retained_bytes']=retained_boundary(root.parent,profile=profile)
    used=boundary(root,config['deadline_ms'],profile=profile);manifest['generation_bytes']=used
    append_only=all(m['metrics'].get('apply_kind')=='append' for m in metrics)
    if config['previous'] is None or compaction is not None:
        # Keep this first-published size across later epochs in the same root.
        manifest['generation_base_bytes']=used
        manifest['generation_append_only']=append_only
    else:
        # An absent legacy marker is conservative; a merge cannot be forgotten
        # merely because a later batch appends. Compaction starts a new history.
        manifest['generation_append_only']=manifest.get('generation_append_only') is True and append_only
    descriptor=dict(format_version=2,installation_id=config['identity']['installation_id'],epoch_id=config['epoch_id'],
        ordinal=config['ordinal'],export_id=config['id'],source_revision=config['source_revision'],
        generation='analytics/incremental/'+(config.get('storage_generation') or config['identity']['generation']),manifest=manifest,
        manifest_sha256=hashlib.sha256(canonical(manifest)).hexdigest(),prepared_at_ms=int(time.time()*1000))
    if len(canonical(descriptor))>2*1024*1024:raise CaptureError('epoch_metadata_budget')
    durable(root,sealed)
    fault('before_epoch_receipt')
    lease.check()
    atomic(work/'result.json',dict(state='ready',id=config['id'],worker_generation=config['worker_generation'],journal_read=config.get('_journal_read'),preparation=config.get('_preparation'),descriptor=descriptor))
    fault('after_epoch_receipt')


def execute(config):
    try:run(config)
    except JournalBusyDeferred:
        atomic(Path(config['workspace'])/'result.json',dict(state='deferred',error='journal_read_busy_deferred',
            phase='journal_before_initialize',id=config['id'],worker_generation=config['worker_generation'],
            attempt=config['attempt'],identity=config['identity'],bootstrap_lsn=config['bootstrap_lsn'],
            after_lsn=config['after_lsn'],target_lsn=config['target_lsn'],journal_read=config['_journal_read']))
        return 2
    except Exception as error:
        code=error.code if isinstance(error,CaptureError) else 'incremental_worker_failed'
        atomic(Path(config['workspace'])/'result.json',dict(state='failed',id=config['id'],worker_generation=config['worker_generation'],error=code,journal_read=config.get('_journal_read')))
        return 1
    return 0


if __name__=='__main__':
    config=read_json(sys.argv[1],4*1024*1024)
    if config.get('reuse_worker',False):
        from incremental.reuse import serve
        del config
        serve(sys.argv[1],execute)
    else:
        sys.exit(execute(config))
