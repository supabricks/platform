"""Versioned Delta roots, sealed epoch inventories, and bounded journal reads."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from capture.spool import CaptureError, canonical, atomic, fault, lsn, checked_prefix, MAX_TRANSACTION

from capture.journal import ReadBusy
from capture.sqlite_journal import snapshot

MAX_FILES=4096
MAX_BYTES=1024*1024*1024
MAX_BATCH=16*1024*1024
RESERVE=128*1024*1024
MAX_RETAINED_BYTES=4*1024*1024*1024
MAX_RETAINED_FILES=32768


def storage_limits(profile='compact'):
    # Resolve per operation, never mutate module globals: reused workers may
    # serve multiple policies, and legacy callers retain their original bounds.
    if profile=='compact':return MAX_BYTES,MAX_RETAINED_BYTES,16*1024*1024
    if profile=='large':return 128*1024**3,384*1024**3,64*1024*1024
    raise CaptureError('invalid_storage_profile')


def validate_storage_profile(config):
    profile=config.get('storage_profile','compact')
    storage_limits(profile)
    if config.get('previous') and config['previous']['manifest'].get('storage_profile','compact')!=profile:
        raise CaptureError('incremental_storage_profile_changed')
    return profile


def retained_boundary(parent,extra=0,*,profile='compact'):
    # Retained epochs are never an eviction candidate for a worker. Explicit GC
    # must release their pins before another generation can consume this budget.
    maximum=storage_limits(profile)[1]
    if extra<0 or extra>maximum:raise CaptureError('incremental_retention_budget')
    used=0;count=0;roots=0
    if not parent.exists():return 0
    for root in parent.iterdir():
        roots+=1
        if root.is_symlink() or not root.is_dir():raise CaptureError('unsafe_incremental_path')
        if roots>256:raise CaptureError('incremental_retention_budget')
        for path in root.rglob('*'):
            if path.is_symlink():raise CaptureError('unsafe_incremental_path')
            if path.is_file():
                used+=path.stat().st_size;count+=1
                if used+extra>maximum or count>MAX_RETAINED_FILES:raise CaptureError('incremental_retention_budget')
    return used


def read_json(path,limit=2*1024*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:raise CaptureError('invalid_apply_metadata')
    return json.loads(path.read_bytes())


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):h.update(chunk)
    return h.hexdigest()


def files(root):
    result=[]
    for path in root.rglob('*'):
        if path.is_symlink():raise CaptureError('unsafe_incremental_path')
        if path.is_file():
            result.append(path)
            if len(result)>MAX_FILES:raise CaptureError('incremental_file_budget')
    return result


def boundary(root,deadline,extra=0,*,live_writer=False,profile='compact'):
    maximum=storage_limits(profile)[0]
    if extra<0:raise CaptureError('incremental_disk_budget')
    # Only the compaction reader samples a directory while Delta's writer is
    # finalizing files. Re-scan after a rename instead of dropping missing bytes.
    # Immutable inventories and ordinary boundary calls remain strict.
    for attempt in range(3 if live_writer else 1):
        if time.time()*1000>deadline:raise CaptureError('apply_deadline')
        try:
            used=sum(p.stat().st_size for p in files(root))
            break
        except FileNotFoundError:
            if not live_writer:raise
            if attempt==2:raise CaptureError('incremental_inventory_unstable') from None
    space=os.statvfs(root)
    if used+extra>maximum or space.f_bavail*space.f_frsize<RESERVE+extra:raise CaptureError('incremental_disk_budget')
    return used


def durable(root,sealed=frozenset()):
    for p in files(root):
        # Published immutable files were already flushed before their epoch
        # committed. New/replayed files and every directory still need fsync.
        if p in sealed:continue
        with p.open('rb') as stream:os.fsync(stream.fileno())
    for p in [*root.rglob('*'),root,root.parent]:
        if p.is_dir():
            fd=os.open(p,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)


JOURNAL_READ_SECONDS=3.0
JOURNAL_BUSY_SECONDS=0.1
JOURNAL_MAX_ATTEMPTS=32


class JournalBusyDeferred(CaptureError):
    """Only raised by the read-only pre-initialization boundary."""
    def __init__(self):super().__init__('journal_read_busy_deferred')


def journal_backoff(seconds):
    time.sleep(seconds)


def journal(config):
    # Freeze the request once. A retry must not follow a newer target or identity.
    if 'journal_access' in config:
        from capture.owner import request_for
        request=copy.deepcopy(request_for(config))
    else:
        # Direct backend access is retained for isolated storage qualification.
        # Daemon-issued production requests always carry journal_access.
        request=copy.deepcopy({k:config[k] for k in ('spool','identity','bootstrap_lsn','after_lsn','target_lsn')})
    started=time.monotonic()
    remaining=(config['deadline_ms']-time.time()*1000)/1000
    if remaining<=0:raise CaptureError('apply_deadline')
    deadline=started+min(JOURNAL_READ_SECONDS,remaining)
    stats=dict(attempts=0,busy=0,wait_ms=0,elapsed_ms=0,outcome='reading')
    config['_journal_read']=stats
    try:
        while True:
            if stats['busy'] and time.monotonic()>=deadline:
                stats['outcome']='deferred'
                raise JournalBusyDeferred()
            stats['attempts']+=1
            try:
                if 'journal_access' in request:
                    from capture.owner import read_range as owner_read
                    # Operational receipts deliberately have a closed schema.
                    # Detailed transport counters belong to the component probe.
                    result=owner_read(request,deadline)
                else:result=journal_attempt(request,deadline)
                stats['outcome']='complete'
                return result
            except CaptureError as error:
                # A scheduling pause can cross the deadline after the outer
                # guard but before the next statement. Prior BUSY still proves
                # this is exhausted read-only contention, never partial apply.
                if error.code=='journal_read_deadline' and stats['busy']:
                    stats['outcome']='deferred'
                    raise JournalBusyDeferred() from None
                raise
            except ReadBusy:
                # Only backend-classified read-only contention is retryable.
                stats['busy']+=1
                remaining=deadline-time.monotonic()
                if remaining<=0 or stats['attempts']>=JOURNAL_MAX_ATTEMPTS:
                    stats['outcome']='deferred'
                    raise JournalBusyDeferred() from None
                delay=min(.01*2**min(stats['busy']-1,4),.1,remaining)
                before=time.monotonic()
                journal_backoff(delay)
                stats['wait_ms']+=round((time.monotonic()-before)*1000)
                if time.monotonic()>=deadline:
                    stats['outcome']='deferred'
                    raise JournalBusyDeferred() from None
    finally:
        stats['elapsed_ms']=round((time.monotonic()-started)*1000)
        if stats['outcome']=='reading':stats['outcome']='failed'


def journal_attempt(config,deadline):
    with snapshot(config['spool'],deadline,JOURNAL_BUSY_SECONDS) as view:
        from capture.ranges import read_range
        return read_range(view,config,deadline)


def initialize(config):
    root=Path(config['generation']);identity=config['identity']
    profile=validate_storage_profile(config)
    maximum=storage_limits(profile)[0]
    marker=dict(identity=identity,bootstrap_id=config['bootstrap_id'],bootstrap_lsn=config['bootstrap_lsn'])
    if profile!='compact':marker['storage_profile']=profile
    if config.get('storage_generation'):marker['storage_generation']=config['storage_generation']
    retained_boundary(root.parent,profile=profile)
    if root.exists():
        if root.is_symlink():raise CaptureError('unsafe_incremental_path')
        if read_json(root/'owner.json')!=marker:raise CaptureError('incremental_identity_mismatch')
        return root
    temporary=root.with_name(root.name+'.initializing')
    if temporary.exists():
        if temporary.is_symlink() or read_json(temporary/'owner.json')!=marker:raise CaptureError('incremental_identity_mismatch')
        shutil.rmtree(temporary)
    temporary.mkdir(mode=0o700,parents=True)
    atomic(temporary/'owner.json',marker)
    try:
        if config.get('previous'):
            if not config.get('storage_generation') or not config.get('previous_generation') or Path(config['previous_generation'])==root:
                raise CaptureError('incremental_history_lost')
            from .maintenance import compact, estimate_bytes
            # The size estimate is a reservation heuristic, not a minimum
            # output size. A compacted live set can fit even when twice its
            # source bytes exceeds the generation quota. Reserve no more than
            # that quota; streaming and final boundaries still enforce it.
            estimate=min(estimate_bytes(config),maximum-boundary(temporary,config['deadline_ms'],profile=profile))
            retained_boundary(root.parent,extra=estimate,profile=profile)
            boundary(temporary,config['deadline_ms'],extra=estimate,profile=profile)
            compact(config,temporary)
        else:
            source=Path(config['bootstrap_manifest']);manifest=read_json(source)
            if manifest['id']!=config['bootstrap_id'] or manifest['source']['lsn']!=config['bootstrap_lsn']:raise CaptureError('bootstrap_changed')
            if any(manifest['source'][key]!=identity[key] for key in ('project_id','branch_id','tenant_id','timeline_id')):raise CaptureError('bootstrap_changed')
            for entry in manifest['files']:
                relative=Path(entry['path'])
                if relative.is_absolute() or '..' in relative.parts or len(relative.parts)>4:raise CaptureError('bootstrap_path')
                src=source.parent/relative;dest=temporary/'tables'/relative
                if src.is_symlink() or src.stat().st_size!=entry['bytes'] or digest(src)!=entry['sha256']:raise CaptureError('bootstrap_checksum')
                dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                os.link(src,dest)
                boundary(temporary,config['deadline_ms'],profile=profile)
        durable(temporary)
        fault('before_compaction_rename')
        temporary.rename(root)
        fd=os.open(root.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        fault('after_compaction_rename')
    except BaseException:
        # Partial initialization has never been publication authority.
        raise
    return root


def verify_previous(root,descriptor):
    for entry in descriptor['manifest']['files']:
        relative=Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts:raise CaptureError('incremental_path')
        path=root/relative
        if path.is_symlink() or path.stat().st_size!=entry['bytes'] or digest(path)!=entry['sha256']:raise CaptureError('incremental_checksum')


def inventory(root,tables):
    # Pin all logs through each selected version and all existing immutable data
    # files. Conservative root retention keeps historical versions and orphaned
    # staged commits until the entire generation has no references.
    versions={str(t['oid']):t['version'] for t in tables}
    result=[]
    for path in sorted(files(root/'tables')):
        relative=path.relative_to(root);parts=relative.parts
        if len(parts)==4 and parts[2]=='_delta_log':
            if path.suffix!='.json' or not path.stem.isdigit():raise CaptureError('unsupported_delta_log')
            if int(path.stem)>versions[parts[1]]:continue
        elif len(parts)!=3 or path.suffix!='.parquet':raise CaptureError('unsupported_delta_file')
        result.append(dict(path=str(relative),bytes=path.stat().st_size,sha256=digest(path)))
    if len(result)>MAX_FILES:raise CaptureError('incremental_file_budget')
    return result
