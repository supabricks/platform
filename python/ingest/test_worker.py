import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import errno

spec=importlib.util.spec_from_file_location('ingest_worker',Path(__file__).with_name('worker.py'))
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)

class CSV(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.config=dict(root=str(self.root),deadline_ms=time.time()*1000+60000,options=dict(delimiter=',',header=True,null_strings=['']))
    def tearDown(self): self.tmp.cleanup()
    def file(self,data):
        path=self.root/'input.csv';path.write_bytes(data);return path
    def test_multiline_bom_null_empty_and_duplicate_headers(self):
        path=self.file(b'\xef\xbb\xbfid,id,"odd"" name"\n001,,""\n9007199254740993,"line\nnext",last\n')
        view=w.inspect_csv(self.config,path)
        self.assertEqual([c['name'] for c in view['mapping']['columns']],['id','id_2','odd" name'])
        self.assertEqual(view['rows'],[['001',None,''],['9007199254740993','line\nnext','last']])
        self.assertEqual([c['input'] for c in view['mapping']['columns']],['0','1','2'])
    def test_tab_headerless_and_malformed_late_rows(self):
        self.config['options'].update(delimiter='\t',header=False)
        path=self.file(b'001\ttrue\n002\tfalse\n')
        self.assertEqual(w.inspect_csv(self.config,path)['rows'][0],['001','true'])
        self.config['options'].update(delimiter=',',header=True)
        path=self.file(b'a,b\n'+b'1,ok\n'*400000+b'"broken\n')
        # A valid prefix is only a sample, never whole-file validation.
        self.assertEqual(len(w.inspect_csv(self.config,path)['rows']),100)
        with self.assertRaises(w.pa.ArrowException): list(w.rows(self.config,path,self.config['options']))
    def test_types_reject_lossy_values(self):
        def c(kind,**other): return dict(nullable=False,data_type=dict(kind=kind,**other))
        self.assertEqual(w.convert('9007199254740993',c('bigint')),9007199254740993)
        self.assertEqual(str(w.convert('12345678901234567890.1234567890',c('decimal',precision=30,scale=10))),'12345678901234567890.1234567890')
        for value,column in [('32768',c('smallint')),('999.99',c('decimal',precision=4,scale=2)),('1.001',c('decimal',precision=4,scale=2)),('NaN',c('double')),('yes',c('boolean')),('2020-01-01T00:00:00Z',c('timestamp')),('2020-01-01T00:00:00.1234567',c('timestamp')),('\0',c('text'))]:
            with self.subTest(value=value),self.assertRaises((w.Rejected,ValueError)):w.convert(value,column)
    def test_headers_and_preview_are_bounded(self):
        path=self.file((','.join(['a']*257)+'\n').encode())
        with self.assertRaises(w.Rejected): w.inspect_csv(self.config,path)
        path=self.file(b'a\n'+b'x'*(1024*1024+1)+b'\n')
        with self.assertRaises((w.Rejected,w.pa.ArrowException)):w.inspect_csv(self.config,path)

    def test_acquisition_detects_mutation_and_never_accepts_io_failure(self):
        path=self.file(b'a\n'+b'value\n'*20000)
        config=dict(self.config,path=str(path),part=str(self.root/'copy.part'),workspace=str(self.root),mode='stage')
        original=path.read_bytes()
        def mutate(*args,**kwargs):
            with path.open('r+b') as out:out.write(b'b')
        with patch.object(w,'progress',mutate),self.assertRaisesRegex(w.Rejected,'source_changed'):
            w.stage(config)
        (self.root/'copy.part').unlink();path.write_bytes(original)
        with patch.object(w,'progress',side_effect=OSError(errno.ENOSPC,'synthetic full volume')),self.assertRaises(OSError) as error:
            w.stage(config)
        self.assertEqual(error.exception.errno,errno.ENOSPC)
        self.assertEqual(path.read_bytes(),original)
        (self.root/'copy.part').unlink()
        with patch.object(w,'FREE_BYTES',10**30),self.assertRaisesRegex(w.Rejected,'disk_reserve'):
            w.stage(config)
        self.assertFalse((self.root/'copy.part').exists())
    def test_decoded_and_deadline_limits(self):
        path=self.file(b'a\n123456789\n')
        with patch.object(w,'DECODED_BYTES',5),self.assertRaisesRegex(w.Rejected,'decoded_limit'):
            list(w.rows(self.config,path,self.config['options']))
        self.config['deadline_ms']=0
        with self.assertRaisesRegex(w.Rejected,'deadline'):w.boundary(self.config)

if __name__=='__main__':unittest.main()
