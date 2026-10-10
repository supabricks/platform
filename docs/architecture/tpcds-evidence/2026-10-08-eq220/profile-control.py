"""Paired control-file polling cost; no source, journal or publication activity."""
import gc,hashlib,json,sys,time
from pathlib import Path
release=Path(sys.argv[1]).resolve();source=Path(sys.argv[2]);output=Path(sys.argv[3]);output.mkdir(mode=0o700)
sys.path.insert(0,str(release/'python/analytics'))
import capture_worker as worker
payload=source.read_bytes();control=output/'control.json';control.write_bytes(payload);control.chmod(0o600)
expected=json.loads(payload);times=[]
for repeat in range(3):
    read=worker.ControlReader(control).read if hasattr(worker,'ControlReader') else lambda:worker.read(control)
    gc.collect();start=time.monotonic()
    for _ in range(100000):value=read()
    times.append(time.monotonic()-start);assert value==expected
control.unlink()
(output/'result.json').write_text(json.dumps(dict(status='PASS',scope='Isolated unchanged control-file polling; no capture/Delta/publication throughput claim',reads_per_sample=100000,samples_seconds=times,control_bytes=len(payload),control_sha256=hashlib.sha256(payload).hexdigest(),release_identity=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),exact_control=True),indent=2)+'\n')
