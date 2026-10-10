import json,subprocess
from pathlib import Path
repo=Path.cwd();base=Path('<evidence-root>/eq220');image=next(v for v in json.loads((base/'c228a-control/launch.json').read_text())['command'] if v.startswith('sha256:'))
# Assert both timed cells have stopped before any correctness workload starts.
for label in ('c228a','c228b'):
 launch=json.loads((base/(label+'-control')/'launch.json').read_text());assert launch['status']=='FINISHED' and launch['exit_code']==0
 assert json.loads((base/label/'result.json').read_text())['status']=='PREFIX_PASS'
for label,release in [('c228a','v0.1.0-alpha.36.eq228a'),('c228b','v0.1.0-alpha.36.eq226serial')]:
 command=['docker','run','--rm','--name','eq228-verify-'+label,'--network','none','--user','1000:1000','--cpuset-cpus','0-7','--memory','16g','--memory-swap','16g','-v',f'{repo}:{repo}:ro','-v',f'{base}:{base}','-w',str(repo),image,'python3','install/native/catalog_gate.py','--timeout','3600','--data-root',str(base/label/'state'),'--report',str(base/(label+'-control')/'verify-cleanup.json'),'--','python3','e2e/tpcds/verify.py','--workload','sf100-growing-prefix','--release',str(repo/'build/eq220/programs/releases'/release),'--inputs',str(repo/'build/eq00-20261006/inputs'),'--load',str(base/label),'--output',str(base/('v'+label))]
 with Path('build/eq228',label+'-verify.log').open('x') as log:subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
 print(json.dumps(dict(label=label,result=json.loads((base/('v'+label)/'result.json').read_text())['status'])),flush=True)
