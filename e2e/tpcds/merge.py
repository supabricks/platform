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
}


def child(python,root,phase):
    subprocess.run([str(python),str(Path(__file__).resolve()),'--root',str(root),'--phase',phase],check=True,timeout=90)


class MergeChecks:
    def __init__(self,release,root):
        self.release=release;self.root=root;self.binary=release/'bin/supabricks';self.checks=[];self.metrics={}
    def check(self,name):
        self.checks.append(name);print('PASS',name,flush=True)
    def run(self,python,export):
        for phase in ('generate','insert','mutate','replay','verify'):child(python,self.root,phase)
        self.metrics.update(json.loads((self.root/'measurements.json').read_text()))
        self.metrics['scope']='Direct installed apply_table regression; synthetic compacted table; not end-to-end throughput'
        for name in sorted(CHECKS):self.check(name)


def run(root,phase):
    import incremental_worker as w
    import pyarrow as pa
    import pyarrow.compute as pc
    from deltalake import write_deltalake
    root=Path(root);generation=root/'generations/current';path=generation/'table'
    schema=pa.schema([pa.field('id',pa.int32(),False)]+[pa.field('v'+str(i),pa.int32()) for i in range(1,9)])
    columns=[[1,'id',23,-1]]+[[0,'v'+str(i),23,-1] for i in range(1,9)]
    def batch(lo,hi):
        keys=pa.array(range(lo,hi),type=pa.int32())
        return pa.RecordBatch.from_arrays([keys]+[pc.add(keys,pa.scalar(i,pa.int32())) for i in range(1,9)],schema=schema)
    if phase=='generate':
        generation.mkdir(parents=True)
        reader=pa.RecordBatchReader.from_batches(schema,(batch(lo,min(lo+16384,ROWS+1)) for lo in range(1,ROWS+1,16384)))
        write_deltalake(str(path),reader,configuration={'delta.dataSkippingNumIndexedCols':'0'},target_file_size=32*1024**2,
                       writer_properties=w.WriterProperties(compression='UNCOMPRESSED',max_row_group_size=1024))
        assert w.DeltaTable(str(path)).version()==0
        (root/'measurements.json').write_text(json.dumps(dict(rows=ROWS,insert_rows=INSERTS,initial_parquet_bytes=sum(p.stat().st_size for p in path.glob('*.parquet')))))
        return
    if phase=='verify':
        for version in (0,1,2):
            maximum=ROWS+(INSERTS if version else 0)+(1 if version==2 else 0);seen=bytearray(maximum+1);count=0
            dataset=w.DeltaTable(str(path),version=version).to_pyarrow_dataset(filesystem=w.fs.SubTreeFileSystem(str(path),w.fs.LocalFileSystem()))
            for record in dataset.scanner(batch_size=16384,batch_readahead=0,fragment_readahead=0,use_threads=False).to_batches():
                keys=record.column('id');values=keys.to_pylist()
                for key in values:
                    assert 1<=key<=maximum and not seen[key],('duplicate/unexpected',version,key)
                    assert version!=2 or key not in (2,3),('deleted',key)
                    seen[key]=1;count+=1
                for i in range(1,9):
                    expected=pc.add(keys,pa.scalar(i,pa.int32()))
                    if version==2:
                        expected=pc.if_else(pc.equal(keys,1),pa.scalar(-i,pa.int32()),expected)
                        expected=pc.if_else(pc.equal(keys,ROWS),pa.scalar(-2*i,pa.int32()),expected)
                        expected=pc.if_else(pc.equal(keys,ROWS+INSERTS+1),pa.scalar(3+i,pa.int32()),expected)
                    assert record.column('v'+str(i)).equals(expected),(version,i)
            assert count==maximum-(2 if version==2 else 0),(version,count)
        return
    rows=([[key,[key]+[key+i for i in range(1,9)]] for key in range(ROWS+1,ROWS+INSERTS+1)] if phase=='insert' else
          [[1,[1]+[-i for i in range(1,9)]],[ROWS,[ROWS]+[-2*i for i in range(1,9)]],[2,None],[3,None],
           [ROWS+INSERTS+1,[ROWS+INSERTS+1]+[3+i for i in range(1,9)]]])
    planned=dict(before=0 if phase=='insert' else 1,columns=columns,rows=rows)
    saved=root/'mutation-plan.json'
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
    report=json.loads((root/'measurements.json').read_text())
    report[phase]=dict(elapsed_seconds=time.monotonic()-started,highwater_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),metrics=metrics)
    (root/'measurements.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--phase',choices=['generate','insert','mutate','replay','verify'],required=True)
    args=parser.parse_args();os.umask(0o077)
    # The child interpreter belongs to the installed archive, not a source venv.
    sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
    run(args.root,args.phase)
