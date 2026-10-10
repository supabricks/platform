import datetime,json,subprocess
from pathlib import Path
root=Path('/data2/supabricks-eq/eq236/u');assert not root.exists()
release=Path('build/eq236/programs/releases/v0.1.0-alpha.36.eq236cache').resolve();binary=release/'bin/supabricks'
report=dict(status='RUNNING',started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),checks=[])
try:
 p=subprocess.run([str(binary),'up','--data-dir',str(root),'--compute-cache-profile','source-load'],capture_output=True,text=True,timeout=90)
 assert p.returncode==0,p.stdout+p.stderr
 assert json.loads((root/'runtime.json').read_text())['compute_cache_profile']=='source-load'
 report['checks'].append('fresh_up_forwards_explicit_profile_to_daemon')
 p=subprocess.run([str(binary),'up','--data-dir',str(root)],capture_output=True,text=True,timeout=90)
 assert p.returncode==0,p.stdout+p.stderr
 report['checks'].append('up_without_flag_reconnects_to_persisted_profile')
 report['status']='PASS'
finally:
 p=subprocess.run([str(binary),'down','--data-dir',str(root)],capture_output=True,text=True,timeout=90)
 assert p.returncode==0,p.stdout+p.stderr
 report['stopped']=True;report['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
 (root/'qualification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
