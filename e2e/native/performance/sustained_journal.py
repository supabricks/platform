#!/usr/bin/env python3
"""SP10c 30-minute fixture: unchanged paced trial, bounded resource sampling.

Main profiler stays off to avoid its short-trial 8 MiB stream budget. The same
independent resource sampler runs for every arm. Tail latency still uses exact
transaction markers/publications and final equality from trial.py.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sqlite3
from contextlib import closing
import threading
import time
import psutil
import trial


class Sustained(trial.InstalledContinuous):
    def resource_details(self):
        return {}

    def setup_source(self,*args,**kwargs):
        super().setup_source(*args,**kwargs)
        self.sample_stop=threading.Event();self.sample_errors=[];self.sample_denials={}
        self.sample_thread=threading.Thread(target=self.sample);self.sample_thread.start()

    def sample(self):
        total=0
        try:
            with self.resource_output.open('x') as stream:
                while not self.sample_stop.is_set():
                    root=psutil.Process(self.daemons[-1].pid);values=[]
                    for process in [root,*root.children(recursive=True)]:
                        try:
                            with process.oneshot():
                                command=process.cmdline();role='other'
                                for needle,label in [('capture_worker.py','capture'),('incremental_worker.py','apply'),('export.py','bootstrap'),('postgres','postgres'),('safekeeper','safekeeper'),('pageserver','pageserver'),('supabricks','daemon'),('weed','object_store'),('java','catalog'),('sail','sail')]:
                                    if any(needle in Path(v).name for v in command):role=label;break
                                cpu=process.cpu_times();io=process.io_counters()
                                values.append(dict(pid=process.pid,created=process.create_time(),role=role,
                                    cpu_seconds=cpu.user+cpu.system,rss_bytes=process.memory_info().rss,
                                    read_bytes=io.read_bytes,write_bytes=io.write_bytes))
                            self.sample_denials.pop(process.pid,None)
                        except psutil.NoSuchProcess:pass
                        except psutil.AccessDenied:
                            count=self.sample_denials.get(process.pid,0)+1;self.sample_denials[process.pid]=count
                            values.append(dict(pid=process.pid,error='AccessDenied',consecutive=count))
                            if count>=3:raise
                    physical=allocated=0
                    for path in (self.root/'capture').glob('*/spool/**/*'):
                        try:
                            if path.is_file():
                                info=path.stat();physical+=info.st_size;allocated+=info.st_blocks*512
                        except FileNotFoundError:pass
                    row=dict(at_ms=time.time()*1000,processes=values,spool_bytes=physical,spool_allocated_bytes=allocated)
                    row.update(self.resource_details())
                    data=json.dumps(row,separators=(',',':'))+'\n';total+=len(data)
                    if total>64*1024**2:raise RuntimeError('resource_sample_budget')
                    stream.write(data);stream.flush();self.sample_stop.wait(1)
        except BaseException as error:self.sample_errors.append(type(error).__name__)

    def stop(self):
        if hasattr(self,'sample_thread'):
            self.sample_stop.set();self.sample_thread.join(timeout=5)
            assert not self.sample_thread.is_alive() and not self.sample_errors,self.sample_errors
        super().stop()
        # No live second RocksDB process. Reopen only after capture and all owned
        # descendants have stopped, and run the production retained-chain verifier.
        code='''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from capture.spool import Spool
root=Path(sys.argv[2]);out=[]
for control in root.glob('capture/*/control.json'):
 config=json.loads(control.read_text())
 spool=Spool(control.parent/'spool',config['identity'],config['spool_bytes'])
 try:
  spool.verify()
  out.append(dict(captured=spool.captured,prefix_lsn=spool.prefix()['lsn'],retained_bytes=spool.get('bytes'),physical_bytes=spool.physical()))
 finally:spool.close()
assert out
print(json.dumps(out))
'''
        result=subprocess.run([str(self.release/'python/analytics/python'),'-I','-B','-c',code,
            str(self.release/'python/analytics'),str(self.root)],capture_output=True,text=True,timeout=60)
        assert result.returncode==0,result.stderr
        self.reopen_output.write_text(result.stdout)
        # trial.py removes a successful private fixture. Export counts after
        # shutdown, before that cleanup, instead of requiring the deleted DB.
        with closing(sqlite3.connect(f'file:{self.root}/state.sqlite3?mode=ro',uri=True)) as db:
            counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                    for table in ('incremental_runs','incremental_requests','sync_runs','publications','snapshots')}
            counts['published']=db.execute("SELECT count(*) FROM publications WHERE state='published'").fetchone()[0]
            assert not db.execute('PRAGMA foreign_key_check').fetchall(),'catalog foreign-key violation'
        self.reopen_output.with_name('history.json').write_text(json.dumps(dict(counts=counts,foreign_keys='passed'),indent=2)+'\n')


def run(args):
    if args.seconds<1800 and not args.screen:raise ValueError('sustained fixture requires at least 30 minutes')
    Sustained.resource_output=args.report.with_name('resources.jsonl')
    Sustained.reopen_output=args.report.with_name('reopen.json')
    trial.InstalledContinuous=Sustained
    return trial.trial(args)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('release','report','scratch'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--rate',type=int,default=1250);p.add_argument('--seconds',type=int,default=1800)
    p.add_argument('--baseline',type=int,default=5);p.add_argument('--warmup',type=int,default=60)
    p.add_argument('--clients',type=int,default=8);p.add_argument('--rows',type=int,default=10000)
    p.add_argument('--screen',action='store_true',help='plumbing check only; excluded from sustained qualification')
    p.set_defaults(profile=False)
    raise SystemExit(run(p.parse_args()))
