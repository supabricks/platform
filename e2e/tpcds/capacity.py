"""Installed >512 MiB live-set compaction/append within the original 1 GiB quota."""
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

ROWS=65_536
WIDTH=8192
INSERTS=16_384
CHECKS={'large_live_set_compaction_and_append_fit_original_capacity',
        'capacity_replay_preserves_exact_source_and_old_versions',
        'append_history_512_versions_keeps_pinned_rows_and_memory_budget'}


class CapacityChecks:
    def __init__(self,release,root):
        self.release=release;self.root=root;self.binary=release/'bin/supabricks';self.checks=[];self.metrics={}
    def check(self,name):self.checks.append(name);print('PASS',name,flush=True)
    def run(self,python,export):
        for phase in ('generate','apply','replay','verify','history'):
            self.metrics['phase']=phase
            try:
                subprocess.run([str(python),str(Path(__file__).resolve()),'--root',str(self.root),'--phase',phase],check=True,timeout=300)
            finally:
                # A failing child writes its measurement before asserting the
                # bound. Preserve it in the installed report, not only /tmp.
                path=self.root/'measurements.json'
                if path.exists():self.metrics.update(json.loads(path.read_text()))
        self.metrics['scope']='Synthetic >512 MiB live table; production initialization/planning/apply and commit replay; no PostgreSQL throughput claim'
        for name in sorted(CHECKS):self.check(name)


def run(root,phase):
    import pyarrow as pa
    import incremental_worker as w
    from incremental.planning import mutation_lease
    from incremental.storage import inventory,verify_previous
    from incremental.maintenance import estimate_bytes
    root=Path(root);source=root/'roots'/'baseline';target=root/'roots'/'candidate'
    path=source/'tables/42';work=root/'work';saved=root/'previous.json';report_path=root/'measurements.json'
    schema=pa.schema([pa.field('id',pa.int32(),False),pa.field('payload',pa.string())])
    def payload(i):return f'{i:08d}'+'x'*(WIDTH-8) if i<ROWS else f'new {i}'
    if phase=='generate':
        source.mkdir(parents=True);work.mkdir()
        def batches():
            for lo in range(0,ROWS,256):
                yield pa.RecordBatch.from_arrays([pa.array(range(lo,lo+256),pa.int32()),
                    pa.array([payload(i) for i in range(lo,lo+256)])],schema=schema)
        w.write_deltalake(str(path),pa.RecordBatchReader.from_batches(schema,batches()),target_file_size=16*1024**2,
            configuration={'delta.dataSkippingNumIndexedCols':'0'},
            writer_properties=w.WriterProperties(compression='UNCOMPRESSED',max_row_group_size=1024))
        tables=[dict(oid=42,name='wide',path='tables/42',rows=ROWS,version=0)]
        previous=dict(epoch_id='baseline',manifest=dict(tables=tables,files=inventory(source,tables),source=dict(lsn='0/64')))
        saved.write_bytes(w.canonical(previous))
        size=sum(p.stat().st_size for p in source.rglob('*') if p.is_file())
        assert 512*1024**2<size<1024**3
        report_path.write_text(json.dumps(dict(rows=ROWS,insert_rows=INSERTS,payload_width=WIDTH,source_bytes=size)))
        return
    if phase=='history':
        started=time.monotonic();latencies=[];memory=[]
        def sample(label):
            memory.append(dict(phase=label,highwater_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)))
        sample('before_append')
        for version in range(2,513):
            before=time.monotonic()
            batch=pa.RecordBatch.from_arrays([pa.array([ROWS+INSERTS+version-2],pa.int32()),
                pa.array([f'history {version}'])],schema=schema)
            w.write_deltalake(str(target/'tables/42'),batch,mode='append')
            latencies.append(time.monotonic()-before)
        sample('after_append')
        for version,maximum in ((0,ROWS),(1,ROWS+INSERTS),(512,ROWS+INSERTS+511)):
            table=w.DeltaTable(str(target/'tables/42'),version=version)
            dataset=table.to_pyarrow_dataset(filesystem=w.fs.SubTreeFileSystem(str(target/'tables/42'),w.fs.LocalFileSystem()))
            seen=bytearray(maximum);count=0
            for batch in dataset.scanner(batch_size=256,batch_readahead=1,fragment_readahead=1,use_threads=False).to_batches():
                for i,value in zip(batch.column('id').to_pylist(),batch.column('payload').to_pylist()):
                    expected=payload(i) if i<ROWS+INSERTS else f'history {i-ROWS-INSERTS+2}'
                    assert 0<=i<maximum and not seen[i] and value==expected
                    seen[i]=1;count+=1
            assert count==maximum
            sample('after_read_version_'+str(version))
        hwm=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
        report=json.loads(report_path.read_text());report['history']=dict(last_version=512,appended_rows=511,
            elapsed_seconds=time.monotonic()-started,highwater_bytes=hwm,commit_seconds=latencies,memory=memory)
        report_path.write_text(json.dumps(report,indent=2)+'\n')
        assert hwm<768*1024**2
        return
    previous=json.loads(saved.read_text());config=dict(id='capacity-apply',identity=dict(installation_id='fixture',generation='capture',decoder_version=1),
        generation=str(target),previous_generation=str(source),previous=previous,storage_generation='candidate',bootstrap_id='bootstrap',bootstrap_lsn='0/64',
        workspace=str(work),after_lsn='0/64',target_lsn='0/D2',deadline_ms=int(time.time()*1000)+240000,
        epoch_id='capacity-epoch',ordinal=1,source_revision=1,worker_generation=1)
    if phase=='verify':
        verify_previous(source,previous)
        for location,version,maximum in ((source,0,ROWS),(target,0,ROWS),(target,1,ROWS+INSERTS)):
            seen=bytearray(maximum);count=0
            dataset=w.DeltaTable(str(location/'tables/42'),version=version).to_pyarrow_dataset(
                filesystem=w.fs.SubTreeFileSystem(str(location/'tables/42'),w.fs.LocalFileSystem()))
            assert dataset.schema.equals(schema,check_metadata=True)
            for batch in dataset.scanner(batch_size=256,batch_readahead=1,fragment_readahead=1,use_threads=False).to_batches():
                for i,value in zip(batch.column('id').to_pylist(),batch.column('payload').to_pylist()):
                    assert 0<=i<maximum and not seen[i] and value==payload(i)
                    seen[i]=1;count+=1
            assert count==maximum
        return
    started=time.monotonic();estimate=estimate_bytes(config)
    assert estimate>1024**3
    generation=w.initialize(config);initialized=time.monotonic()
    data=None
    if phase=='apply':
        messages=[b'B'+struct.pack('!QqI',200,0,123)]
        for i in range(ROWS,ROWS+INSERTS):
            record=b'I'+struct.pack('!I',42)+b'N'+struct.pack('!H',2)
            for text in (str(i).encode(),payload(i).encode()):record+=b't'+struct.pack('!I',len(text))+text
            messages.append(record)
        messages.append(b'C'+struct.pack('!BQQq',0,200,210,0))
        packet=b''.join(struct.pack('!I',len(m))+m for m in messages)
        data=({'42':['public','wide','d',[[1,'id',23,-1],[0,'payload',25,-1]]]},[(210,packet)],None,None)
        def crash(point):
            if point=='after_table_commit':raise SystemExit(86)
        w.fault=crash
        with mutation_lease(generation) as lease:
            try:w.run_owned(config,generation,data,lease)
            except SystemExit as error:assert error.code==86
            else:raise AssertionError('commit-before-receipt fault not reached')
        assert not (work/'result.json').exists()
    else:
        with mutation_lease(generation) as lease:w.run_owned(config,generation,None,lease)
        manifest=json.loads((work/'result.json').read_text())['descriptor']['manifest']
        metric=manifest['apply_metrics'][0]['metrics'];assert metric['replayed'] and metric['apply_kind']=='append'
        assert manifest['generation_base_bytes']==manifest['generation_bytes']
        assert manifest['tables'][0]['rows']==ROWS+INSERTS
    assert w.DeltaTable(str(target/'tables/42')).version()==1
    assert w.DeltaTable(str(target/'tables/42')).history(1)[0]['operation']=='WRITE'
    size=sum(p.stat().st_size for p in target.rglob('*') if p.is_file())
    hwm=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
    report=json.loads(report_path.read_text());report[phase]=dict(elapsed_seconds=time.monotonic()-started,
        initialization_seconds=initialized-started,source_estimate_bytes=estimate,generation_bytes=size,highwater_bytes=hwm)
    report_path.write_text(json.dumps(report,indent=2)+'\n')
    assert size<1024**3 and hwm<768*1024**2


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--phase',choices=['generate','apply','replay','verify','history'],required=True)
    args=parser.parse_args();os.umask(0o077)
    sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
    run(args.root,args.phase)
