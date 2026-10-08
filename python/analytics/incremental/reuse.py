"""Bounded, serial daemon mailbox. Only imported code survives between requests.

The daemon owns authorization, cancellation and process-group fencing. A mailbox
is private to one launch; it is never recovered across daemon generations.
"""
import gc
import resource
import sys
import time
from pathlib import Path
from capture.spool import CaptureError, atomic, canonical
from incremental.storage import read_json

MAX_REQUESTS=64
# Daemon retires at 60 s / 5 s idle. These independent hard stops leave a
# margin for its 200 ms controller tick; neither extends an in-flight deadline.
MAX_AGE_SECONDS=65
IDLE_SECONDS=6
RECYCLE_BYTES=512*1024*1024


def scope(config):
    return canonical({key:config.get(key) for key in (
        'identity','worker_generation','source_revision','storage_generation','storage_profile',
        'generation','spool','bootstrap_id','bootstrap_manifest','bootstrap_lsn',
        'reuse_authority')})


def serve(mailbox, execute):
    mailbox=Path(mailbox)
    born=time.monotonic();last=born;seen=set();bound=None;signature=None;last_request=None
    while len(seen)<MAX_REQUESTS and time.monotonic()-born<MAX_AGE_SECONDS:
        meta=mailbox.stat();current_signature=(meta.st_dev,meta.st_ino,meta.st_size,meta.st_mtime_ns)
        if current_signature==signature:
            if time.monotonic()-last>=IDLE_SECONDS:return
            time.sleep(.025)
            continue
        config=read_json(mailbox,4*1024*1024)
        signature=current_signature
        request=(config['id'],config['attempt'])
        if request in seen:
            if request!=last_request:raise CaptureError('worker_request_replayed')
            del config
            continue
        current=scope(config)
        if bound is not None and current!=bound:raise CaptureError('worker_scope_changed')
        bound=current
        if time.time()*1000>=config['deadline_ms']:raise CaptureError('apply_deadline')
        seen.add(request);last_request=request
        code=execute(config)
        # execute/run released mutation leases and all request-local handles.
        gc.collect()
        highwater=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform!='darwin':highwater*=1024
        reusable=not code and highwater<RECYCLE_BYTES and len(seen)<MAX_REQUESTS
        # Only this marker permits a live process's receipt to be consumed.
        atomic(mailbox.with_name('done.json'),dict(id=config['id'],attempt=config['attempt'],
            worker_generation=config['worker_generation'],reusable=reusable))
        del config
        last=time.monotonic()
        if not reusable:return
