import json,time,subprocess,hashlib,sys
from pathlib import Path
root=Path(__file__).resolve().parent
probe=Path('/data2/supabricks-performance/issue169-20261006/probe-03')
out=root/'fsync-methods-01';out.mkdir(exist_ok=False)
(out/'status.json').write_text(json.dumps(dict(status='waiting_for_probe_cleanup'))+'\n')
deadline=time.monotonic()+2400
while not (probe/'exit.json').exists():
 if time.monotonic()>deadline:raise TimeoutError('preceding diagnostic did not finish')
 time.sleep(5)
cleanup=json.loads((probe/'cleanup.json').read_text());assert not any(cleanup[k] for k in ('exit_code','timed_out','inspection_failed','leaked_descendants','remaining_descendants'))
binary=root/'sqlite-owner-runtime-01/engine/pg_install/v17/bin/pg_test_fsync'
metadata=dict(scope='Disposable-file component diagnostic; not source throughput or qualification. Production runtime/configuration is not changed.',binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),seconds_per_test=1,devices=['/dev/nvme0n1p5','/dev/nvme1n1p1'],results=[])
sys.path.insert(0,str(root/'harness-05/e2e/native/performance'));from host_monitor import HostMonitor
monitor=HostMonitor(out,quiet_seconds=0).start()
try:
 for repeat in range(1,4):
  order=['root','data2'] if repeat%2 else ['data2','root']
  for name in order:
   base=out if name=='root' else probe.parent
   filename=base/f'owned-pg-test-fsync-{name}-r{repeat}.out'
   assert not filename.exists()
   (out/'status.json').write_text(json.dumps(dict(status='running',device=name,repeat=repeat))+'\n')
   start=time.time()*1000
   with (out/f'{name}-r{repeat}.log').open('x') as log:
    command=['taskset','-c','0-3,8-11',str(binary),'-s','1','-f',str(filename)]
    result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=120)
   metadata['results'].append(dict(device=name,repeat=repeat,exit_code=result.returncode,started_at_ms=start,ended_at_ms=time.time()*1000))
   assert result.returncode==0
   filename.unlink(missing_ok=True)
 (out/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
 (out/'status.json').write_text(json.dumps(dict(status='complete'))+'\n')
finally:monitor.close()
