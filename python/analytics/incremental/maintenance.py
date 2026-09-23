"""Copy selected rows into a new, independently recoverable storage generation."""
import copy
import hashlib
from pathlib import Path
import time
import pyarrow as pa
import pyarrow.fs as fs
from deltalake import DeltaTable, write_deltalake, WriterProperties
from capture.spool import CaptureError, atomic, canonical, fault
from .storage import boundary, inventory, read_json, verify_previous


def estimate_bytes(config):
    source=Path(config['previous_generation']);total=0
    verify_previous(source,config['previous'])
    for table in config['previous']['manifest']['tables']:
        path=source/table['path']
        delta=DeltaTable(str(path),version=table['version'])
        total+=sum(Path(p).stat().st_size for p in delta.file_uris())
    return total*2+4*1024*1024


def compact(config, temporary):
    previous=config['previous'];source=Path(config['previous_generation'])
    verify_previous(source,previous)
    tables=copy.deepcopy(previous['manifest']['tables'])
    started=time.monotonic();rows=0
    for table in tables:
        path=source/table['path']
        delta=DeltaTable(str(path),version=table['version'])
        dataset=delta.to_pyarrow_dataset(filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem()))
        count=[0]
        def batches():
            for batch in dataset.scanner(batch_size=256,batch_readahead=1,fragment_readahead=1,use_threads=False).to_batches():
                if batch.nbytes>32*1024*1024:raise CaptureError('compaction_value_budget')
                boundary(temporary,config['deadline_ms'],extra=4*batch.nbytes+4*1024*1024)
                count[0]+=batch.num_rows
                yield batch
        reader=pa.RecordBatchReader.from_batches(dataset.schema,batches())
        write_deltalake(str(temporary/table['path']),reader,
            target_file_size=16*1024*1024,
            writer_properties=WriterProperties(compression='UNCOMPRESSED',max_row_group_size=1024),
            configuration={'delta.dataSkippingNumIndexedCols':'0'})
        if count[0]!=table['rows']:raise CaptureError('compaction_row_count')
        rows+=count[0];table['version']=0
        boundary(temporary,config['deadline_ms'])
        fault('after_compaction_table')
    entries=inventory(temporary,tables)
    receipt=dict(source_sha256=hashlib.sha256(canonical(previous)).hexdigest(),tables=tables,files=entries,
        metrics=dict(rows=rows,source_files=len(previous['manifest']['files']),output_files=len(entries),
            source_bytes=sum(f['bytes'] for f in previous['manifest']['files']),
            output_bytes=sum(f['bytes'] for f in entries),elapsed_ms=int((time.monotonic()-started)*1000)))
    receipt['sha256']=hashlib.sha256(canonical(receipt)).hexdigest()
    atomic(temporary/'compaction.json',receipt)


def base(config,root):
    previous=config['previous']
    if not previous or not config.get('previous_generation') or Path(config['previous_generation'])==root:
        return previous,None
    receipt=read_json(root/'compaction.json')
    checksum=receipt.pop('sha256',None)
    if checksum!=hashlib.sha256(canonical(receipt)).hexdigest():raise CaptureError('compaction_receipt_corrupt')
    if receipt['source_sha256']!=hashlib.sha256(canonical(previous)).hexdigest():raise CaptureError('compaction_source_changed')
    result=copy.deepcopy(previous)
    result['manifest'].update(tables=receipt['tables'],files=receipt['files'])
    verify_previous(root,result)
    return result,receipt['metrics']
