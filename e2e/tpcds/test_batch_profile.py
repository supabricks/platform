"""Diagnostic accounting is bounded and cannot expose source row contents."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('batch_profile',Path(__file__).with_name('batch_profile.py'))
profile=importlib.util.module_from_spec(spec);spec.loader.exec_module(profile)


class BatchProfileTests(unittest.TestCase):
    def test_framing_counts_changes_without_reading_values(self):
        frames=[b'Bheader',b'Iprivate-value',b'Uother-secret',b'Dkey',b'Mbarrier',b'Ccommit']
        wire=b''.join(struct.pack('!I',len(f))+f for f in frames)
        self.assertEqual(profile.frame_rows(wire),3)
        for invalid in (b'\0',struct.pack('!I',0),struct.pack('!I',100)+b'I'):
            with self.assertRaises(ValueError):profile.frame_rows(invalid)

    def test_event_budget_never_changes_operation_or_exceeds_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'events.jsonl'
            with patch.multiple(profile,OUTPUT=path,WRITTEN=0,DROPPED=0,ERRORS=0,LIMIT=1024):
                for _ in range(100):profile.event('test',count=1)
                self.assertLessEqual(path.stat().st_size,1024)
                self.assertGreater(profile.DROPPED,0)
                self.assertEqual(profile.ERRORS,0)
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                self.assertTrue(all(json.loads(s)['fields']=={'count':1} for s in path.read_text().splitlines()))

    def test_diagnostic_write_failure_preserves_return_and_exception(self):
        with patch.multiple(profile,OUTPUT=Path('/unavailable/parent/trace'),WRITTEN=0,ERRORS=0):
            wrapped=profile.wrap(lambda:17,'test');self.assertEqual(wrapped(),17)
            def failure():raise ValueError('original')
            with self.assertRaisesRegex(ValueError,'original'):profile.wrap(failure,'test')()
            self.assertEqual(profile.ERRORS,2)

    def test_disabled_observation_writes_nothing(self):
        with patch.object(profile,'OUTPUT',None),patch.object(profile.os,'open',side_effect=AssertionError('write')):
            self.assertEqual(profile.wrap(lambda:5,'test')(),5)


if __name__=='__main__':unittest.main()
