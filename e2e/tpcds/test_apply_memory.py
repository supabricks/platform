import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

from apply_memory import is_apply_worker


class ProcIdentity:
    """Read the same kernel identity/cmdline without a test-only psutil dependency."""
    def __init__(self,pid):self.pid=pid
    def create_time(self):
        return int(Path(f'/proc/{self.pid}/stat').read_text().rsplit(')',1)[1].split()[19])
    def cmdline(self):
        return [os.fsdecode(v) for v in Path(f'/proc/{self.pid}/cmdline').read_bytes().split(b'\0') if v]


@unittest.skipUnless(Path('/proc/self/stat').exists(),'Linux qualification sampler')
class ApplyMemoryRoleTests(unittest.TestCase):
    def test_same_process_becomes_visible_after_exec(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);worker=root/'incremental_worker.py';bridge=root/'bridge.py'
            worker.write_text("import sys\nprint('worker',flush=True)\nsys.stdin.readline()\n")
            bridge.write_text("import os,sys\nprint('bridge',flush=True)\nsys.stdin.readline()\nos.execv(sys.executable,[sys.executable,os.path.join(os.path.dirname(__file__),'incremental_worker.py')])\n")
            with subprocess.Popen([sys.executable,str(bridge)],stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as child:
                try:
                    def ready(expected):
                        self.assertTrue(select.select([child.stdout],[],[],5)[0],'child handshake timed out')
                        self.assertEqual(child.stdout.readline().strip(),expected)
                    ready('bridge');process=ProcIdentity(child.pid);birth=process.create_time();roles=set()
                    # The worker path must not be a command-line argument of the
                    # bridge: the production detector matches exact path basenames.
                    self.assertFalse(is_apply_worker(process,roles))
                    child.stdin.write('exec\n');child.stdin.flush();ready('worker')
                    self.assertEqual(process.create_time(),birth)
                    self.assertTrue(is_apply_worker(process,roles))
                    child.stdin.write('stop\n');child.stdin.flush()
                    self.assertEqual(child.wait(timeout=5),0)
                finally:
                    if child.poll() is None:child.kill();child.wait(timeout=5)
