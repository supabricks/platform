"""Format fidelity and resource bounds; integration gates exercise real COPY."""
from test_worker import w
from pathlib import Path
from decimal import Decimal
import datetime as dt
import json
import tempfile
import time
import unittest
from unittest.mock import patch
import pyarrow as pa
import pyarrow.parquet as pq

class Formats(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=dict(root=str(self.root),deadline_ms=time.time()*1000+60000,
                         options=dict(format='json_lines',delimiter=',',header=True,null_strings=[]))
    def tearDown(self):self.tmp.cleanup()
    def file(self,raw):
        p=self.root/'input';p.write_bytes(raw.encode() if isinstance(raw,str) else raw);return p
    def inspect(self,p,format):
        self.config['options']['format']=format
        return w.inspect_source(self.config,p)
    def load(self,p,m):return [r for r,_ in w.load_rows(self.config,p,m)]
    def test_json_numbers_missing_null_nested_and_key_order(self):
        p=self.file('{"id":9007199254740993,"zip":"001","amount":12345678901234567890.1234567890,"meta":{"n":9007199254740993}}\n{"meta":null,"id":2,"zip":"002","amount":null}\n{"id":3,"zip":"003","amount":1.0000000000}\n')
        v=self.inspect(p,'json_lines');m=v['mapping'];rows=self.load(p,m)
        self.assertEqual(rows[0][:3],[9007199254740993,'001',Decimal('12345678901234567890.1234567890')])
        self.assertEqual(rows[1][3].obj,None);self.assertIsNone(rows[2][3])
        self.assertEqual(rows[0][3].dumps(rows[0][3].obj),'{"n":9007199254740993}')
        self.assertEqual(v['rows'][1][3],'null');self.assertIsNone(v['rows'][2][3])
    def test_json_array_and_document_modes(self):
        p=self.file('[{"id":1},{"id":2}]');m=self.inspect(p,'json_array')['mapping']
        self.assertEqual(self.load(p,m),[[1],[2]])
        p=self.file('{"rows":[1,null,{"amount":1.234567890123456789}]}')
        with self.assertRaisesRegex(w.Rejected,'json_array_required'):self.inspect(p,'json_array')
        m=self.inspect(p,'json_document')['mapping'];row=self.load(p,m)[0][0]
        self.assertEqual(row.dumps(row.obj),'{"rows":[1,null,{"amount":1.234567890123456789}]}')
        m['columns'][0]['data_type']={'kind':'text'}
        with self.assertRaisesRegex(w.Rejected,'document_requires_jsonb'):self.load(p,m)
    def test_duplicate_keys_nonfinite_shapes_unicode_depth(self):
        for raw in ['{"x":1,"x":2}\n','{"x":NaN}\n','[]\n','\n','{"x":"\\u0000"}\n','{"x":"\\ud800"}\n','{"x":'+ '['*66+'0'+']'*66+'}\n']:
            with self.subTest(raw=raw), self.assertRaises((w.Rejected,ValueError,UnicodeError)):
                self.inspect(self.file(raw),'json_lines')
    def test_late_schema_and_type_drift(self):
        for suffix,code in [('{"id":1,"new":2}\n','unmapped_json_key'),('{"id":"oops"}\n','source_type_mismatch')]:
            p=self.file('{"id":1}\n'*110+suffix);m=self.inspect(p,'json_lines')['mapping']
            with self.assertRaisesRegex(w.Rejected,code):self.load(p,m)
    def test_json_bounds_and_truncated_inputs(self):
        p=self.file('[{"a":1}]')
        with patch.object(w,'JSON_BYTES',5),self.assertRaisesRegex(w.Rejected,'json_source_limit'):self.inspect(p,'json_array')
        p=self.file('{"a":"'+ 'x'*100+'"}\n')
        with patch.object(w,'RECORD_BYTES',50),self.assertRaisesRegex(w.Rejected,'record_limit'):self.inspect(p,'json_lines')
        with patch.object(w,'DECODED_BYTES',50),self.assertRaisesRegex(w.Rejected,'decoded_limit'):self.inspect(p,'json_lines')
        with self.assertRaises(ValueError):self.inspect(self.file('[{"a":1}'),'json_array')
    def parquet(self,table):
        p=self.root/'input.parquet';pq.write_table(table,p,row_group_size=128);return p
    def test_parquet_unsigned_decimal_timezone_nested(self):
        table=pa.table({'u':pa.array([2**64-1,None],type=pa.uint64()),
            'd':pa.array([Decimal('12345678901234567890.1234567890'),None],type=pa.decimal128(30,10)),
            't':pa.array([dt.datetime(2020,1,1,tzinfo=dt.timezone.utc),None],type=pa.timestamp('us',tz='America/Chicago')),
            'nested':pa.array([{'xs':[1,2]},None])})
        p=self.parquet(table);v=self.inspect(p,'parquet');rows=self.load(p,v['mapping'])
        self.assertEqual(rows[0][0],Decimal(2**64-1));self.assertEqual(rows[0][1],Decimal('12345678901234567890.1234567890'))
        self.assertEqual(rows[0][2],dt.datetime(2020,1,1,tzinfo=dt.timezone.utc))
        self.assertEqual(rows[0][3].obj,{'xs':[1,2]});self.assertEqual(rows[1],[None]*4)
        self.assertEqual(v['rows'][1],[None]*4)
        self.assertIn('America/Chicago',v['source_schema'][2]['arrow_type'])
        v['mapping']['columns'][0]['data_type']={'kind':'bigint'}
        with self.assertRaisesRegex(w.Rejected,'integer_overflow'):self.load(p,v['mapping'])
    def test_parquet_nanoseconds_reject_loss(self):
        p=self.parquet(pa.table({'t':pa.array([1000],type=pa.timestamp('ns'))}))
        self.assertEqual(self.load(p,self.inspect(p,'parquet')['mapping'])[0][0],dt.datetime(1970,1,1,0,0,0,1))
        p=self.parquet(pa.table({'t':pa.array([1001],type=pa.timestamp('ns'))}))
        with self.assertRaisesRegex(w.Rejected,'invalid_parquet'):self.inspect(p,'parquet')
    def test_parquet_unsupported_types_corruption_and_decoded_bound(self):
        for t,value in [(pa.binary(),b'x'),(pa.map_(pa.string(),pa.int64()),[('x',1)]),(pa.decimal256(50,2),Decimal('1.00'))]:
            with self.subTest(type=t),self.assertRaises(w.Rejected):self.inspect(self.parquet(pa.table({'x':pa.array([value],type=t)})),'parquet')
        p=self.parquet(pa.table({'a':['repeated'*100]*1000}))
        with patch.object(w,'DECODED_BYTES',100),self.assertRaisesRegex(w.Rejected,'decoded_limit'):self.inspect(p,'parquet')
        p.write_bytes(p.read_bytes()[:-1])
        with self.assertRaises(w.Rejected):self.inspect(p,'parquet')
    def test_parquet_late_page_checksum_failure(self):
        p=self.root/'corrupt.parquet'
        pq.write_table(pa.table({'id':list(range(300))}),p,row_group_size=100,compression='NONE',use_dictionary=False,write_page_checksum=True)
        reader=pq.ParquetFile(p)
        offset=reader.metadata.row_group(2).column(0).data_page_offset
        # Flip a byte in the final data page; footer and the preview remain valid.
        raw=bytearray(p.read_bytes());raw[offset+100] ^= 1;p.write_bytes(raw)
        m=self.inspect(p,'parquet')['mapping']
        with self.assertRaisesRegex(w.Rejected,'invalid_parquet'):self.load(p,m)
    def test_mapping_cannot_drop_parquet_inputs_or_extra_json_keys(self):
        p=self.parquet(pa.table({'a':[1],'b':[2]}));m=self.inspect(p,'parquet')['mapping'];m['columns'].pop()
        with self.assertRaisesRegex(w.Rejected,'mapping_requires_each_input_column_once'):self.load(p,m)
    def test_json_scalar_drift_requires_explicit_jsonb(self):
        p=self.file('{"x":true}\n{"x":"true"}\n');m=self.inspect(p,'json_lines')['mapping']
        self.assertEqual(m['columns'][0]['data_type']['kind'],'jsonb')
        self.assertEqual([x[0].obj for x in self.load(p,m)],[True,'true'])

if __name__=='__main__':unittest.main()
