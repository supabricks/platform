"""Explicit large-profile batching keeps complete transactions and replay proof."""
import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch
from deltalake import DeltaTable
from capture.spool import CaptureError
from incremental_worker import run
from incremental.rows import MAX_ROWS,UNCHANGED
import test_incremental as fixtures
from test_incremental import change,tx


class LargeApplyTests(unittest.TestCase):
    storage_profile='large'
    setUp=fixtures.IncrementalTests.setUp
    tearDown=fixtures.IncrementalTests.tearDown
    config_next=fixtures.IncrementalTests.config_next
    rows=fixtures.IncrementalTests.rows

    def test_large_prefix_crash_replay_and_key_moves_preserve_exact_history(self):
        # Four individually legal commits fill the 65,536-operation apply.
        # A following cross-table key move/delete must be a separate epoch.
        for batch in range(4):
            end=300+100*batch;oid=42+batch%2;start=2+batch*MAX_ROWS
            payload=tx(end-20,end,*(change(b'I',oid,new=[key,None,'retained'])
                                    for key in range(start,start+MAX_ROWS)))
            self.spool.append(end-20,end,payload)
        self.spool.append(680,700,tx(680,700,
            change(b'U',42,old=[2,None,None],new=[100000,None,UNCHANGED]),
            change(b'D',43,old=[MAX_ROWS+2,None,None])))
        config=self.config_next('0/2BC')
        def crash(point):
            if point=='after_first_table':raise SystemExit(86)
        with patch('incremental_worker.fault',crash),self.assertRaises(SystemExit):run(config)
        plan=json.loads((Path(config['workspace'])/'plan.json').read_text())
        self.assertEqual(plan['end_lsn'],'0/258')
        self.assertEqual(sum(len(t['rows']) for t in plan['tables']),65536)
        with patch('incremental_worker.journal',side_effect=AssertionError('replay reread journal')):run(config)
        prefix=json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
        self.assertEqual([t['rows'] for t in prefix['manifest']['tables']],[32769,32769,1])
        for oid in [42,43]:
            expected={1}|{key for batch in range(4) if 42+batch%2==oid
                          for key in range(2+batch*MAX_ROWS,2+(batch+1)*MAX_ROWS)}
            self.assertEqual({r['id'] for r in self.rows(prefix,oid)},expected)
        following=dict(config,id=str(uuid.uuid4()),epoch_id=str(uuid.uuid4()),ordinal=3,
                       workspace=str(self.root/'work3'),previous=prefix,after_lsn='0/258')
        Path(following['workspace']).mkdir();run(following)
        final=json.loads((Path(following['workspace'])/'result.json').read_text())['descriptor']
        self.assertEqual(final['manifest']['source']['lsn'],'0/2BC')
        current={r['id']:r for r in self.rows(final,42)}
        self.assertNotIn(2,current);self.assertEqual(current[100000]['note'],'retained')
        self.assertNotIn(MAX_ROWS+2,{r['id'] for r in self.rows(final,43)})
        self.assertIn(2,{r['id'] for r in self.rows(prefix,42)})
        self.assertIn(MAX_ROWS+2,{r['id'] for r in self.rows(prefix,43)})
        self.assertEqual(self.rows(self.first,42)[0]['note'],'original')

    def test_large_profile_does_not_expand_single_transaction_admission(self):
        payload=tx(280,300,*(change(b'I',43,new=[n,None,None]) for n in range(2,MAX_ROWS+3)))
        self.spool.append(280,300,payload);config=self.config_next('0/12C')
        with self.assertRaisesRegex(CaptureError,'apply_row_budget'):run(config)
        self.assertFalse((Path(config['workspace'])/'plan.json').exists())
        self.assertEqual(DeltaTable(str(Path(config['generation'])/'tables/43')).version(),0)

    def test_large_profile_still_proves_absent_keys_before_append(self):
        self.spool.append(280,300,tx(280,300,change(b'I',42,new=[1,None,'collision'])))
        config=self.config_next('0/12C')
        with self.assertRaisesRegex(CaptureError,'duplicate_source_key'):run(config)
        self.assertEqual(DeltaTable(str(Path(config['generation'])/'tables/42')).version(),0)


if __name__=='__main__':unittest.main()
