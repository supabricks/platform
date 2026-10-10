import json,os,sys,tempfile,time
os.umask(0o077)
from pathlib import Path
sys.path[:0]=[str(Path('python/analytics').resolve()),str(Path('e2e/tpcds').resolve())]
import batch_profile as p
import incremental_worker as w
import test_incremental as f
fixture=f.IncrementalTests();fixture.setUp()
try:
 root=fixture.root;prof=root/'sync-profile';prof.mkdir(mode=0o700);(prof/'enabled').touch()
 sys.argv=['incremental_worker.py',str(root/'mailbox/input.json')];p.install(w.__dict__,'incremental')
 fixture.spool.append(280,300,f.tx(280,300,f.change(b'I',42,new=[2,None,'secret-row-must-not-appear'])))
 config=fixture.config_next('0/12C');config['attempt']=1;w.run(config)
 text=p.OUTPUT.read_text();rows=[json.loads(line) for line in text.splitlines()]
 assert 'secret-row-must-not-appear' not in text
 assert any(r['stage']=='worker.decoded' and r['fields']['rows']==1 for r in rows)
 assert any(r['stage']=='worker.end' and r['id']==config['id'] for r in rows)
 assert json.loads((Path(config['workspace'])/'result.json').read_text())['state']=='ready'
 print('PASS real apply: exact changed row, request correlation, no source values')
finally:
 p.OUTPUT=None;fixture.tearDown()
