"""Installed new-key append, memory bound and crash replay on a large target."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import struct
import subprocess
import sys
import time

ROWS=16_777_216
INSERTS=16_384
CHECKS={'proven_new_keys_append_without_target_sized_memory',
        'append_commit_replay_and_old_versions_are_exact'}


class AppendChecks:
    def __init__(self,release,root):
        self.release=release;self.root=root;self.binary=release/'bin/supabricks';self.checks=[];self.metrics={}
    def run(self,python,export):
        for phase in ('generate','append','replay','verify'):
            subprocess.run([str(python),str(Path(__file__).resolve()),'--root',str(self.root),
                            '--phase',phase],check=True,timeout=180)
        self.metrics.update(json.loads((self.root/'measurements.json').read_text()))
        self.metrics['scope']='Synthetic large composite-key target; production planner/apply and saved-commit replay; no PG throughput claim'
        for name in sorted(CHECKS):
            self.checks.append(name);print('PASS',name,flush=True)


def run(root,phase):
    import pyarrow as pa
    import pyarrow.compute as pc
    import incremental_worker as w
    from incremental.planning import mutation_lease
    from deltalake import write_deltalake
    root=Path(root);generation=root/'generations'/'append';path=generation/'table'
    report_path=root/'measurements.json';saved=root/'plan.json'
    names=['k1','k2','k3','value'];columns=[[int(i<3),name,23,-1] for i,name in enumerate(names)]
    schema=pa.schema([pa.field(name,pa.int32(),False) for name in names])
    def new_row(i):return [1000+i,i%18000,1,i%101-50]
    if phase=='generate':
        generation.mkdir(parents=True)
        def batches():
            for lo in range(0,ROWS,16384):
                values=range(lo,min(lo+16384,ROWS))
                yield pa.RecordBatch.from_arrays([
                    pa.array([n%241 for n in values],pa.int32()),
                    pa.array([n//241%18000 for n in values],pa.int32()),
                    pa.array([n//(241*18000) for n in values],pa.int32()),
                    pa.array([n%101-50 for n in values],pa.int32())],schema=schema)
        write_deltalake(str(path),pa.RecordBatchReader.from_batches(schema,batches()),
            target_file_size=16*1024**2,configuration={'delta.dataSkippingNumIndexedCols':'0'},
            writer_properties=w.WriterProperties(compression='UNCOMPRESSED',max_row_group_size=1024))
        report_path.write_text(json.dumps(dict(rows=ROWS,insert_rows=INSERTS,
            target_files=len(list(path.glob('*.parquet'))),parquet_bytes=sum(p.stat().st_size for p in path.glob('*.parquet')))))
        return
    if phase=='verify':
        for version in (0,1):
            seen=bytearray(ROWS+INSERTS);count=0
            dataset=w.DeltaTable(str(path),version=version).to_pyarrow_dataset(
                filesystem=w.fs.SubTreeFileSystem(str(path),w.fs.LocalFileSystem()))
            assert dataset.schema.equals(schema,check_metadata=True)
            for batch in dataset.scanner(batch_size=16384,batch_readahead=1,fragment_readahead=1,use_threads=False).to_batches():
                a,b,c,value=[batch.column(name) for name in names]
                inserted=pc.greater_equal(a,1000)
                ordinal=pc.add(pc.multiply(pc.add(pc.multiply(c.cast(pa.int64()),18000),b),241),a)
                ordinal=pc.if_else(inserted,pc.add(a.cast(pa.int64()),ROWS-1000),ordinal)
                new_index=pc.subtract(a.cast(pa.int64()),1000)
                def modulo(x,n):return pc.subtract(x,pc.multiply(pc.divide(x,n),n))
                expected=pc.subtract(modulo(pc.if_else(inserted,new_index,ordinal),101),50).cast(pa.int32())
                assert value.equals(expected),('values',version)
                assert pc.all(pc.if_else(inserted,pc.and_(pc.equal(c,1),pc.equal(b,modulo(new_index,18000))),
                    pc.and_(pc.and_(pc.greater_equal(a,0),pc.less(a,241)),pc.and_(pc.greater_equal(b,0),pc.less(b,18000))))).as_py()
                for key in ordinal.to_pylist():
                    assert 0<=key<ROWS+(INSERTS if version else 0) and not seen[key],('duplicate/unexpected',version,key)
                    seen[key]=1;count+=1
            assert count==ROWS+(INSERTS if version else 0),(version,count)
        return
    config=dict(id='bounded-append',identity=dict(generation='fixture',decoder_version=1),
                after_lsn='0/64',target_lsn='0/D2',deadline_ms=int(time.time()*1000)+120000)
    table=dict(oid=42,path='table',version=0,rows=ROWS)
    started=time.monotonic()
    if phase=='append':
        messages=[b'B'+struct.pack('!QqI',200,0,123)]
        for i in range(INSERTS):
            row=new_row(i);data=b'I'+struct.pack('!I',42)+b'N'+struct.pack('!H',len(row))
            for value in row:
                text=str(value).encode();data+=b't'+struct.pack('!I',len(text))+text
            messages.append(data)
        messages.append(b'C'+struct.pack('!BQQq',0,200,210,0))
        payload=b''.join(struct.pack('!I',len(m))+m for m in messages)
        previous=dict(epoch_id='baseline',manifest=dict(tables=[table]))
        with mutation_lease(generation) as lease:
            prepared=w.plan(config,generation,previous,
                ({'42':['public','inventory','d',columns]},[(210,payload)],None,None),lease)
        saved.write_bytes(w.canonical(prepared));planned=prepared['tables'][0]
        assert planned['row_delta']==INSERTS and len(planned['rows'])==INSERTS
    else:
        prepared=json.loads(saved.read_text());planned=prepared['tables'][0]
    planned_at=time.monotonic();checksum=hashlib.sha256(w.canonical(prepared)).hexdigest()
    if phase=='append':
        def crash(point):
            if point=='after_table_commit':raise SystemExit(86)
        w.fault=crash
        try:w.apply_table(config,generation,table,planned,checksum,frozenset())
        except SystemExit as error:assert error.code==86
        else:raise AssertionError('commit fault not reached')
        assert w.DeltaTable(str(path)).version()==1
        metrics=dict(committed_before_receipt=True,operation=w.DeltaTable(str(path)).history(1)[0]['operation'])
    else:
        version,metrics=w.apply_table(config,generation,table,planned,checksum,frozenset())
        assert version==1 and metrics['replayed']
    hwm=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
    report=json.loads(report_path.read_text());report[phase]=dict(elapsed_seconds=time.monotonic()-started,
        planning_seconds=planned_at-started,apply_seconds=time.monotonic()-planned_at,highwater_bytes=hwm,metrics=metrics)
    report_path.write_text(json.dumps(report,indent=2)+'\n')
    assert hwm<768*1024**2,('worker_memory_budget',hwm)
    if phase=='append':assert metrics['operation']=='WRITE'
    else:assert metrics['apply_kind']=='append'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--phase',choices=['generate','append','replay','verify'],required=True)
    args=parser.parse_args();os.umask(0o077)
    sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
    run(args.root,args.phase)
