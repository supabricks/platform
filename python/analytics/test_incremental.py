"""SY03 independent version reads, complete commit replay and lossless row overlay."""
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
import pyarrow as pa
import pyarrow.fs as fs
from deltalake import DeltaTable,write_deltalake
from capture.spool import Spool,canonical,CaptureError
from incremental_worker import run
from incremental.rows import changes,overlay,UNCHANGED
from test_capture import begin,commit

MOD=((38<<16)|8)+4
PROFILE={str(oid):['public',name,'d',[[1,'id',23,-1],[0,'amount',1700,MOD],[0,'note',25,-1]]] for oid,name in [(42,'orders'),(43,'payments'),(44,'unchanged')]}

def tup(values):
    data=struct.pack('!H',len(values))
    for v in values:
        if v is UNCHANGED:data+=b'u'
        elif v is None:data+=b'n'
        else:
            value=str(v).encode();data+=b't'+struct.pack('!I',len(value))+value
    return data

def change(tag,oid,new=None,old=None):
    data=tag+struct.pack('!I',oid)
    if old is not None:data+=b'K'+tup(old)
    if new is not None:data+=b'N'+tup(new)
    return data

def tx(commit_lsn,end,*messages):
    return b''.join(struct.pack('!I',len(p))+p for p in [begin(commit_lsn),*messages,commit(commit_lsn,end)])

class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        identity=dict(generation=str(uuid.uuid4()),installation_id='install',project_id='p',branch_id='b',tenant_id='t',timeline_id='l',decoder_version=1)
        self.base=self.root/'baseline';self.base.mkdir();tables=[]
        schema=pa.schema([pa.field('id',pa.int32(),nullable=False),pa.field('amount',pa.decimal128(38,8)),pa.field('note',pa.string())])
        for oid,(_,name,_,_) in PROFILE.items():
            write_deltalake(str(self.base/oid),pa.Table.from_pylist([dict(id=1,amount=Decimal('12345678901234567890.12345678'),note='original')],schema=schema),configuration={'delta.dataSkippingNumIndexedCols':'0'})
            tables.append(dict(oid=int(oid),schema='public',name=name,path=oid,version=0,rows=1,columns=[dict(name=c[1],type_oid=c[2],typmod=c[3]) for c in PROFILE[oid][3]]))
        files=[dict(path=str(p.relative_to(self.base)),bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(self.base.rglob('*')) if p.is_file()]
        manifest=dict(id='bootstrap',format_version=1,status='files_complete',published=False,source={**{k:identity[k] for k in ['project_id','branch_id','tenant_id','timeline_id']},'lsn':'0/C8'},tables=tables,files=files)
        (self.base/'manifest.json').write_bytes(canonical(manifest))
        self.spool=Spool(self.root/'spool',identity);self.spool.establish(100,PROFILE);self.spool.set('bootstrap',dict(id='bootstrap',lsn='0/C8'))
        self.config=dict(id=str(uuid.uuid4()),epoch_id=str(uuid.uuid4()),ordinal=1,source_revision=1,identity=identity,worker_generation=1,
            workspace=str(self.root/'work1'),generation=str(self.root/'analytics/incremental'/identity['generation']),spool=str(self.spool.path),bootstrap_id='bootstrap',bootstrap_manifest=str(self.base/'manifest.json'),bootstrap_lsn='0/C8',after_lsn='0/C8',target_lsn='0/C8',previous=None,deadline_ms=int(time.time()*1000)+60000)
        Path(self.config['workspace']).mkdir();run(self.config)
        self.first=json.loads((Path(self.config['workspace'])/'result.json').read_text())['descriptor']
    def tearDown(self):self.spool.close();self.tmp.cleanup()
    def config_next(self,target):
        result=dict(self.config,id=str(uuid.uuid4()),epoch_id=str(uuid.uuid4()),ordinal=2,workspace=str(self.root/'work2'),previous=self.first,target_lsn=target)
        Path(result['workspace']).mkdir();return result
    def rows(self,descriptor,oid):
        table=next(t for t in descriptor['manifest']['tables'] if t['oid']==oid)
        path=Path(self.config['generation'])/table['path']
        # Fresh process, pinned delta-rs version; no in-process cached table state.
        code="import sys,json;from deltalake import DeltaTable;import pyarrow.fs as f;p=sys.argv[1];print(json.dumps(DeltaTable(p,version=int(sys.argv[2])).to_pyarrow_table(filesystem=f.SubTreeFileSystem(p,f.LocalFileSystem())).to_pylist(),default=str))"
        return json.loads(subprocess.check_output([sys.executable,'-c',code,str(path),str(table['version'])],text=True))
    def assert_incremental_durability(self,replay):
        root=Path(self.config['generation'])
        old={root/e['path'] for e in self.first['manifest']['files']}
        self.spool.append(280,300,tx(280,300,change(b'I',42,new=[2,None,'two'])))
        config=self.config_next('0/12C')
        if replay:
            def crash(point):
                if point=='after_table_commit':raise SystemExit(86)
            with patch('incremental_worker.fault',crash),self.assertRaises(SystemExit):run(config)
        flushed=set();fsync=os.fsync
        def record(fd):
            stat=os.fstat(fd);flushed.add((stat.st_dev,stat.st_ino));fsync(fd)
        with patch('incremental.storage.os.fsync',record):run(config)
        def identity(path):
            stat=path.stat();return stat.st_dev,stat.st_ino
        result=json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
        new={root/e['path'] for e in result['manifest']['files']}-old
        self.assertTrue(new)
        self.assertFalse({identity(p) for p in old}&flushed)
        self.assertTrue({identity(p) for p in new}<=flushed)
        self.assertTrue({identity(p.parent) for p in new}<=flushed)
    def test_new_files_are_durable_without_reflushing_published_prefix(self):
        self.assert_incremental_durability(False)
    def test_unpublished_replayed_files_are_flushed_even_when_they_already_exist(self):
        self.assert_incremental_durability(True)
    def test_key_move_unchanged_toast_decimal_delete_and_unchanged_table(self):
        raw=tx(280,300,change(b'U',42,new=[2,Decimal('12345678901234567890.12345678'),UNCHANGED],old=[1,None,None]),change(b'D',43,old=[1,None,None]),change(b'I',43,new=[3,None,'Unicode 🧱']))
        self.spool.append(280,300,raw);config=self.config_next('0/12C');run(config)
        result=json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
        self.assertEqual(self.rows(result,42),[dict(id=2,amount='12345678901234567890.12345678',note='original')])
        self.assertEqual(self.rows(result,43),[dict(id=3,amount=None,note='Unicode 🧱')])
        self.assertEqual(self.rows(self.first,42)[0]['id'],1)
        self.assertEqual(result['manifest']['tables'][2]['version'],0)
        self.assertEqual(result['manifest']['source']['lsn'],'0/12C')
    def test_partial_table_commit_reconciles_without_duplicate_and_preserves_old_map(self):
        raw=tx(280,300,change(b'I',42,new=[2,None,'two']),change(b'I',43,new=[2,None,'two']))
        self.spool.append(280,300,raw);config=self.config_next('0/12C')
        def crash(point):
            if point=='after_first_table':raise SystemExit(86)
        with patch('incremental_worker.fault',crash),self.assertRaises(SystemExit):run(config)
        self.assertEqual(len(self.rows(self.first,42)),1);self.assertEqual(len(self.rows(self.first,43)),1)
        run(config);result=json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
        self.assertEqual([t['version'] for t in result['manifest']['tables']],[1,1,0])
        self.assertEqual(len(self.rows(result,42)),2);self.assertEqual(len(self.rows(result,43)),2)
    def test_cross_transaction_order_and_empty_table(self):
        self.spool.append(280,300,tx(280,300,change(b'I',42,new=[2,None,'inserted']),change(b'D',43,old=[1,None,None])))
        self.spool.append(380,400,tx(380,400,change(b'U',42,new=[3,None,UNCHANGED],old=[2,None,None]),change(b'D',42,old=[1,None,None])))
        config=self.config_next('0/190');run(config)
        result=json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
        self.assertEqual(self.rows(result,42),[dict(id=3,amount=None,note='inserted')])
        self.assertEqual(self.rows(result,43),[])
        self.assertEqual([t['rows'] for t in result['manifest']['tables']],[1,0,1])
    def test_one_key_update_rewrites_only_affected_files(self):
        from incremental.storage import inventory
        root=Path(self.config['generation']);path=root/'tables/42'
        schema=DeltaTable(str(path)).to_pyarrow_dataset(filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem())).schema
        for start in range(1000,9000,1000):
            write_deltalake(str(path),pa.Table.from_pylist([dict(id=i,amount=None,note='payload-'+str(i)*100) for i in range(start,start+1000)],schema=schema),mode='append')
        self.first['manifest']['tables'][0].update(version=8,rows=8001)
        self.first['manifest']['files']=inventory(root,self.first['manifest']['tables'])
        old_files={p.name:p.stat().st_size for p in path.glob('*.parquet')}
        self.spool.append(280,300,tx(280,300,change(b'U',42,new=[1500,None,'updated'])))
        config=self.config_next('0/12C');run(config)
        result=json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
        metric=result['manifest']['apply_metrics'][0]['metrics']
        self.assertEqual(metric['num_target_rows_updated'],1)
        self.assertEqual(metric['num_target_files_removed'],1)
        self.assertLess(metric['num_target_files_added'],len(old_files))
        written=sum(p.stat().st_size for p in path.glob('*.parquet') if p.name not in old_files)
        self.assertEqual(metric['new_parquet_bytes'],written)
        self.assertEqual(metric['retained_parquet_bytes'],sum(old_files.values())+written)
        self.assertGreater(written,0)
        self.assertTrue(all((path/name).stat().st_size==size for name,size in old_files.items()))
        self.assertEqual(len(self.rows(result,42)),8001)
        self.assertEqual(result['manifest']['tables'][0]['rows'],8001)
    def test_checksum_corruption_and_prewrite_budget_preserve_versions(self):
        raw=tx(280,300,change(b'I',42,new=[2,None,'two']))
        self.spool.append(280,300,raw);config=self.config_next('0/12C')
        with patch('incremental.storage.MAX_BYTES',1),self.assertRaises(CaptureError):run(config)
        self.assertEqual(DeltaTable(str(Path(config['generation'])/'tables/42')).version(),0)
        data=next((Path(config['generation'])/'tables/42').glob('*.parquet'))
        with data.open('r+b') as stream:stream.write(b'BAD!')
        with self.assertRaisesRegex(CaptureError,'incremental_checksum'):run(config)
        self.assertFalse((Path(config['workspace'])/'result.json').exists())
    def test_duplicate_missing_and_nonfinite_values_fail_closed(self):
        cases=[tx(280,300,change(b'I',42,new=[1,None,'duplicate'])),tx(280,300,change(b'D',42,old=[99,None,None])),tx(280,300,change(b'I',42,new=[2,'NaN','bad']))]
        for raw in cases:
            with self.subTest(raw=raw[:4]),self.assertRaises(CaptureError):
                ops=changes(raw,PROFILE,300);overlay(ops,{'42':{1:[1,None,'original']}},PROFILE)
    def test_insert_update_delete_same_key_in_one_transaction_has_no_visible_effect(self):
        raw=tx(280,300,change(b'I',42,new=[2,None,'two']),change(b'U',42,new=[2,None,'changed']),change(b'D',42,old=[2,None,None]))
        self.spool.append(280,300,raw);config=self.config_next('0/12C');run(config)
        result=json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
        self.assertEqual([t['version'] for t in result['manifest']['tables']],[0,0,0]);self.assertEqual(result['manifest']['source']['lsn'],'0/12C')

if __name__=='__main__':unittest.main()
