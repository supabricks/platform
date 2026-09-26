"""Private diagnostic-package profiler; never records SQL text or row values.

Installed only by profile_package.py. Fixed-label, nested wall-time histograms,
periodic cumulative snapshots, and safe exception stacks. No durability changes.
"""
import atexit
import ctypes
import functools
import json
import os
from pathlib import Path
import resource
import re
import sqlite3
import sys
import threading
import time
import traceback

IMPORTED_NS=time.perf_counter_ns()
METRICS={};WORK={};ERRORS=[];LOCK=threading.RLock();LOCAL=threading.local()
ENABLED=False;OUTPUT=None;ROLE=None;CONTEXT_ID=None;START_MS=time.time()*1000;WRITTEN=0;WRITE_NS=0;WRITE_ERRORS=0;STOP=threading.Event()
BUDGET=8*1024*1024
NATIVE=None

def native_io():
    if NATIVE is None:return None
    values=(ctypes.c_uint64*8)();NATIVE(values)
    return {name:dict(zip(('calls','total_ns','max_ns','errors'),values[i*4:i*4+4])) for i,name in enumerate(('fsync','fdatasync'))}



def error_record(error):
    return dict(type=type(error).__name__,sqlite_errorcode=getattr(error,'sqlite_errorcode',None),
        sqlite_errorname=getattr(error,'sqlite_errorname',None),
        stack=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_tb(error.__traceback__)[-12:]])


def record(name,elapsed,children=0,error=None):
    with LOCK:
        m=METRICS.setdefault(name,dict(calls=0,total_ns=0,self_ns=0,max_ns=0,errors=0,hist_us_log2={}))
        m['calls']+=1;m['total_ns']+=elapsed;m['self_ns']+=max(0,elapsed-children);m['max_ns']=max(m['max_ns'],elapsed)
        bucket=max(0,(max(1,elapsed//1000)).bit_length()-1)
        m['hist_us_log2'][bucket]=m['hist_us_log2'].get(bucket,0)+1
        if error is not None:
            m['errors']+=1
            if len(ERRORS)<16:ERRORS.append(dict(stage=name,at_ms=time.time()*1000,**error_record(error)))


class Span:
    def __init__(self,name):self.name=name
    def __enter__(self):
        self.start=time.perf_counter_ns();self.children=0
        if not hasattr(LOCAL,'stack'):LOCAL.stack=[]
        LOCAL.stack.append(self);return self
    def __exit__(self,kind,error,tb):
        elapsed=time.perf_counter_ns()-self.start
        LOCAL.stack.pop()
        if LOCAL.stack:LOCAL.stack[-1].children+=elapsed
        record(self.name,elapsed,self.children,error)


def wrap(function,name):
    @functools.wraps(function)
    def measured(*args,**kwargs):
        if name=='durability.atomic' and Path(args[0]).name=='result.json':flush()
        try:
            with Span(name):result=function(*args,**kwargs)
        except BaseException:
            if name=='apply.run':
                # Full exception stays in the private worker log, never the exported trace.
                traceback.print_exc();flush()
            raise
        values={}
        if name=='capture.spool.append' and result:
            if isinstance(result,dict):
                values=dict(transactions=result['transactions'],payload_bytes=result['payload_bytes'],groups=int(result['transactions']>0))
            else:values=dict(transactions=1,payload_bytes=len(args[3]),groups=1)
        elif name=='capture.groups.flush' and result:
            values={key:result[key] for key in ('accumulation_ms','commit_ms')}
        elif name=='apply.journal':values=dict(transactions=len(result[1]),input_bytes=result[3])
        elif name=='apply.delta_merge':
            values={k:float(v) for k,v in result.items() if k in ('execution_time_ms','scan_time_ms','rewrite_time_ms','num_source_rows','num_target_rows_updated','num_target_rows_inserted','num_target_rows_deleted','num_target_files_added','num_target_files_removed')}
        if values:
            with LOCK:
                for key,value in values.items():
                    metric=WORK.setdefault(name+'.'+key,dict(count=0,total=0,maximum=0))
                    metric['count']+=1;metric['total']+=value;metric['maximum']=max(metric['maximum'],value)
        return result
    return measured


def flush(final=False):
    global WRITTEN,WRITE_NS,WRITE_ERRORS
    if not ENABLED:return
    start=time.perf_counter_ns()
    try:
        with LOCK:
            usage=resource.getrusage(resource.RUSAGE_SELF)
            row=dict(role=ROLE,context_id=CONTEXT_ID,pid=os.getpid(),started_at_ms=START_MS,at_ms=time.time()*1000,final=final,
                metrics=METRICS,work=WORK,native_io=native_io(),exceptions=ERRORS,cpu_user_s=usage.ru_utime,cpu_system_s=usage.ru_stime,
                maxrss_kib=usage.ru_maxrss,voluntary_switches=usage.ru_nvcsw,involuntary_switches=usage.ru_nivcsw,
                profile_write_ns=WRITE_NS,profile_write_errors=WRITE_ERRORS,budget_exceeded=WRITTEN>=BUDGET)
            data=(json.dumps(row,separators=(',',':'))+'\n').encode()
            if WRITTEN>=BUDGET:return
            fd=os.open(OUTPUT,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
            try:
                with os.fdopen(fd,'ab') as stream:stream.write(data)
            finally:pass
            WRITTEN+=len(data)
    except OSError:WRITE_ERRORS+=1
    finally:WRITE_NS+=time.perf_counter_ns()-start


def sql_label(sql):
    # Only return fixed labels, never a statement, literal, parameter or source name.
    words=str(sql).upper().split();op=words[0] if words else 'OTHER'
    if op not in ('SELECT','INSERT','UPDATE','DELETE','BEGIN','COMMIT','ROLLBACK','PRAGMA'):op='OTHER'
    return 'sqlite.'+op


class Connection(sqlite3.Connection):
    def execute(self,sql,*args,**kwargs):
        label=sql_label(sql);before=native_io() if label=='sqlite.COMMIT' else None
        try:
            with Span(label):return super().execute(sql,*args,**kwargs)
        finally:
            if before is not None:
                after=native_io()
                with LOCK:
                    for name in ('fsync','fdatasync'):
                        for field in ('calls','total_ns','errors'):
                            value=after[name][field]-before[name][field]
                            key='sqlite.COMMIT.'+name+'.'+field
                            metric=WORK.setdefault(key,dict(count=0,total=0,maximum=0))
                            metric['count']+=1;metric['total']+=value;metric['maximum']=max(metric['maximum'],value)
    def executescript(self,sql,*args,**kwargs):
        with Span('sqlite.script'):return super().executescript(sql,*args,**kwargs)


def install(namespace,role):
    global ENABLED,OUTPUT,ROLE,NATIVE,CONTEXT_ID
    if len(sys.argv)<2:return
    path=Path(sys.argv[1])
    root=next((p for p in list(path.parents)[:6] if (p/'sync-profile/enabled').is_file()),None)
    if root is None:return
    try:
        NATIVE=ctypes.CDLL(None).sb_profile_io_snapshot
        NATIVE.argtypes=[ctypes.POINTER(ctypes.c_uint64)];NATIVE.restype=None
    except AttributeError:pass  # Unit tests can exercise Python hooks alone.
    context=path.parent.name
    CONTEXT_ID=context if re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',context) else None
    ENABLED=True;ROLE=role;OUTPUT=root/'sync-profile'/f'{role}-{os.getpid()}.jsonl'
    record('startup.imports',time.perf_counter_ns()-IMPORTED_NS)
    original_connect=sqlite3.connect
    def connect(*args,**kwargs):
        kwargs.setdefault('factory',Connection)
        with Span('sqlite.connect'):return original_connect(*args,**kwargs)
    sqlite3.connect=connect
    os.fsync=wrap(os.fsync,'durability.fsync')
    targets=[]
    if role=='capture':
        from capture.spool import Spool
        from capture.source import Source
        from capture.protocol import Decoder,Wire
        append_name='append_many' if hasattr(Spool,'append_many') else 'append'
        targets.append((Spool,append_name,'capture.spool.append'))
        try:
            from capture.groups import Groups
        except ImportError:pass  # Predecessor has only one-transaction appends.
        else:targets.append((Groups,'flush','capture.groups.flush'))
        for cls,names,prefix in ((Spool,('prune','progress','verify'),'capture.spool'),(Source,('setup','check'),'capture.source'),(Wire,('receive','feedback'),'capture.wire'),(Decoder,('feed',),'capture.decode')):
            for name in names:targets.append((cls,name,prefix+'.'+name))
        # Bound capture loop and blocking socket wait separately.
        namespace['select'].select=wrap(namespace['select'].select,'capture.socket_wait')
    if role=='incremental':
        from incremental import storage,maintenance
        for mod,names,prefix in ((storage,('journal','initialize','verify_previous','inventory','durable','boundary','retained_boundary','digest'),'apply'),(maintenance,('base','compact'),'apply.maintenance')):
            for name in names:
                if hasattr(mod,name):targets.append((mod,name,prefix+'.'+name))
        for name in ('plan','apply_table','schema_for','commit_metrics','run'):
            namespace[name]=wrap(namespace[name],'apply.'+name)
        from deltalake.table import TableMerger
        targets.append((TableMerger,'execute','apply.delta_merge'))
    if role=='export':
        for name in ('discover','export','boundary','disk_bytes'):
            namespace[name]=wrap(namespace[name],'bootstrap.'+name)
    # Replace imported aliases too, so nested stages remain attributable.
    import capture.spool as spool
    targets.append((spool,'atomic','durability.atomic'))
    for owner,name,label in targets:
        original=getattr(owner,name);wrapped=wrap(original,label);setattr(owner,name,wrapped)
        for key,value in list(namespace.items()):
            if value is original:namespace[key]=wrapped
        if role=='incremental':
            for mod in (storage,maintenance):
                for key,value in list(vars(mod).items()):
                    if value is original:setattr(mod,key,wrapped)
    flush()
    def writer():
        while not STOP.wait(1):flush()
    threading.Thread(target=writer,daemon=True).start()
    def finish():STOP.set();flush(final=True)
    atexit.register(finish)
