import json,os,sys,tempfile,unittest
from pathlib import Path
os.umask(0o077)
sys.path[:0]=[str(Path('python/analytics').resolve()),str(Path('e2e/tpcds').resolve())]
import batch_profile as p
import capture_worker as w
import test_journal_owner as t
with tempfile.TemporaryDirectory() as directory:
 root=Path(directory);prof=root/'sync-profile';prof.mkdir();(prof/'enabled').touch()
 sys.argv=['capture_worker.py',str(root/'capture/worker/control.json')];p.install(w.__dict__,'capture')
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(t.OwnerTests)
 result=unittest.TextTestRunner(verbosity=1).run(suite)
 rows=[json.loads(line) for line in p.OUTPUT.read_text().splitlines()]
 assert any(r['stage']=='journal.selected' for r in rows)
 print('profile events',len(rows),'diagnostic_errors',p.ERRORS)
 p.OUTPUT=None
 if not result.wasSuccessful():raise SystemExit(1)
