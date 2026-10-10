import json,os,subprocess,time
from pathlib import Path
root=Path(__file__).resolve().parent
results=[]
for mib in (128,640,896):
 for repeat in range(3):
  for label in (('baseline','scheduling') if repeat%2==0 else ('scheduling','baseline')):
   log=root/f'scheduling-{mib}-{repeat}-{label}.log'
   env=dict(os.environ,SB_PUBLICATION_BENCH_MIB=str(mib),SB_PUBLICATION_BENCH_BINARY=str(root/(label+'-supabricks')))
   command=['taskset','-c','0-7','target/debug/deps/analytics-cdaaf7692fe26d0c','--exact','daemon_streams_large_publication_and_rejects_tail_corruption','--nocapture']
   before=time.monotonic()
   with log.open('x') as stream:code=subprocess.run(command,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=180).returncode
   measures=[json.loads(line.split('publication_measurement ',1)[1]) for line in log.read_text().splitlines() if line.startswith('publication_measurement ')]
   results.append(dict(mib=mib,repeat=repeat,candidate=label,code=code,elapsed_seconds=time.monotonic()-before,measurements=measures))
   (root/'scheduling-measurements.json').write_text(json.dumps(results,indent=2)+'\n')
   if code:raise RuntimeError(str(log))
