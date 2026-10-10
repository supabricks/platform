"""Explicit disk profiles preserve fences, legacy admission and worker reuse."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from capture.spool import CaptureError
from incremental import storage
from incremental.planning import inventory_snapshot, mutation_lease, PlanningBoundary
from incremental_worker import run
import test_incremental as fixture


class StorageProfileTests(unittest.TestCase):
    def setUp(self):
        previous=os.umask(0o077);self.addCleanup(os.umask,previous)

    def test_large_admission_does_not_leak_into_the_next_compact_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();path=root/'sparse'
            with path.open('wb') as stream:stream.truncate(1024**3+1)
            deadline=time.time()*1000+10000
            self.assertEqual(storage.boundary(root,deadline,profile='large'),1024**3+1)
            self.assertEqual(inventory_snapshot(root,deadline,'large')[2],1024**3+1)
            with mutation_lease(root) as lease,PlanningBoundary(root,deadline,lease,'large') as guard:
                guard.check()
            with self.assertRaisesRegex(CaptureError,'incremental_disk_budget'):storage.boundary(root,deadline)
            with self.assertRaisesRegex(CaptureError,'incremental_disk_budget'):inventory_snapshot(root,deadline)
            with path.open('wb') as stream:stream.truncate(128*1024**3+1)
            with self.assertRaisesRegex(CaptureError,'incremental_disk_budget'):storage.boundary(root,deadline,profile='large')
            with self.assertRaisesRegex(CaptureError,'incremental_disk_budget'):inventory_snapshot(root,deadline,'large')

    def test_large_retention_remains_bounded_and_counts_every_root(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=Path(temp);root=parent/'root';root.mkdir();path=root/'sparse'
            with path.open('wb') as stream:stream.truncate(4*1024**3+1)
            self.assertEqual(storage.retained_boundary(parent,profile='large'),4*1024**3+1)
            with self.assertRaisesRegex(CaptureError,'incremental_retention_budget'):storage.retained_boundary(parent)
            with self.assertRaisesRegex(CaptureError,'incremental_retention_budget'):
                storage.retained_boundary(parent,extra=380*1024**3,profile='large')
            empty=parent/'empty';empty.mkdir()
            with self.assertRaisesRegex(CaptureError,'incremental_retention_budget'):
                storage.retained_boundary(empty,extra=384*1024**3+1,profile='large')

    def test_unknown_profile_and_actual_disk_exhaustion_fail_closed(self):
        from incremental.reuse import scope
        self.assertNotEqual(scope({'storage_profile':'compact'}),scope({'storage_profile':'large'}))
        for profile in [None,True,{},'unlimited',128*1024**3]:
            with self.subTest(profile=profile),self.assertRaises(CaptureError):storage.storage_limits(profile)
        with tempfile.TemporaryDirectory() as temp:
            with patch('incremental.storage.os.statvfs') as space:
                space.return_value.f_bavail=0;space.return_value.f_frsize=4096
                with self.assertRaisesRegex(CaptureError,'incremental_disk_budget'):
                    storage.boundary(Path(temp),time.time()*1000+1000,profile='large')


class LargeWorkerTests(unittest.TestCase):
    tearDown=fixture.IncrementalTests.tearDown
    config_next=fixture.IncrementalTests.config_next
    rows=fixture.IncrementalTests.rows
    def setUp(self):
        fixture.IncrementalTests.setUp(self)
        generation=str(uuid.uuid4())
        self.config=dict(self.config,storage_profile='large',storage_generation=generation,
            generation=str(self.root/'analytics/incremental'/generation),workspace=str(self.root/'large-bootstrap'))
        Path(self.config['workspace']).mkdir();run(self.config)
        self.first=json.loads((Path(self.config['workspace'])/'result.json').read_bytes())['descriptor']

    def test_profile_is_bound_to_bootstrap_owner_and_commit_replay(self):
        self.assertEqual(self.first['manifest']['storage_profile'],'large')
        owner=json.loads((Path(self.config['generation'])/'owner.json').read_bytes())
        self.assertEqual(owner['storage_profile'],'large')
        self.spool.append(280,300,fixture.tx(280,300,fixture.change(b'I',42,new=[2,None,'two'])))
        config=self.config_next('0/12C')
        def crash(point):
            if point=='after_table_commit':raise SystemExit(86)
        with patch('incremental_worker.fault',crash),self.assertRaises(SystemExit):run(config)
        run(config)
        result=json.loads((Path(config['workspace'])/'result.json').read_bytes())['descriptor']
        self.assertEqual(result['manifest']['storage_profile'],'large')
        self.assertEqual(len(self.rows(self.first,42)),1)
        self.assertEqual(len(self.rows(result,42)),2)
        self.assertTrue(result['manifest']['apply_metrics'][0]['metrics']['replayed'])

    def test_previous_profile_cannot_be_downgraded_or_omitted(self):
        config=self.config_next('0/C8')
        for profile in ['compact',None]:
            bad=dict(config)
            if profile is None:bad.pop('storage_profile')
            else:bad['storage_profile']=profile
            with self.assertRaisesRegex(CaptureError,'incremental_storage_profile_changed'):run(bad)
        self.assertFalse((Path(config['workspace'])/'result.json').exists())


if __name__=='__main__':unittest.main()
