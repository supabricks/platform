"""SP04 ownership, accounting lifetime and safety boundaries."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from capture.spool import CaptureError
from incremental import storage
from incremental.planning import mutation_lease, PlanningBoundary, inventory_snapshot

class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.mask=os.umask(0o077)
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()/'generation'
        self.root.mkdir(mode=0o700);(self.root/'table').mkdir()
        self.file=self.root/'table/data';self.file.write_bytes(b'original')
        self.deadline=time.time()*1000+60000
    def tearDown(self):self.tmp.cleanup();os.umask(self.mask)
    def test_owned_and_unowned_scan_counts(self):
        with patch('incremental.planning.inventory_snapshot',wraps=inventory_snapshot) as scan:
            with mutation_lease(self.root) as lease,PlanningBoundary(self.root,self.deadline,lease) as guard:
                for _ in range(100):guard.check()
            self.assertEqual(scan.call_count,2)
        with patch('incremental.storage.boundary',wraps=storage.boundary) as scan:
            with PlanningBoundary(self.root,self.deadline) as guard:
                for _ in range(100):guard.check()
            self.assertEqual(scan.call_count,102)
    def test_deadline_space_and_admission_budgets(self):
        with mutation_lease(self.root) as lease,PlanningBoundary(self.root,self.deadline,lease) as guard:
            with patch('incremental.planning.time.time',return_value=self.deadline/1000+1),self.assertRaisesRegex(CaptureError,'apply_deadline'):guard.check()
            with patch('incremental.planning.os.statvfs',return_value=SimpleNamespace(f_bavail=0,f_frsize=4096)),self.assertRaisesRegex(CaptureError,'incremental_disk_budget'):guard.check()
        for budget in ('MAX_FILES','MAX_BYTES'):
            with patch.object(storage,budget,0),mutation_lease(self.root) as lease,self.assertRaises(CaptureError):
                with PlanningBoundary(self.root,self.deadline,lease):pass
    def test_namespace_mutations_fail_before_next_batch(self):
        for mutation in ('create','remove','symlink'):
            with self.subTest(mutation=mutation):
                self.file.write_bytes(b'original')
                with mutation_lease(self.root) as lease,self.assertRaisesRegex(CaptureError,'incremental_plan_mutated'):
                    with PlanningBoundary(self.root,self.deadline,lease) as guard:
                        if mutation=='create':(self.root/'table/new').write_bytes(b'x')
                        else:
                            self.file.unlink()
                            if mutation=='symlink':self.file.symlink_to('/etc/passwd')
                        guard.check()
                for p in (self.file,self.root/'table/new'):
                    if p.exists() or p.is_symlink():p.unlink()
    def test_in_place_growth_and_same_size_edit_fail_at_exit(self):
        for value in (b'x'*128,b'changed!'):
            self.file.write_bytes(b'original')
            with mutation_lease(self.root) as lease,self.assertRaisesRegex(CaptureError,'incremental_plan_mutated'):
                with PlanningBoundary(self.root,self.deadline,lease):self.file.write_bytes(value)
    def test_root_and_ancestor_replacement(self):
        for path in (self.root,self.root.parent):
            with mutation_lease(self.root) as lease:
                moved=path.with_name(path.name+'-moved');path.rename(moved);path.symlink_to(moved,target_is_directory=True)
                try:
                    with self.assertRaisesRegex(CaptureError,'incremental_lease_lost'):lease.check()
                finally:path.unlink();moved.rename(path)
    def test_unsafe_paths_and_writable_tree(self):
        self.file.unlink();self.file.symlink_to('/etc/passwd')
        with mutation_lease(self.root) as lease,self.assertRaisesRegex(CaptureError,'unsafe_incremental_path'):
            with PlanningBoundary(self.root,self.deadline,lease):pass
        self.file.unlink();self.file.write_bytes(b'ok');self.file.chmod(0o666)
        with mutation_lease(self.root) as lease,self.assertRaisesRegex(CaptureError,'unsafe_incremental_path'):
            with PlanningBoundary(self.root,self.deadline,lease):pass
    def test_mutation_needs_new_inventory_and_closed_lease_is_invalid(self):
        with mutation_lease(self.root) as lease:
            with PlanningBoundary(self.root,self.deadline,lease):pass
            self.file.write_bytes(b'new')
            with PlanningBoundary(self.root,self.deadline,lease) as guard:self.assertEqual(guard.snapshot[2],3)
        with self.assertRaisesRegex(CaptureError,'incremental_lease_lost'):
            with PlanningBoundary(self.root,self.deadline,lease):pass
    def test_overlapping_process_and_kill_recovery(self):
        script="import sys,time;from incremental.planning import mutation_lease;\nwith mutation_lease(sys.argv[1]):\n print('locked',flush=True);time.sleep(60)"
        process=subprocess.Popen([sys.executable,'-c',script,str(self.root)],stdout=subprocess.PIPE,text=True,cwd=Path(__file__).resolve().parent)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            with self.assertRaisesRegex(CaptureError,'incremental_writer_busy'):
                with mutation_lease(self.root):pass
        finally:process.kill();process.wait();process.stdout.close()
        with mutation_lease(self.root) as lease:
            with PlanningBoundary(self.root,self.deadline,lease):pass

if __name__=='__main__':unittest.main()
