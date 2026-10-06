"""EQ01 multi-column identity, exact lookup, saved-plan replay and pinned versions."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.fs as fs
from deltalake import DeltaTable, write_deltalake
from capture.protocol import Decoder
from capture.spool import Spool, CaptureError, canonical
from incremental.rows import changes, overlay, UNCHANGED
from incremental_worker import run, key_batches
from test_incremental import tx, change
from test_capture import begin, commit
import struct

MOD=((18<<16)|2)+4
COLUMNS=[[1,'a',20,-1],[0,'amount',1700,MOD],[1,'b',21,-1],[0,'note',25,-1]]
PROFILE={'42':['public','pairs','d',COLUMNS],
         '43':['public','triples','d',[[1,'a',23,-1],[1,'b',23,-1],[1,'c',23,-1]]]}


class CompositeKeys(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.identity=dict(generation=str(uuid.uuid4()),installation_id='install',project_id='p',branch_id='b',tenant_id='t',timeline_id='l',decoder_version=1)
        self.spool=Spool(self.root/'spool',self.identity);self.spool.establish(100,PROFILE)
        self.spool.set('bootstrap',dict(id='bootstrap',lsn='0/C8'))
        base=self.root/'base';base.mkdir();tables=[]
        schemas=[pa.schema([pa.field('a',pa.int64(),False),pa.field('amount',pa.decimal128(18,2)),pa.field('b',pa.int16(),False),pa.field('note',pa.string())]),
                 pa.schema([pa.field(n,pa.int32(),False) for n in ('a','b','c')])]
        data=[[dict(a=a,b=b,amount=Decimal('1234567890123456.78'),note=f'{a}:{b}') for a,b in [(1,1),(1,2),(2,1),(2,2),(-2**63,-32768),(2**63-1,32767)]],
              [dict(a=1,b=1,c=1),dict(a=1,b=1,c=2),dict(a=1,b=2,c=1)]]
        for (oid,(_,name,_,cols)),schema,rows in zip(PROFILE.items(),schemas,data):
            write_deltalake(str(base/oid),pa.Table.from_pylist(rows,schema=schema))
            tables.append(dict(oid=int(oid),schema='public',name=name,path=oid,version=0,rows=len(rows),columns=[dict(name=c[1],type_oid=c[2],typmod=c[3]) for c in cols]))
        files=[dict(path=str(p.relative_to(base)),bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(base.rglob('*')) if p.is_file()]
        manifest=dict(id='bootstrap',format_version=1,status='files_complete',published=False,source={**{k:self.identity[k] for k in ['project_id','branch_id','tenant_id','timeline_id']},'lsn':'0/C8'},tables=tables,files=files)
        (base/'manifest.json').write_bytes(canonical(manifest))
        self.config=dict(id=str(uuid.uuid4()),epoch_id=str(uuid.uuid4()),ordinal=1,source_revision=1,identity=self.identity,worker_generation=1,
            workspace=str(self.root/'work1'),generation=str(self.root/'analytics/incremental'/self.identity['generation']),spool=str(self.spool.path),bootstrap_id='bootstrap',bootstrap_manifest=str(base/'manifest.json'),bootstrap_lsn='0/C8',after_lsn='0/C8',target_lsn='0/C8',previous=None,deadline_ms=int(time.time()*1000)+60000)
        Path(self.config['workspace']).mkdir();run(self.config)
        self.first=self.result(self.config)
    def tearDown(self):self.spool.close();self.tmp.cleanup()
    def result(self,config):return json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
    def next(self):
        c=dict(self.config,id=str(uuid.uuid4()),epoch_id=str(uuid.uuid4()),ordinal=2,workspace=str(self.root/'work2'),previous=self.first,target_lsn='0/12C')
        Path(c['workspace']).mkdir();return c
    def rows(self,descriptor,oid):
        t=next(t for t in descriptor['manifest']['tables'] if t['oid']==oid)
        path=Path(self.config['generation'])/t['path']
        return DeltaTable(str(path),version=t['version']).to_pyarrow_table(filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem())).to_pylist()
    def test_exact_pair_filter_never_selects_cartesian_neighbors(self):
        dataset=ds.dataset(pa.table({'a':[1,1,2,2],'b':[1,2,1,2]}))
        result=[r for b in key_batches(dataset,COLUMNS,{(1,1),(2,2)}) for r in b.to_pylist()]
        self.assertEqual(result,[dict(a=1,b=1),dict(a=2,b=2)])
        # A full key-moving batch can touch 32,768 old/new identities.
        result=[r for b in key_batches(dataset,COLUMNS,{(i,i) for i in range(32768)}) for r in b.to_pylist()]
        self.assertEqual(result,[dict(a=1,b=1),dict(a=2,b=2)])
    def test_partial_commit_replay_full_keys_and_immutable_previous_epoch(self):
        original=self.rows(self.first,42)
        raw=tx(280,300,
            change(b'U',42,new=[1,Decimal('1.25'),3,UNCHANGED],old=[1,None,1,None]),
            change(b'D',42,old=[2,None,2,None]),
            change(b'U',42,new=[-2**63,Decimal('2.50'),-32767,UNCHANGED],old=[-2**63,None,-32768,None]),
            change(b'I',42,new=[8,None,8,'temporary']),
            change(b'D',42,old=[8,None,8,None]),
            change(b'U',43,new=[1,1,3],old=[1,1,1]),
            change(b'D',43,old=[1,2,1]))
        self.spool.append(280,300,raw)
        self.spool.close();self.spool=Spool(self.root/'spool',self.identity)
        self.assertFalse(self.spool.append(280,300,raw))
        config=self.next()
        def crash(point):
            if point=='after_first_table':raise SystemExit(86)
        with patch('incremental_worker.fault',crash),self.assertRaises(SystemExit):run(config)
        saved=json.loads((Path(config['workspace'])/'plan.json').read_text())
        self.assertTrue(all(isinstance(k,list) for t in saved['tables'] for k,_ in t['rows']))
        self.assertEqual(self.rows(self.first,42),original)
        with patch('incremental_worker.journal',side_effect=AssertionError('replay must use saved plan')):run(config)
        result=self.result(config);rows={(r['a'],r['b']):r for r in self.rows(result,42)}
        self.assertEqual(set(rows),{(1,2),(1,3),(2,1),(-2**63,-32767),(2**63-1,32767)})
        self.assertEqual(rows[(1,3)],dict(a=1,b=3,amount=Decimal('1.25'),note='1:1'))
        self.assertEqual(rows[(1,2)]['amount'],Decimal('1234567890123456.78'))
        self.assertEqual({tuple(r[n] for n in ('a','b','c')) for r in self.rows(result,43)},{(1,1,2),(1,1,3)})
        self.assertEqual([t['version'] for t in result['manifest']['tables']],[1,1])
        self.assertEqual([t['rows'] for t in result['manifest']['tables']],[5,2])
        self.assertEqual(self.rows(self.first,42),original)
    def test_complete_key_duplicate_and_missing_rejections(self):
        existing={'42':{(1,1):[1,None,1,'one'],(1,2):[1,None,2,'two']}}
        cases=[change(b'I',42,new=[1,None,1,'duplicate']),
               change(b'U',42,new=[1,None,2,'collision'],old=[1,None,1,None]),
               change(b'D',42,old=[1,None,3,None])]
        for message in cases:
            with self.subTest(message=message),self.assertRaises(CaptureError):
                overlay(changes(tx(280,300,message),PROFILE,300),existing,PROFILE)
        # Sharing one component is valid and must never collide.
        changed=overlay(changes(tx(280,300,change(b'I',42,new=[1,None,3,'three'])),PROFILE,300),existing,PROFILE)
        self.assertEqual(set(changed['42']),{(1,3)})
    def test_null_unchanged_and_noninteger_key_components_rejected(self):
        for invalid in (None,UNCHANGED,'not-int',32768):
            with self.subTest(invalid=invalid),self.assertRaises(CaptureError):
                changes(tx(280,300,change(b'I',42,new=[1,None,invalid,'bad'])),PROFILE,300)
    def test_pgoutput_relation_all_key_flags_survive_reconnect(self):
        relation=b'R'+struct.pack('!I',42)+b'public\0pairs\0d'+struct.pack('!H',len(COLUMNS))
        for flag,name,typ,mod in COLUMNS:relation+=bytes([flag])+name.encode()+b'\0'+struct.pack('!Ii',typ,mod)
        outputs=[]
        for _ in range(2):
            decoder=Decoder(PROFILE,'fence')
            for frame in [begin(280),relation,change(b'D',42,old=[1,None,2,None])]:decoder.feed(frame)
            outputs.append(decoder.feed(commit(280,300)))
        self.assertEqual(outputs[0],outputs[1])
        self.assertEqual(changes(outputs[0][2],PROFILE,300)[0][2],(1,2))


if __name__=='__main__':unittest.main()
