import sys,json,shutil,time,argparse,threading
from pathlib import Path
sys.path.insert(0,'/repo/e2e/native/performance')
import sp11_trial
from profile_trial import Profile,TimedSQL
from trial import counters
Base=sp11_trial.Steady
class Probe(Base):
 def setup_source(self,*args,**kw):
  folder=self.root/'sync-profile';folder.mkdir();(folder/'enabled').touch()
  self.last_probe=0;self.stats={};self.stats_lock=threading.Lock()
  super().setup_source(*args,**kw)
 def transaction(self,db,key,value):
  timed=TimedSQL(db);result=super().transaction(timed,key,value)
  bucket=int(result['ack_ms']//60000)
  with self.stats_lock:
   row=self.stats.setdefault(bucket,dict(count=0,sql_total_ms={},sql_max_ms={}))
   row['count']+=1
   for k,v in timed.times.items():
    row['sql_total_ms'][k]=row['sql_total_ms'].get(k,0)+v
    row['sql_max_ms'][k]=max(row['sql_max_ms'].get(k,0),v)
  return result
 def resource_details(self):
  row=super().resource_details()
  if time.monotonic()-self.last_probe>=5 and hasattr(self,'parent'):
   with self.source() as db:
    db.execute("SET statement_timeout='2s'")
    row['pg_waits']=[dict(backend_type=a,state=b,wait_type=c,wait=d,count=n) for a,b,c,d,n in db.execute("SELECT backend_type,state,wait_event_type,wait_event,count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() GROUP BY 1,2,3,4").fetchall()]
    row['pg_wal']=dict(zip(('records','fpi','bytes','buffer_full','writes','syncs','write_ms','sync_ms'),map(float,db.execute('SELECT wal_records,wal_fpi,wal_bytes,wal_buffers_full,wal_write,wal_sync,wal_write_time,wal_sync_time FROM pg_stat_wal').fetchone())))
    row['pg_tables']=[dict(table=a,live=b,dead=c,updates=d,hot=e,autovacuum=f) for a,b,c,d,e,f in db.execute("SELECT relname,n_live_tup,n_dead_tup,n_tup_upd,n_tup_hot_upd,autovacuum_count FROM pg_stat_user_tables ORDER BY relname").fetchall()]
   row['storage_metrics']={role:Profile.storage_metrics(self,f"http://127.0.0.1:{self.config['ports'][port]}/metrics") for role,port in [('safekeeper','sk_http'),('pageserver','ps_http')]}
   row['cgroup']=counters()
   with self.stats_lock:row['sql_buckets']=json.loads(json.dumps(self.stats))
   self.last_probe=time.monotonic()
  return row
 def stop(self):
  super().stop()
  if (self.root/'sync-profile/daemon.jsonl').exists():shutil.copyfile(self.root/'sync-profile/daemon.jsonl',Path('/reports/daemon.jsonl'))
sp11_trial.Steady=Probe
args=argparse.Namespace(release=Path('/release'),report=Path('/reports/result.json'),scratch=Path('/reports'),rate=1250,seconds=1500,baseline=5,warmup=60,clients=8,rows=10000,screen=True,profile=False)
raise SystemExit(sp11_trial.run(args))
