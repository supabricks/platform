import sys,json,subprocess,time,threading
from pathlib import Path
root=Path(__file__).resolve().parent
out=root/'probe-04';out.mkdir(exist_ok=False)
sys.path.insert(0,str(root/'harness-05/e2e/native/performance'))
from host_monitor import HostMonitor
from host_io import observe
cmd=json.loads((root/'probe-02-command.json').read_text())
cmd=[s.replace('sp11-issue169-probe-02','sp11-issue169-probe-04').replace(str(root/'probe-02')+':/reports',str(out)+':/reports') .replace('/probe-02.py:/probe.py','/probe-04.py:/probe.py') for s in cmd]
(out/'command.json').write_text(json.dumps(cmd,indent=2)+'\n')
(out/'experiment.json').write_text(json.dumps(dict(scope='Diagnostic only: same frozen harness, package, workload, observations and analysis on original disk; all source connections set commit_delay=2000 microseconds, commit_siblings=5. Durability asserted unchanged. Sequential unpaired comparison.',predecessor=str(root/'probe-02'),started_at_ms=time.time()*1000,harness_revision='2207494e053af74c57da863991dc1e9d76c93591',runtime_identity='d03e01db77563e1da1d4b1b0da992727e7cc89196212b1eb91173ff700421bb7',probe_sha256=__import__('hashlib').sha256((root/'probe-04.py').read_bytes()).hexdigest(),device='/dev/nvme0n1p5',commit_delay_us=2000,commit_siblings=5),indent=2)+'\n')
monitor=HostMonitor(out,quiet_seconds=0).start();stop=threading.Event()
def host_io():
 # Match predecessor's extra host sampler coverage: only after eight minutes.
 if stop.wait(480):return
 total=0
 with (out/'host-io.jsonl').open('x') as stream:
  while not stop.is_set():
   row=observe();row['nvme_temperatures_millicelsius']={str(p):int(p.read_text()) for p in Path('/sys/class/nvme').glob('nvme*/hwmon*/temp*_input')}
   data=json.dumps(row,separators=(',',':'))+'\n';total+=len(data)
   if total>64*1024**2:raise RuntimeError('host diagnostic budget')
   stream.write(data);stream.flush();stop.wait(5)
thread=threading.Thread(target=host_io);thread.start()
try:
 result=subprocess.run(cmd)
 (out/'exit.json').write_text(json.dumps(dict(exit_code=result.returncode,at_ms=time.time()*1000))+'\n')
finally:stop.set();thread.join();monitor.close()
