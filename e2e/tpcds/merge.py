"""Installed bounded Delta merge regression; synthetic compacted storage, no PG throughput claim."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

ROWS=1_888_080
INSERTS=16_384
CHECKS={
    'bounded_merge_into_large_compacted_table_completes',
    'sparse_update_delete_key_move_and_saved_commit_replay_are_exact',
    'old_delta_versions_remain_exact_after_bounded_merges',
    'bounded_merge_many_small_files_keeps_source_hash_build',
}


def child(python,root,phase):
    subprocess.run([str(python),str(Path(__file__).resolve()),'--root',str(root),'--phase',phase],check=True,timeout=90)


class MergeChecks:
    def __init__(self,release,root):
        self.release=release;self.root=root;self.binary=release/'bin/supabricks';self.checks=[];self.metrics={}
    def check(self,name):
        self.checks.append(name);print('PASS',name,flush=True)
    def run(self,python,export):
        for layout in ('compacted','fragmented'):
            for phase in ('generate','insert','mutate','replay','verify'):child(python,self.root,layout+'-'+phase)
            self.metrics[layout]=json.loads((self.root/(layout+'-measurements.json')).read_text())
        self.metrics['scope']='Direct installed apply_table regression; synthetic compacted table; not end-to-end throughput'
        for name in sorted(CHECKS):self.check(name)


def run(root,phase):
    import incremental_worker as w
    import pyarrow as pa
    import pyarrow.compute as pc
    from deltalake import write_deltalake
    layout,phase=phase.split('-',1);row_count=ROWS if layout=='compacted' else 131072
    root=Path(root);generation=root/'generations'/layout;path=generation/'table'
    report_path=root/(layout+'-measurements.json')
    # Match the mixed-width customer_demographics layout that triggered #184:
    # four padded CHAR columns expand in Arrow even when Parquet is compact.
    chars={1:'M',2:'M',3:'College             ',5:'Good      '}
    schema=pa.schema([pa.field('id',pa.int32(),False)]+[
        pa.field('v'+str(i),pa.string() if i in chars else pa.int32()) for i in range(1,9)])
    columns=[[1,'id',23,-1]]+[[0,'v'+str(i),1042 if i in chars else 23,len(chars[i])+4 if i in chars else -1] for i in range(1,9)]
    def value(i,mutation=0):
        if i in chars:return chars[i] if not mutation else ('X' if mutation==1 else 'Y').ljust(len(chars[i]))
        return i if not mutation else -mutation*i
    def values(key,mutation=0):return [key]+[value(i,mutation) for i in range(1,9)]
    def batch(lo,hi):
        keys=pa.array(range(lo,hi),type=pa.int32())
        return pa.RecordBatch.from_arrays([keys]+[pa.repeat(pa.scalar(value(i),schema.field('v'+str(i)).type),len(keys)) for i in range(1,9)],schema=schema)
    if phase=='generate':
        generation.mkdir(parents=True)
        options=dict(configuration={'delta.dataSkippingNumIndexedCols':'0'},target_file_size=1024**3,
                     writer_properties=w.WriterProperties(compression='UNCOMPRESSED',max_row_group_size=1024))
        if layout=='compacted':
            reader=pa.RecordBatchReader.from_batches(schema,(batch(lo,min(lo+16384,row_count+1)) for lo in range(1,row_count+1,16384)))
            write_deltalake(str(path),reader,**options)
            assert len(list(path.glob('*.parquet')))==1,'single large compacted file required'
        else:
            # One transaction writes eight small files, reproducing the misleading
            # byte-size estimate without introducing unrelated Delta versions.
            options['target_file_size']=64*1024
            reader=pa.RecordBatchReader.from_batches(schema,(batch(lo,lo+16384) for lo in range(1,row_count+1,16384)))
            write_deltalake(str(path),reader,**options)
            assert len(list(path.glob('*.parquet')))>=8,'many small files required'
        assert w.DeltaTable(str(path)).version()==0
        report_path.write_text(json.dumps(dict(rows=row_count,insert_rows=INSERTS,initial_parquet_bytes=sum(p.stat().st_size for p in path.glob('*.parquet')))))
        return
    if phase=='verify':
        for version in (0,1,2):
            maximum=row_count+(INSERTS if version else 0)+(1 if version==2 else 0);seen=bytearray(maximum+1);count=0
            dataset=w.DeltaTable(str(path),version=version).to_pyarrow_dataset(filesystem=w.fs.SubTreeFileSystem(str(path),w.fs.LocalFileSystem()))
            for record in dataset.scanner(batch_size=16384,batch_readahead=0,fragment_readahead=0,use_threads=False).to_batches():
                keys=record.column('id');values=keys.to_pylist()
                for key in values:
                    assert 1<=key<=maximum and not seen[key],('duplicate/unexpected',version,key)
                    assert version!=2 or key not in (2,3),('deleted',key)
                    seen[key]=1;count+=1
                for i in range(1,9):
                    typ=schema.field('v'+str(i)).type
                    expected=pa.repeat(pa.scalar(value(i),typ),len(keys))
                    if version==2:
                        expected=pc.if_else(pc.equal(keys,1),pa.scalar(value(i,1),typ),expected)
                        expected=pc.if_else(pc.equal(keys,row_count),pa.scalar(value(i,2),typ),expected)
                    assert record.column('v'+str(i)).equals(expected),(version,i)
            assert count==maximum-(2 if version==2 else 0),(version,count)
        return
    rows=([[key,values(key)] for key in range(row_count+1,row_count+INSERTS+1)] if phase=='insert' else
          [[1,values(1,1)],[row_count,values(row_count,2)],[2,None],[3,None],
           [row_count+INSERTS+1,values(row_count+INSERTS+1)]])
    planned=dict(before=0 if phase=='insert' else 1,columns=columns,rows=rows)
    saved=root/(layout+'-mutation-plan.json')
    if phase=='mutate':saved.write_bytes(w.canonical(planned))
    if phase=='replay':planned=json.loads(saved.read_text())
    checksum=hashlib.sha256(w.canonical(planned)).hexdigest()
    config=dict(id='bounded-merge-insert' if phase=='insert' else 'bounded-merge-mutate',deadline_ms=int(time.time()*1000)+60000)
    started=time.monotonic()
    if phase=='mutate':
        def crash(point):
            if point=='after_table_commit':raise SystemExit(86)
        w.fault=crash
        try:w.apply_table(config,generation,dict(path='table'),planned,checksum,frozenset())
        except SystemExit as error:assert error.code==86
        else:raise AssertionError('commit fault not reached')
        assert w.DeltaTable(str(path)).version()==2
        metrics=dict(committed_before_receipt=True)
    else:
        version,metrics=w.apply_table(config,generation,dict(path='table'),planned,checksum,frozenset())
        assert version==(1 if phase=='insert' else 2)
        if phase=='replay':assert metrics['replayed']
    report=json.loads(report_path.read_text())
    report[phase]=dict(elapsed_seconds=time.monotonic()-started,highwater_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),metrics=metrics)
    report_path.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--phase',choices=[layout+'-'+phase for layout in ('compacted','fragmented') for phase in ('generate','insert','mutate','replay','verify')],required=True)
    args=parser.parse_args();os.umask(0o077)
    # The child interpreter belongs to the installed archive, not a source venv.
    sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
    run(args.root,args.phase)
