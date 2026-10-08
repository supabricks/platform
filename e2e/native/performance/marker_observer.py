"""Read diagnostic durable-commit markers identically for SQLite and RocksDB."""
import os
import struct
from pathlib import Path

RECORD=struct.Struct('!QQId')
LIMIT=256*1024*1024


def enable(cell,analytics=None):
    # Accepted historical packages keep their original SQL observer. Diagnostic
    # overlays explicitly identify the new instrumentation for bridge controls.
    analytics=Path(analytics) if analytics is not None else cell.release/'python/analytics'
    if not (analytics/'marker_profile.py').is_file():return False
    root=cell.root/'marker-observer';root.mkdir(mode=0o700)
    (root/'enabled').touch(mode=0o600);return True


def rows(observer):
    path=observer.spool.parent.parent.parent.parent/'marker-observer'/(observer.capture_id+'.bin')
    if not path.exists():raise RuntimeError('marker_file_missing')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        size=os.fstat(fd).st_size
        if size>LIMIT or size<observer.seq*RECORD.size:raise RuntimeError('marker_budget_or_regression')
        # Concurrent append may expose an incomplete final record; leave it for
        # the next poll. Final transaction attribution rejects missing markers.
        length=min(size//RECORD.size-observer.seq,65536)*RECORD.size
        data=os.pread(fd,length,observer.seq*RECORD.size)
        if len(data)!=length:raise RuntimeError('marker_read_incomplete')
    finally:os.close(fd)
    result=[]
    for seq,end,xid,stamp in RECORD.iter_unpack(data):
        if seq!=observer.seq+len(result)+1:raise RuntimeError('marker_sequence_gap')
        result.append((seq,f'{end:016x}',struct.pack('!I',xid)))
    return result
