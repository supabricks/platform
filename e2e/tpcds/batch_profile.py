"""Opt-in diagnostic overlay for EQ230; no row values, SQL or authority changes.

Separate bounded process streams; no extra journal queries and no fsync. Capture
transaction frame counts are observed only AFTER successful durable append.
"""
import atexit
import functools
import json
import os
from pathlib import Path
import resource
import struct
import sys
import threading
import time

LIMIT=8*1024*1024
OUTPUT=None;ROLE=None;WRITTEN=0;DROPPED=0;ERRORS=0;WRITE_NS=0;OBSERVE_NS=0
LOCK=threading.RLock();LOCAL=threading.local();REQUEST_INDEX=0


def event(stage,**fields):
    global WRITTEN,DROPPED,ERRORS,WRITE_NS
    if OUTPUT is None:return
    started=time.perf_counter_ns()
    with LOCK:
        try:
            data=(json.dumps(dict(stage=stage,at_ms=time.time_ns()/1e6,pid=os.getpid(),
                id=getattr(LOCAL,'request',None),fields=fields),separators=(',',':'))+'\n').encode()
            if len(data)>65536 or WRITTEN+len(data)>LIMIT:DROPPED+=1;return
            fd=os.open(OUTPUT,os.O_CREAT|os.O_APPEND|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'ab') as stream:stream.write(data)
            WRITTEN+=len(data)
        except OSError:ERRORS+=1
        finally:WRITE_NS+=time.perf_counter_ns()-started


def counters():
    return dict(bytes=WRITTEN,dropped=DROPPED,errors=ERRORS,write_ns=WRITE_NS,observe_ns=OBSERVE_NS)


def frame_rows(payload):
    # Already validated framing; skip field contents without decoding values.
    pos=0;rows=0
    while pos<len(payload):
        if pos+4>len(payload):raise ValueError('diagnostic framing')
        size=struct.unpack_from('!I',payload,pos)[0];pos+=4
        if size<1 or pos+size>len(payload):raise ValueError('diagnostic framing')
        rows+=payload[pos] in (73,85,68);pos+=size
    return rows


def wrap(function,stage):
    @functools.wraps(function)
    def measured(*args,**kwargs):
        start=time.perf_counter_ns();cpu=time.process_time_ns();ok=False
        try:
            result=function(*args,**kwargs);ok=True;return result
        finally:event(stage,wall_ns=time.perf_counter_ns()-start,cpu_ns=time.process_time_ns()-cpu,ok=ok)
    return measured


def install(namespace,role):
    global OUTPUT,ROLE
    if len(sys.argv)<2:return
    path=Path(sys.argv[1]);root=next((p for p in list(path.parents)[:6] if (p/'sync-profile/enabled').is_file()),None)
    if root is None:return
    ROLE=role;OUTPUT=root/'sync-profile'/f'batches-{role}-{os.getpid()}.jsonl'
    if role=='capture':
        from capture.spool import Spool
        from capture import owner
        original=Spool.append_many
        def append(self,transactions):
            global OBSERVE_NS,ERRORS
            started=time.time_ns()/1e6;timer=time.perf_counter_ns()
            result=original(self,transactions);durable=time.time_ns()/1e6;wall=time.perf_counter_ns()-timer
            timer=time.perf_counter_ns()
            try:
                inserted=transactions[-result['transactions']:] if result['transactions'] else []
                records=[dict(commit=commit,end=end,bytes=len(payload),rows=frame_rows(payload),committed_at_ms=946684800000+struct.unpack('!q',payload[-8:])[0]/1000)
                    for commit,end,payload in inserted]
                event('capture.durable',started_ms=started,durable_ms=durable,wall_ns=wall,
                    end=result['captured_lsn'],records=records)
            except (ValueError,IndexError,struct.error):ERRORS+=1
            finally:OBSERVE_NS+=time.perf_counter_ns()-timer
            return result
        Spool.append_many=append
        atomic=namespace['atomic']
        def report(path,value):
            result=atomic(path,value)
            if Path(path).name=='status.json':
                p=value.get('progress') or {};g=p.get('capture_groups') or {}
                event('capture.report',observed_at_ms=value.get('observed_at_ms'),captured_lsn=value.get('captured_lsn'),
                    source_lsn=value.get('source_lsn'),decoded_lsn=g.get('decoded_lsn'),pending_transactions=g.get('pending_transactions'),
                    published_lsn=p.get('published_lsn'),backlog_bytes=p.get('backlog_bytes'),observer=counters())
            return result
        namespace['atomic']=report
        materialize=owner.materialize
        def read(view,config,*args,**kwargs):
            # Proxy the existing metadata read; never prolong the snapshot with
            # a shadow query, parse extra payloads or affect the authorized cut.
            observed={}
            class View:
                def metadata(self):
                    result=view.metadata();observed['captured']=result['captured'];return result
                def records(self,*a):return view.records(*a)
            start=time.time_ns()/1e6;timer=time.perf_counter_ns()
            result=materialize(View(),config,*args,**kwargs)
            LOCAL.selected=dict(request=config['id'],attempt=config['attempt'],started_ms=start,selected_at_ms=time.time_ns()/1e6,
                after_lsn=config['after_lsn'],target_lsn=config['target_lsn'],durable_lsn=observed.get('captured'),
                selected_end=result[2],selected_bytes=result[3],selected_transactions=len(result[1]),wall_ns=time.perf_counter_ns()-timer)
            return result
        owner.materialize=read
        handle=owner.Owner.handle
        def serve(self,connection):
            LOCAL.selected=None
            try:return handle(self,connection)
            finally:
                selected=LOCAL.selected;LOCAL.selected=None
                if selected is not None:event('journal.selected',**selected)
        owner.Owner.handle=serve
    elif role=='incremental':
        from incremental import storage,maintenance,reuse
        for name in ('journal','verify_previous','plan','apply_table','inventory','durable','boundary','retained_boundary'):
            if name in namespace:namespace[name]=wrap(namespace[name],'worker.'+name)
        original_decode=namespace['decode']
        def decode(config,data,*args,**kwargs):
            start=time.perf_counter_ns();result=original_decode(config,data,*args,**kwargs)
            event('worker.decoded',end=result.end,rows=len(result.operations),transactions=result.transactions,
                input_bytes=result.input_bytes,journal_transactions=len(data[1]),journal_bytes=data[3],journal_end=data[2],
                wall_ns=time.perf_counter_ns()-start)
            return result
        namespace['decode']=decode
        original_batches=namespace['key_batches']
        def batches(*args,**kwargs):
            start=time.perf_counter_ns();rows=0
            for batch in original_batches(*args,**kwargs):rows+=batch.num_rows;yield batch
            event('worker.key_scan',rows=rows,wall_ns=time.perf_counter_ns()-start)
        namespace['key_batches']=batches
        original=namespace['run']
        def run(config,*args,**kwargs):
            global REQUEST_INDEX
            REQUEST_INDEX+=1;LOCAL.request=config['id'];start=time.perf_counter_ns();cpu=time.process_time_ns()
            event('worker.start',attempt=config['attempt'],after_lsn=config['after_lsn'],target_lsn=config['target_lsn'],request_index=REQUEST_INDEX)
            try:return original(config,*args,**kwargs)
            finally:
                event('worker.end',wall_ns=time.perf_counter_ns()-start,cpu_ns=time.process_time_ns()-cpu,
                    maxrss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),observer=counters())
                LOCAL.request=None
        namespace['run']=run
        atomic=reuse.atomic
        def done(path,value):
            result=atomic(path,value)
            if Path(path).name=='done.json':event('worker.done',request=value['id'],attempt=value['attempt'],reusable=value['reusable'],observer=counters())
            return result
        reuse.atomic=done
    event('profile.start',role=role)
    atexit.register(lambda:event('profile.end',role=role,observer=counters()))
