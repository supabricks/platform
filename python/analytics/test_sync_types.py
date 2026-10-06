"""Lossless DATE plans/replay and fixed-width CHAR storage metadata."""
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
import pyarrow as pa
import pyarrow.fs as fs
from deltalake import DeltaTable,write_deltalake
from capture.spool import Spool,CaptureError,canonical
from incremental.rows import value,UNCHANGED
from incremental_worker import run,apply_table
from export import arrow_type,field_metadata,Rejected,query
from test_incremental import tx,change


class DateSync(unittest.TestCase):
    def setUp(self):
        previous=os.umask(0o077);self.addCleanup(os.umask,previous)
    def test_only_iso_finite_dates_are_decoded(self):
        column=[0,'d',1082,-1]
        for raw in ['0001-01-01','9999-12-31','2000-02-29','1900-03-01','1970-01-01']:
            self.assertEqual(value(raw,column),date.fromisoformat(raw))
        for raw in ['infinity','-infinity','10000-01-01','0001-01-01 BC','1900-02-29','20240229','2024-W09-4','02/29/2024','2024-02-29 00:00:00']:
            with self.subTest(raw=raw),self.assertRaises(CaptureError):value(raw,column)
        self.assertIsNone(value(None,column))

    def test_typed_dates_and_nulls_survive_key_move_and_committed_plan_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);base=root/'base';base.mkdir();identity=dict(generation=str(uuid.uuid4()),installation_id='i',project_id='p',branch_id='b',tenant_id='t',timeline_id='l',decoder_version=1)
            cols=[[1,'id',23,-1],[0,'d',1082,-1],[0,'amount',1700,((18<<16)|2)+4],[0,'note',25,-1]]
            schema=pa.schema([pa.field('id',pa.int32(),False),pa.field('d',pa.date32()),pa.field('amount',pa.decimal128(18,2)),pa.field('note',pa.string())])
            original=[dict(id=1,d=date(1,1,1),amount=Decimal('1234567890123456.78'),note='keep'),dict(id=2,d=date(9999,12,31),amount=None,note=None)]
            write_deltalake(str(base/'42'),pa.Table.from_pylist(original,schema=schema),configuration={'delta.dataSkippingNumIndexedCols':'0'})
            table=dict(oid=42,schema='public',name='dates',path='42',version=0,rows=2,columns=[dict(name=c[1],type_oid=c[2],typmod=c[3]) for c in cols])
            files=[dict(path=str(p.relative_to(base)),bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(base.rglob('*')) if p.is_file()]
            manifest=dict(id='bootstrap',format_version=1,status='files_complete',published=False,source={**{k:identity[k] for k in ['project_id','branch_id','tenant_id','timeline_id']},'lsn':'0/C8'},tables=[table],files=files)
            (base/'manifest.json').write_bytes(canonical(manifest))
            spool=Spool(root/'spool',identity);self.addCleanup(spool.close);spool.establish(100,{'42':['public','dates','d',cols]});spool.set('bootstrap',dict(id='bootstrap',lsn='0/C8'))
            config=dict(id=str(uuid.uuid4()),epoch_id=str(uuid.uuid4()),ordinal=1,source_revision=1,identity=identity,worker_generation=1,workspace=str(root/'first'),generation=str(root/'analytics/incremental'/identity['generation']),spool=str(spool.path),bootstrap_id='bootstrap',bootstrap_manifest=str(base/'manifest.json'),bootstrap_lsn='0/C8',after_lsn='0/C8',target_lsn='0/C8',previous=None,deadline_ms=int(time.time()*1000)+60000)
            Path(config['workspace']).mkdir();run(config);first=json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
            spool.append(280,300,tx(280,300,change(b'U',42,new=[3,'2000-02-29',Decimal('1234567890123456.79'),UNCHANGED],old=[1,None,None,None]),change(b'D',42,old=[2,None,None,None]),change(b'I',42,new=[4,None,None,'null']),change(b'I',42,new=[5,'9999-12-31',None,'max'])))
            config.update(id=str(uuid.uuid4()),epoch_id=str(uuid.uuid4()),ordinal=2,workspace=str(root/'next'),previous=first,target_lsn='0/12C');Path(config['workspace']).mkdir()
            def crash(point):
                if point=='after_table_commit':raise SystemExit(86)
            with patch('incremental_worker.fault',crash),self.assertRaises(SystemExit):run(config)
            saved=json.loads((Path(config['workspace'])/'plan.json').read_text());self.assertIn('2000-02-29',json.dumps(saved))
            with patch('incremental_worker.journal',side_effect=AssertionError('must replay saved plan')):run(config)
            path=Path(config['generation'])/'tables/42';filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem())
            current=DeltaTable(str(path)).to_pyarrow_table(filesystem=filesystem)
            self.assertEqual(current.schema.field('d').type,pa.date32())
            self.assertEqual(sorted(current.to_pylist(),key=lambda r:r['id']),[dict(id=3,d=date(2000,2,29),amount=Decimal('1234567890123456.79'),note='keep'),dict(id=4,d=None,amount=None,note='null'),dict(id=5,d=date(9999,12,31),amount=None,note='max')])
            self.assertEqual(DeltaTable(str(path),version=0).to_pyarrow_table(filesystem=filesystem).to_pylist(),original)
            spool.close()


class CharSync(unittest.TestCase):
    def setUp(self):
        previous=os.umask(0o077);self.addCleanup(os.umask,previous)
    def test_fixed_width_unicode_spaces_and_nulls_are_preserved(self):
        column=[0,'c',1042,8]
        for raw in ['x   ','    ','🧱é  ','a\t  ']:self.assertEqual(value(raw,column),raw)
        self.assertIsNone(value(None,column))
        for raw in ['x','abcde']:
            with self.assertRaises(CaptureError):value(raw,column)
        for mod in [-1,4,10_485_765]:
            with self.assertRaises(Rejected):arrow_type(1042,mod)
        self.assertEqual(field_metadata(1042,8),{b'__CHAR_VARCHAR_TYPE_STRING':b'char(4)'})
        self.assertIn('octet_length("c")',query('public','t',[dict(name='c',type_oid=1042)]).as_string())
    def test_char_metadata_and_raw_bytes_survive_delta_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'generation';root.mkdir();path=root/'table'
            schema=pa.schema([pa.field('id',pa.int32(),False),pa.field('c',pa.string(),metadata=field_metadata(1042,8))])
            write_deltalake(str(path),pa.Table.from_pylist([dict(id=1,c='x   '),dict(id=2,c='    '),dict(id=3,c=None)],schema=schema))
            planned=dict(before=0,columns=[[1,'id',23,-1],[0,'c',1042,8]],rows=[[1,[1,'y   ']],[2,None],[4,[4,'🧱é  ']]])
            config=dict(id=str(uuid.uuid4()),deadline_ms=int(time.time()*1000)+60000)
            def crash(point):
                if point=='after_table_commit':raise SystemExit(86)
            with patch('incremental_worker.fault',crash),self.assertRaises(SystemExit):apply_table(config,root,dict(path='table'),planned,'a'*64,frozenset())
            version,metrics=apply_table(config,root,dict(path='table'),json.loads(canonical(planned)),'a'*64,frozenset())
            self.assertEqual(version,1);self.assertTrue(metrics['replayed'])
            filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem())
            current=DeltaTable(str(path)).to_pyarrow_table(filesystem=filesystem)
            self.assertEqual(current.schema.field('c').metadata,field_metadata(1042,8))
            self.assertEqual(sorted(current.to_pylist(),key=lambda r:r['id']),[dict(id=1,c='y   '),dict(id=3,c=None),dict(id=4,c='🧱é  ')])
            self.assertEqual(DeltaTable(str(path),version=0).to_pyarrow_table(filesystem=filesystem).to_pylist(),[dict(id=1,c='x   '),dict(id=2,c='    '),dict(id=3,c=None)])


if __name__=='__main__':unittest.main()
