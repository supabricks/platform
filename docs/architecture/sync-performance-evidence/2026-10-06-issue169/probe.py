import sys,json,shutil,time,argparse
from pathlib import Path
sys.path.insert(0,'/repo/e2e/native/performance')
import sp11_trial
Base=sp11_trial.Steady
class Probe(Base):
 def setup_source(self,*args,**kw):
  folder=self.root/'sync-profile';folder.mkdir();(folder/'enabled').touch()
  self.last_probe=0
  super().setup_source(*args,**kw)
 def resource_details(self):
  row=super().resource_details()
  if time.monotonic()-self.last_probe>=5 and hasattr(self,'parent'):
   with self.source() as db:
    db.execute("SET statement_timeout='2s'")
    row['pg_waits']=[dict(backend_type=a,wait_type=b,wait=c,count=n) for a,b,c,n in db.execute("SELECT backend_type,wait_event_type,wait_event,count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() GROUP BY 1,2,3").fetchall()]
   self.last_probe=time.monotonic()
  return row
 def stop(self):
  super().stop()
  if (self.root/'sync-profile/daemon.jsonl').exists():shutil.copyfile(self.root/'sync-profile/daemon.jsonl',Path('/reports/daemon.jsonl'))
  if (self.root/'state.sqlite3').exists():
   p=Path('/reports/private-state.sqlite3');shutil.copyfile(self.root/'state.sqlite3',p);p.chmod(0o600)
sp11_trial.Steady=Probe
args=argparse.Namespace(release=Path('/release'),report=Path('/reports/result.json'),scratch=Path('/reports'),rate=1250,seconds=1800,baseline=5,warmup=60,clients=8,rows=10000,screen=True,profile=False)
raise SystemExit(sp11_trial.run(args))
