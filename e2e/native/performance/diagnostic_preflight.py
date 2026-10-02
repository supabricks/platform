#!/usr/bin/env python3
"""Verify diagnostic capability behavior before admitting a benchmark package."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from native_package import sha


def check(binary):
    # A copied executable outside release.json discovers no installed engine.
    # This probes the exact bytes with a disposable engine-free daemon only.
    identity=sha(binary)
    with tempfile.TemporaryDirectory(prefix='sb-profile-check-') as temporary:
        root=Path(temporary);probe=root/'supabricks';shutil.copy2(binary,probe)
        assert sha(probe)==identity
        data=root/'data';profile=data/'sync-profile';profile.mkdir(parents=True);data.chmod(0o700)
        (profile/'enabled').touch()
        with (root/'private.log').open('wb') as log:
            child=subprocess.Popen([str(probe),'daemon','--data-dir',str(data)],stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+15
                while not (data/'control.sock').exists():
                    assert child.poll() is None,'diagnostic daemon exited during startup'
                    assert time.monotonic()<deadline,'diagnostic daemon startup timeout'
                    time.sleep(.02)
                subprocess.run([str(probe),'status','--data-dir',str(data)],check=True,
                               stdout=subprocess.DEVNULL,stderr=log,timeout=10)
                subprocess.run([str(probe),'down','--data-dir',str(data)],check=True,
                               stdout=subprocess.DEVNULL,stderr=log,timeout=15)
                assert child.wait(timeout=10)==0,'diagnostic daemon shutdown failed'
            finally:
                if child.poll() is None:
                    child.kill();child.wait(timeout=10)
        path=profile/'daemon.jsonl'
        assert path.is_file(),'native sync-profile capability missing; rebuild with --features sync-profile'
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        assert rows and rows[-1]['final'],'daemon final profile missing'
        assert all(not r['profile_write_errors'] and not r['budget_exceeded'] for r in rows)
        return dict(binary_sha256=identity,status='passed',engine_execution=False,
                    final_profile=True,snapshots=len(rows),profile_sha256=sha(path),exit_code=0)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',required=True,type=Path)
    parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args()
    args.report.write_text(json.dumps(check(args.binary.resolve()),indent=2)+'\n')
