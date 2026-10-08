#!/usr/bin/env python3
"""Exact SF1 source/publication checks and complete managed-session SQL results.

Run only after a successful load. This restarts the same private installed cell;
it does not repair failed captures or replace the source with a direct Delta load.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from inputs import LOCK, inventory, sha, statements
from load import save


def canonical(row):
    # Typed schemas are checked separately. This representation is lossless for
    # the admitted integers, exact decimals, finite dates and padded strings.
    return json.dumps([None if value is None else str(value) for value in row],
                      ensure_ascii=True,separators=(',',':')).encode()


def digest_rows(rows, directory):
    """Order-independent partitioned SHA-256, preserving duplicate multiplicity."""
    directory.mkdir(exist_ok=False)
    files=[(directory/f'{i:02x}.hashes').open('xb') for i in range(256)]
    count=0
    try:
        for row in rows:
            value=hashlib.sha256(canonical(row)).digest()
            files[value[0]].write(value);count+=1
    finally:
        for stream in files:stream.close()
    partitions=[]
    for i in range(256):
        path=directory/f'{i:02x}.hashes'
        if path.stat().st_size>64*1024**2:raise ValueError('verification partition exceeds memory bound')
        data=path.read_bytes()
        values=sorted(data[offset:offset+32] for offset in range(0,len(data),32))
        partitions.append(dict(rows=len(values),sha256=hashlib.sha256(b''.join(values)).hexdigest()))
    return dict(rows=count,partitions=partitions)


def table_worker(config):
    import psycopg
    import pyarrow as pa
    import pyarrow.fs as fs
    from deltalake import DeltaTable
    table=config['table'];name=table['name'];columns=table['columns']
    root=Path(config['output']);root.mkdir(exist_ok=False)
    path=Path(config['generation'])/config['published']['path']
    delta=DeltaTable(str(path),version=config['published']['version'])
    dataset=delta.to_pyarrow_dataset(filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem()))
    expected=[]
    for c in columns:
        kind=c['type']
        if kind=='integer':typ=pa.int32()
        elif kind=='date':typ=pa.date32()
        elif kind.startswith('decimal('):typ=pa.decimal128(*map(int,kind[8:-1].split(',')))
        else:typ=pa.string()
        field=dataset.schema.field(c['name'])
        assert field.type==typ,(name,c['name'],str(field.type),str(typ))
        if kind.startswith('char('):
            assert (field.metadata or {}).get(b'__CHAR_VARCHAR_TYPE_STRING')==kind.encode()
        expected.append(c['name'])
    assert dataset.schema.names==expected
    with psycopg.connect(**config['credentials']) as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        db.execute("SET DateStyle='ISO,YMD'")
        with db.cursor(name='eq02_verify') as cursor:
            cursor.itersize=8192
            cursor.execute(psycopg.sql.SQL('SELECT {} FROM {}').format(
                psycopg.sql.SQL(',').join(map(psycopg.sql.Identifier,expected)),psycopg.sql.Identifier(name)))
            source=digest_rows(cursor,root/'source')
    def delta_rows():
        for batch in dataset.scanner(batch_size=8192,batch_readahead=1,fragment_readahead=1,use_threads=False).to_batches():
            # Column order is explicit; never coerce decimals through floats.
            yield from zip(*(batch.column(i).to_pylist() for i in range(len(columns))))
    actual=digest_rows(delta_rows(),root/'delta')
    result=dict(table=name,source=source,delta=actual,schema=str(dataset.schema),
                status='PASS' if source==actual else 'MISMATCH')
    save(root/'result.json',result)
    print(json.dumps(dict(table=name,status=result['status'],rows=source['rows'],report_sha256=sha(root/'result.json'))))
    if source!=actual:raise ValueError('source/Delta partition mismatch; retained hashes require row-difference investigation')


def query_worker(config):
    from pyspark.sql import SparkSession
    spark=SparkSession.builder.remote(config['endpoint']).getOrCreate()
    assert spark.conf.get('supabricks.epoch_id')==config['epoch_id']
    output=Path(config['output']);frame=spark.sql(config['sql']);count=size=0
    schema=frame.schema.jsonValue()
    with output.open('xb') as stream:
        for row in frame.toLocalIterator():
            line=canonical(row)+b'\n';size+=len(line)
            if size>16*1024**2:raise ValueError('complete query result exceeds evidence byte ceiling')
            stream.write(line);count+=1
    print(json.dumps(dict(rows=count,schema=schema,result_sha256=sha(output),result_bytes=size)))
    # Parent closes the product-owned session, including failed/cancelled work.


def release_provenance(loaded, release, load_root, load_release=None):
    """Admit a stopped, explicit native-only upgrade without rewriting receipts."""
    identity=sha(release/'release.json')
    original=loaded['release_identity']
    if load_release is None:
        assert original==identity,'load release changed without explicit upgrade provenance'
        return dict(release_identity=identity)
    assert sha(load_release/'release.json')==original,'wrong original load release'
    before=json.loads((load_release/'release.json').read_text())
    after=json.loads((release/'release.json').read_text())
    assert before['version']!=after['version'],'upgrade must have a distinct version'
    before.pop('version');after.pop('version')
    old_binary=before['files'].pop('bin/supabricks')
    new_binary=after['files'].pop('bin/supabricks')
    assert old_binary!=new_binary and before==after,'only the native binary may change'
    journal=load_root/'state/last-upgrade.json'
    upgrade=json.loads(journal.read_text())
    assert upgrade['from']['identity']==original and upgrade['to']['identity']==identity
    assert upgrade['backup_id'] and upgrade['backup'],'explicit stopped backup required'
    runtime=json.loads((load_root/'state/runtime.json').read_text())
    assert runtime['installation_identity']==identity,'upgrade not applied to this cell'
    assert not (load_root/'state/upgrade.json').exists(),'incomplete upgrade'
    return dict(release_identity=identity,load_release_identity=original,
                upgrade_receipt_sha256=sha(journal),upgrade_backup_id=upgrade['backup_id'],
                changed_payload_files=['bin/supabricks'])


def run(args):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
    from installed_sync import InstalledContinuous
    from cell import wait
    loaded=json.loads((args.load/'result.json').read_text())
    assert loaded['status']=='PASS' and loaded['committed_rows']==19557335
    assert loaded['stopped']
    provenance=release_provenance(loaded,args.release,args.load,args.load_release)
    assert loaded['input_lock_sha256']==sha(LOCK)
    manifest=inventory(json.loads(LOCK.read_text()),args.inputs)
    args.output.mkdir(parents=True,exist_ok=False)
    cell=InstalledContinuous(args.release.resolve(),(args.load/'state').resolve())
    cell.project=loaded['final_capture']['project_id'];cell.work=cell.root/'work'
    cell.python=str(cell.release/'python/analytics/python');cell.policy_id=loaded['final_policy']['id']
    report=dict(status='RUNNING',scope='EQ02 engineering installed product; no exact release claim',
                fixture_sha256=sha(Path(__file__)),load_receipt_sha256=sha(args.load/'result.json'),
                input_lock_sha256=sha(LOCK),**provenance,
                generation_receipt_sha256=loaded['generation_receipt_sha256'],
                load_profile_sha256=loaded['load_profile_sha256'],
                epoch_id=loaded['publication']['epoch_id'],tables=[],
                comparison='exact typed values; order differences require review; no floating-point tolerance',
                queries=[dict(q,status='not_run',reason='exact table verification pending') for q in manifest['queries']])
    started=time.monotonic();reader=None
    def checkpoint():
        report['elapsed_seconds']=time.monotonic()-started;save(args.output/'result.json',report)
    try:
        cell.start();cell.parent=cell.request(method='branch',id=loaded['final_capture']['branch_id'])
        cell.parent=cell.state(cell.parent,'running');wait(lambda:cell.sql(cell.parent,'SELECT 1')=='1')
        cell.healthy()
        assert cell.current()['epoch_id']==report['epoch_id'],'source boundary changed since stopped load'
        # Keep this one coherent epoch retained throughout bounded verification.
        lease=cell.cli('analytics','pin',report['epoch_id'],'--ttl-ms','3600000')
        report['lease_id']=lease['id'];checkpoint()
        for table in manifest['tables']:
            publication=loaded['publication'];descriptor=publication['descriptor']
            config=dict(table=table,published=next(t for t in descriptor['manifest']['tables'] if t['name']==table['name']),
                        generation=str(cell.root/descriptor['generation']),output=str(args.output/table['name']),
                        credentials=dict(host='127.0.0.1',port=cell.parent['ports']['sql'],user='cloud_admin',
                                         password=cell.credentials(cell.parent),dbname='postgres'))
            command=[cell.python,str(Path(__file__).resolve()),'--worker','table']
            child=subprocess.run(command,input=json.dumps(config),text=True,capture_output=True,timeout=900)
            if child.returncode:raise RuntimeError(child.stderr)
            report['tables'].append(json.loads(child.stdout));checkpoint()
            cell.cli('analytics','renew',lease['id'],'--ttl-ms','3600000')
            print('VERIFIED',table['name'],report['tables'][-1]['rows'],flush=True)
        query_root=args.output/'queries';query_root.mkdir()
        for entry in report['queries']:
            identifier=entry['id'];start=time.monotonic();entry.pop('reason',None)
            try:
                reader=cell.opened(epoch=report['epoch_id'],ttl_ms=600000)
                entry['session_start_seconds']=time.monotonic()-start
                config=dict(endpoint=reader['endpoint'],epoch_id=reader['epoch_id'],
                            sql=statements((args.inputs/'spark'/(identifier+'.sql')).read_text())[0],
                            output=str(query_root/(identifier+'.rows.jsonl')))
                start=time.monotonic()
                child=subprocess.run([cell.python,str(Path(__file__).resolve()),'--worker','query'],
                                     input=json.dumps(config),text=True,capture_output=True,timeout=120)
                if child.returncode:
                    (query_root/(identifier+'.error.txt')).write_text(child.stderr)
                    entry.update(status='failed',error_sha256=sha(query_root/(identifier+'.error.txt')))
                else:entry.update(json.loads(child.stdout),status='complete_requires_reference_comparison')
            except subprocess.TimeoutExpired:
                entry.update(status='timeout',reason='120-second statement ceiling')
            except Exception as error:
                entry.update(status='failed',reason=str(error))
            finally:
                entry['elapsed_seconds']=time.monotonic()-start
                if reader is not None:
                    cell.close(reader);reader=None
                cell.cli('analytics','renew',lease['id'],'--ttl-ms','3600000')
                checkpoint();print('PRODUCT QUERY',identifier,entry['status'],flush=True)
        cell.cli('analytics','unpin',lease['id'])
        report['status']='EXECUTED_REQUIRES_REFERENCE_COMPARISON'
    except BaseException as error:
        report.update(status='FAIL',error=str(error));raise
    finally:
        if (cell.root/'control.sock').exists():
            try:cell.stop();report['stopped']=True
            except Exception as error:report.update(status='FAIL',cleanup_error=str(error))
        checkpoint()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',choices=['table','query'])
    for name in ('release','inputs','load','output'):parser.add_argument('--'+name,type=Path)
    parser.add_argument('--load-release',type=Path,
                        help='Original release, only after an explicit stopped native-only upgrade')
    args=parser.parse_args()
    if args.worker:
        {'table':table_worker,'query':query_worker}[args.worker](json.load(sys.stdin))
    else:
        if any(getattr(args,name) is None for name in ('release','inputs','load','output')):parser.error('all paths required')
        run(args)
