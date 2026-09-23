"""Versioned Delta roots, sealed epoch inventories, and bounded journal reads."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from capture.spool import CaptureError, canonical, atomic, fault, lsn, checked_prefix, MAX_TRANSACTION

MAX_FILES=4096
MAX_BYTES=1024*1024*1024
MAX_BATCH=16*1024*1024
RESERVE=128*1024*1024
MAX_RETAINED_BYTES=4*1024*1024*1024
MAX_RETAINED_FILES=32768


def retained_boundary(parent,extra=0):
    # Retained epochs are never an eviction candidate for a worker. Explicit GC
    # must release their pins before another generation can consume this budget.
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
                if used+extra>MAX_RETAINED_BYTES or count>MAX_RETAINED_FILES:raise CaptureError('incremental_retention_budget')
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


def boundary(root,deadline,extra=0):
    if time.time()*1000>deadline:raise CaptureError('apply_deadline')
    used=sum(p.stat().st_size for p in files(root))
    space=os.statvfs(root)
    if used+extra>MAX_BYTES or space.f_bavail*space.f_frsize<RESERVE+extra:raise CaptureError('incremental_disk_budget')
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


def journal(config):
    path=Path(config['spool'])
    if path.is_symlink() or path.stat().st_size>512*1024*1024:raise CaptureError('spool_budget')
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=3)
    try:
        db.execute('BEGIN')
        meta={}
        for k,v,size in db.execute('SELECT key,CASE WHEN length(value)<=2097152 THEN value ELSE NULL END,length(value) FROM metadata LIMIT 33'):
            if len(meta)>=32 or size>2097152:raise CaptureError('spool_metadata_budget')
            meta[k]=json.loads(v)
        if meta['identity']!=config['identity'] or meta['bootstrap']['lsn']!=config['bootstrap_lsn']:raise CaptureError('spool_identity_mismatch')
        after=lsn(config['after_lsn']);target=lsn(config['target_lsn'])
        prefix=checked_prefix(meta.get('pruned_prefix'),meta['identity'],meta['start'],meta['captured'])
        if meta['captured']<target or prefix['lsn']>after:raise CaptureError('source_history_lost')
        result=[];used=0;previous=None;end=after
        cursor=db.execute('SELECT end_lsn,previous_lsn,commit_lsn,length(payload),CASE WHEN length(payload)<=4194304 THEN payload ELSE NULL END,sha256 FROM transactions WHERE end_lsn>? AND end_lsn<=? ORDER BY seq',(f'{after:016x}',f'{target:016x}'))
        for cut,prior,commit,size,payload,checksum in cursor:
            cut,prior,commit=int(cut,16),int(prior,16),int(commit,16)
            if size>MAX_TRANSACTION or len(payload)!=size or hashlib.sha256(payload).hexdigest()!=checksum:raise CaptureError('spool_corrupt')
            if not prior<=commit<cut or (previous is not None and prior!=previous) or (previous is None and (prior>after or (after!=lsn(config['bootstrap_lsn']) and prior!=after))):raise CaptureError('spool_history_gap')
            if used+size>MAX_BATCH:break
            used+=size;result.append((cut,payload));previous=end=cut
        if not result and target>after:raise CaptureError('spool_history_gap')
        return meta['schema'],result,end,used
    finally:db.close()


def initialize(config):
    root=Path(config['generation']);identity=config['identity']
    marker=dict(identity=identity,bootstrap_id=config['bootstrap_id'],bootstrap_lsn=config['bootstrap_lsn'])
    if config.get('storage_generation'):marker['storage_generation']=config['storage_generation']
    retained_boundary(root.parent)
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
            estimate=estimate_bytes(config)
            retained_boundary(root.parent,extra=estimate)
            boundary(temporary,config['deadline_ms'],extra=estimate)
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
                boundary(temporary,config['deadline_ms'])
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
