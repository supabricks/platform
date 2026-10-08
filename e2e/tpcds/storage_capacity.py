"""Installed >1 GiB live-set admission, compaction, exact history and commit replay.

Each phase runs in a fresh installed Python process. This isolates incremental
worker capacity; it is not PostgreSQL throughput or SF100 qualification.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import struct
import sys
import time

ROWS=131072
WIDTH=8192
INSERTS=1024


def payload_packet(rows):
    messages=[b'B'+struct.pack('!QqI',200,0,123)]
    for key,value in rows:
        record=b'I'+struct.pack('!I',42)+b'N'+struct.pack('!H',2)
        for field in (str(key).encode(),value.encode()):record+=b't'+struct.pack('!I',len(field))+field
        messages.append(record)
    messages.append(b'C'+struct.pack('!BQQq',0,200,210,0))
    return b''.join(struct.pack('!I',len(message))+message for message in messages)


def run(release,root,phase,expected):
    # Production run() sets this before Delta creates nested paths. This fixture
    # calls the owned phases directly to inject a journal and a commit fault.
    os.umask(0o077)
    sys.path.insert(0,str(release/'python/analytics'))
    import pyarrow as pa
    import incremental_worker as w
    from incremental.storage import inventory,verify_previous
    from incremental.planning import mutation_lease
    from capture.spool import CaptureError
    root.mkdir(parents=True,exist_ok=True)
    source=root/'roots/baseline';target=root/'roots/candidate';work=root/'work'
    report_path=root/(phase+'.json')
    if report_path.exists():raise ValueError('fresh phase receipt required')
    saved=root/'previous.json'
    schema=pa.schema([pa.field('id',pa.int32(),False),pa.field('payload',pa.string())])
    def payload(i):return f'{i:08d}'+'x'*(WIDTH-8) if i<ROWS else f'new {i}'
    report=dict(status='FAIL',phase=phase,release_identity=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),
        fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        scope=__doc__,rows=ROWS,width=WIDTH,inserts=INSERTS)
    started=time.monotonic()
    try:
        if phase=='generate':
            source.mkdir(parents=True);work.mkdir()
            def batches():
                for lo in range(0,ROWS,256):
                    yield pa.RecordBatch.from_arrays([pa.array(range(lo,lo+256),pa.int32()),pa.array([payload(i) for i in range(lo,lo+256)])],schema=schema)
            w.write_deltalake(str(source/'tables/42'),pa.RecordBatchReader.from_batches(schema,batches()),target_file_size=16*1024**2,
                max_spill_size=64*1024**2,max_temp_directory_size=64*1024**2,
                configuration={'delta.dataSkippingNumIndexedCols':'0'},
                writer_properties=w.WriterProperties(compression='UNCOMPRESSED',max_row_group_size=1024))
            tables=[dict(oid=42,name='wide',path='tables/42',rows=ROWS,version=0)]
            previous=dict(epoch_id='baseline',manifest=dict(storage_profile='large',tables=tables,
                files=inventory(source,tables),source=dict(lsn='0/64')))
            saved.write_bytes(w.canonical(previous))
            size=sum(p.stat().st_size for p in source.rglob('*') if p.is_file())
            assert 1024**3<size<2*1024**3
            report['source_bytes']=size
        else:
            previous=json.loads(saved.read_bytes())
            report['source_descriptor_sha256']=hashlib.sha256(saved.read_bytes()).hexdigest()
            config=dict(id='capacity-apply',identity=dict(installation_id='fixture',generation='capture',decoder_version=1),
                storage_profile='large',generation=str(target),previous_generation=str(source),previous=previous,
                storage_generation='candidate',bootstrap_id='bootstrap',bootstrap_lsn='0/64',workspace=str(work),
                after_lsn='0/64',target_lsn='0/D2',deadline_ms=int(time.time()*1000)+240000,
                epoch_id='capacity-epoch',ordinal=1,source_revision=1,worker_generation=1)
            if phase=='verify':
                verify_previous(source,previous)
                result=json.loads((work/'result.json').read_bytes())['descriptor']
                verify_previous(target,result)
                for location,version,maximum in [(source,0,ROWS),(target,0,ROWS),(target,1,ROWS+INSERTS)]:
                    seen=bytearray(maximum);count=0
                    path=location/'tables/42'
                    dataset=w.DeltaTable(str(path),version=version).to_pyarrow_dataset(filesystem=w.fs.SubTreeFileSystem(str(path),w.fs.LocalFileSystem()))
                    assert dataset.schema.equals(schema,check_metadata=True)
                    for batch in dataset.scanner(batch_size=256,batch_readahead=1,fragment_readahead=1,use_threads=False).to_batches():
                        for i,value in zip(batch.column('id').to_pylist(),batch.column('payload').to_pylist()):
                            assert 0<=i<maximum and not seen[i] and value==payload(i)
                            seen[i]=1;count+=1
                    assert count==maximum
                report['exact_versions']=[ROWS,ROWS,ROWS+INSERTS]
            else:
                try:
                    generation=w.initialize(config)
                except CaptureError as error:
                    report['observed_error']=error.code
                    if expected!='reject' or error.code!='incremental_disk_budget':raise
                    report['status']='PASS'
                    return
                if expected=='reject':raise AssertionError('original byte quota unexpectedly admitted large live set')
                report['initialize_seconds']=time.monotonic()-started
                data=({'42':['public','wide','d',[[1,'id',23,-1],[0,'payload',25,-1]]]},
                    [(210,payload_packet([(i,payload(i)) for i in range(ROWS,ROWS+INSERTS)]))],None,None)
                if phase=='apply':
                    def crash(point):
                        if point=='after_table_commit':raise SystemExit(86)
                    w.fault=crash
                    with mutation_lease(generation) as lease:
                        try:w.run_owned(config,generation,data,lease)
                        except SystemExit as error:assert error.code==86
                        else:raise AssertionError('fault did not reach committed Delta log')
                    assert not (work/'result.json').exists()
                    report['interrupted_after_commit']=True
                else:
                    with mutation_lease(generation) as lease:w.run_owned(config,generation,None,lease)
                    manifest=json.loads((work/'result.json').read_bytes())['descriptor']['manifest']
                    assert manifest['storage_profile']=='large'
                    assert manifest['apply_metrics'][0]['metrics']['replayed']
                    assert manifest['tables'][0]['rows']==ROWS+INSERTS
                    report['compaction']=manifest['compaction']
                    report['generation_bytes']=manifest['generation_bytes']
                    report['retained_bytes']=manifest['retained_bytes']
                assert w.DeltaTable(str(target/'tables/42')).version()==1
        report['status']='PASS'
    except BaseException as error:
        report['error']=str(error)
        raise
    finally:
        report['elapsed_seconds']=time.monotonic()-started
        report['highwater_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
        if report['highwater_bytes']>=768*1024**2:
            report['status']='FAIL';report['error']='worker_rss_budget'
        report_path.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']!='PASS':raise RuntimeError('capacity phase failed; inspect '+str(report_path))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--phase',choices=['generate','apply','replay','verify'],required=True)
    parser.add_argument('--expected',choices=['accept','reject'],default='accept')
    args=parser.parse_args();run(args.release.resolve(),args.root.resolve(),args.phase,args.expected)
