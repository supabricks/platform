"""Opt-in whole-stack observations for diagnostic trials, including failures."""
from contextlib import closing
import gzip
import json
from pathlib import Path
import sqlite3
import re
import urllib.request
import threading
import time
import psutil

class TimedSQL:
    def __init__(self,db):self.db=db;self.times={}
    def execute(self,sql,*args,**kwargs):
        op=sql.split()[0].upper();label={'BEGIN':'begin','UPDATE':'updates','SELECT':'xid','COMMIT':'commit'}.get(op,'other')
        start=time.perf_counter_ns()
        try:return self.db.execute(sql,*args,**kwargs)
        finally:self.times[label]=(time.perf_counter_ns()-start)/1e6

class Profile:
    def __init__(self,cell,report):
        self.cell=cell;self.report=report;self.root=cell.root/'sync-profile';self.root.mkdir(mode=0o700)
        (self.root/'enabled').write_text('private diagnostic trial\n')
        self.stop=threading.Event();self.rows=[];self.errors=[];self.phase='setup';self.thread=None;self.monitor_ns=0;self.observer_metrics=dict(calls=0,wall_ns=0,errors=0);self.storage_status={};self.storage_urls={};self.pg_settings={}
        self.process_sample_errors=[];self.process_denials={}
        original=cell.transaction
        def transaction(db,key,value):
            timed=TimedSQL(db);result=original(timed,key,value);result['sql_ms']=timed.times;return result
        cell.transaction=transaction
    def watch_observer(self,observer):
        original=observer.poll
        def poll():
            start=time.perf_counter_ns()
            try:return original()
            except Exception:
                self.observer_metrics['errors']+=1;raise
            finally:
                self.observer_metrics['calls']+=1;self.observer_metrics['wall_ns']+=time.perf_counter_ns()-start
        observer.poll=poll
    def storage_metrics(self,url):
        with urllib.request.urlopen(url,timeout=.5) as response:data=response.read(1024*1024+1)
        if len(data)>1024*1024:raise ValueError('metrics response limit')
        values={}
        for line in data.decode().splitlines():
            if line.startswith('#'):continue
            match=re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})? ([^ ]+)(?: .*)?',line)
            if not match:continue
            name,labels,value=match.groups()
            if not any(x in name.lower() for x in ('wal','flush','fsync','disk','write')):continue
            bucket=re.search(r'(?:^|,)le="([0-9.eE+\-Inf]+)"',labels or '')
            key=name+('|le='+bucket.group(1) if bucket else '')
            number=float(value)
            if number!=number or number in (float('inf'),float('-inf')):continue
            values[key]=values.get(key,0)+number
        return values
    def start(self):
        for role,port in (('safekeeper','sk_http'),('pageserver','ps_http')):
            url=f"http://127.0.0.1:{self.cell.config['ports'][port]}/metrics"
            try:
                self.storage_metrics(url);self.storage_urls[role]=url;self.storage_status[role]='available; all labels except numeric histogram bounds aggregated'
            except Exception as error:self.storage_status[role]='unavailable:'+type(error).__name__
        self.thread=threading.Thread(target=self.run);self.thread.start()
    def processes(self):
        root=psutil.Process(self.cell.daemons[-1].pid);result=[]
        for p in [root,*root.children(recursive=True)]:
            role='other'
            try:
                cmd=p.cmdline();name=p.name();role='other'
                for needle,label in [('capture_worker.py','capture'),('incremental_worker.py','apply'),('export.py','bootstrap'),('postgres','postgres'),('safekeeper','safekeeper'),('pageserver','pageserver'),('supabricks','daemon'),('weed','object_store'),('java','catalog'),('sail','sail')]:
                    if any(needle in Path(x).name for x in cmd):role=label;break
                cpu=p.cpu_times();io=p.io_counters();switch=p.num_ctx_switches()
                context=Path(cmd[-1]).parent.name if cmd and role in ('capture','apply','bootstrap') else None
                if context and not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',context):context=None
                result.append(dict(pid=p.pid,created=p.create_time(),role=role,context_id=context,cpu_s=cpu.user+cpu.system,rss_bytes=p.memory_info().rss,read_bytes=io.read_bytes,write_bytes=io.write_bytes,voluntary_switches=switch.voluntary,involuntary_switches=switch.involuntary))
                self.process_denials.pop(p.pid,None)
            except psutil.NoSuchProcess:pass
            except psutil.AccessDenied:
                # Linux can deny /proc reads while a short-lived worker exits.
                # Retain the omission; persistent denials still invalidate profiling.
                count=self.process_denials.get(p.pid,0)+1;self.process_denials[p.pid]=count
                self.process_sample_errors.append(dict(at_ms=time.time()*1000,pid=p.pid,role=role,error='AccessDenied',consecutive=count))
                if p.pid==root.pid or count>=3 or len(self.process_sample_errors)>100:raise
        return result
    def run(self):
        try:
            with self.cell.source() as db:
                db.execute("SET application_name='sb-profile-monitor'");db.execute("SET statement_timeout='2s'")
                self.pg_settings={k:v for k,v in db.execute("SELECT name,setting FROM pg_settings WHERE name IN ('synchronous_commit','wal_sync_method','track_io_timing','track_wal_io_timing','fsync','full_page_writes')").fetchall()}
                last_resource=0
                while not self.stop.is_set():
                    start=time.perf_counter_ns();row=dict(at_ms=time.time()*1000,phase=self.report.get('phase','setup'))
                    row['pg_waits']=[dict(backend_type=a,state=b,wait_type=c,wait=d,count=n) for a,b,c,d,n in db.execute("SELECT backend_type,state,wait_event_type,wait_event,count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() GROUP BY 1,2,3,4").fetchall()]
                    if time.monotonic()-last_resource>=1:
                        row['processes']=self.processes()
                        row['storage_metrics']={}
                        for role,url in self.storage_urls.items():
                            try:row['storage_metrics'][role]=self.storage_metrics(url)
                            except Exception as error:row['storage_metrics'][role]={'observation_error':type(error).__name__}
                        row['pg_wal']=dict(zip(('records','full_page_images','bytes','buffer_full','writes','syncs','write_ms','sync_ms'),[float(x) for x in db.execute('SELECT wal_records,wal_fpi,wal_bytes,wal_buffers_full,wal_write,wal_sync,wal_write_time,wal_sync_time FROM pg_stat_wal').fetchone()]))
                        last_resource=time.monotonic()
                    self.rows.append(row);self.monitor_ns+=time.perf_counter_ns()-start
                    self.stop.wait(.2)
        except Exception as error:self.errors.append(type(error).__name__)
    def finish(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=5);assert not self.thread.is_alive(),'profile monitor did not stop'
    def collect(self,path):
        workers={}
        for p in self.root.glob('*.jsonl'):
            assert p.stat().st_size<8*1024*1024,'profile budget reached'
            rows=[json.loads(line) for line in p.read_text().splitlines()]
            assert all(not r.get('profile_write_errors') and not r.get('budget_exceeded') for r in rows),'profile write failure'
            if p.name!='daemon.jsonl':assert all(r.get('native_io') is not None for r in rows),'native sync hooks missing'
            workers[p.name]=rows
        with closing(sqlite3.connect(f'file:{self.cell.root}/state.sqlite3?mode=ro',uri=True)) as db:
            # Durable records retain all batch/publication timings, including failed runs.
            batches=[json.loads(r[0]) for r in db.execute('SELECT record FROM incremental_runs')]
            publications=[dict(ordinal=o,state=s,requested_at_ms=q,published_at_ms=p,descriptor=json.loads(d) if d else None) for o,s,q,p,d in db.execute('SELECT ordinal,state,requested_at_ms,published_at_ms,descriptor FROM publications')]
        # Export only numeric timing/cursor/work metrics, not source or connection metadata.
        batches=[{k:v for k,v in b.items() if k in ('id','state','created_at_ms','started_at_ms','finished_at_ms','deadline_ms','target_lsn','after_lsn','applied_lsn','error','attempts','journal_deferrals','journal_reads')} for b in batches]
        for p in publications:
            d=p.pop('descriptor') or {};m=d.get('manifest',{})
            p.update(export_id=d.get('export_id'),prepared_at_ms=d.get('prepared_at_ms'),input_bytes=m.get('input_bytes'),apply_metrics=m.get('apply_metrics'),generation_bytes=m.get('generation_bytes'),retained_bytes=m.get('retained_bytes'),file_count=len(m.get('files',[])),table_count=len(m.get('tables',[])))
        # Persist partial observations before rejecting a monitor failure, so an
        # invalid attempt remains diagnosable without exposing private logs.
        data=dict(scope='Diagnostic timing; inclusive spans overlap. SQL text, row values, credentials and exception messages omitted.',workers=workers,observations=self.rows,monitor_wall_ns=self.monitor_ns,observer_metrics=self.observer_metrics,pg_settings=self.pg_settings,storage_status=self.storage_status,batches=batches,publications=publications,monitor_errors=self.errors,process_sample_errors=self.process_sample_errors)
        path.write_bytes(gzip.compress((json.dumps(data,separators=(',',':'))+'\n').encode(),mtime=0))
        assert not self.errors,self.errors
        assert 'daemon.jsonl' in workers and any(k.startswith('capture-') for k in workers) and any(k.startswith('incremental-') for k in workers),'missing workflow profile'
        assert workers['daemon.jsonl'][-1].get('final'),'daemon profile missing final snapshot'
        return dict(path=path.name,workers=len(workers),observations=len(self.rows),monitor_wall_ns=self.monitor_ns)
