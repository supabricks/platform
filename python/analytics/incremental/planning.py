"""Read-only planning inventory, scoped to one live generation mutation lease.

The daemon admits one writer and pins its source/destination against GC. The
kernel lock additionally fences overlapping worker processes; it is never a
persisted ownership token. No inventory survives planning, writes or recovery.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time
from capture.spool import CaptureError
from . import storage


def signature(meta):
    return (meta.st_dev,meta.st_ino,meta.st_mode,meta.st_uid,meta.st_nlink,
            meta.st_size,meta.st_mtime_ns,meta.st_ctime_ns)


class MutationLease:
    def __init__(self,root,fd):
        self.root=Path(root);self.fd=fd;self.identity=signature(os.fstat(fd))[:4]
        self.ancestors={}
        for path in self.root.parents:
            meta=path.lstat()
            if not stat.S_ISDIR(meta.st_mode):raise CaptureError('unsafe_incremental_path')
            self.ancestors[path]=signature(meta)[:4]
    def check(self):
        if self.fd is None:raise CaptureError('incremental_lease_lost')
        try:
            opened=signature(os.fstat(self.fd))[:4]
            current=signature(self.root.lstat())[:4]
        except OSError:raise CaptureError('incremental_lease_lost') from None
        if opened!=self.identity or current!=self.identity:raise CaptureError('incremental_lease_lost')
        for path,expected in self.ancestors.items():
            try:actual=signature(path.lstat())[:4]
            except OSError:raise CaptureError('incremental_lease_lost') from None
            if actual!=expected:raise CaptureError('incremental_lease_lost')


@contextmanager
def mutation_lease(root):
    # Lock the directory inode itself: no extra mutable files in sealed roots.
    try:fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except OSError:raise CaptureError('unsafe_incremental_path') from None
    try:
        meta=os.fstat(fd)
        if meta.st_uid!=os.getuid() or meta.st_mode&0o022:raise CaptureError('unsafe_incremental_path')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise CaptureError('incremental_writer_busy') from None
        lease=MutationLease(root,fd)
        try:
            lease.check()
            yield lease
            lease.check()
        finally:lease.fd=None
    finally:os.close(fd)


def inventory_snapshot(root,deadline):
    """One bounded walk: sizes plus identities, including unreferenced files."""
    if time.time()*1000>deadline:raise CaptureError('apply_deadline')
    directories={};files={};used=0
    for path in _paths(root):
        meta=path.lstat()
        if meta.st_uid!=os.getuid() or meta.st_mode&0o022:raise CaptureError('unsafe_incremental_path')
        if stat.S_ISDIR(meta.st_mode):
            directories[path]=signature(meta)
            if len(directories)>storage.MAX_FILES:raise CaptureError('incremental_file_budget')
        elif stat.S_ISREG(meta.st_mode):
            files[path]=signature(meta);used+=meta.st_size
            if len(files)>storage.MAX_FILES:raise CaptureError('incremental_file_budget')
        else:raise CaptureError('unsafe_incremental_path')
        if used>storage.MAX_BYTES:raise CaptureError('incremental_disk_budget')
    return directories,files,used


def _paths(root):
    yield root
    yield from root.rglob('*')


class PlanningBoundary:
    def __init__(self,root,deadline,lease=None):
        self.root=Path(root);self.deadline=deadline;self.lease=lease
    def __enter__(self):
        if self.lease is not None:
            if self.lease.root!=self.root:raise CaptureError('incremental_lease_lost')
            self.lease.check()
            self.snapshot=inventory_snapshot(self.root,self.deadline)
        self.check()
        return self
    def check(self):
        if self.lease is None:
            # Standalone/unowned callers retain conservative per-batch scans.
            storage.boundary(self.root,self.deadline)
            return
        if time.time()*1000>self.deadline:raise CaptureError('apply_deadline')
        self.lease.check()
        for path,expected in self.snapshot[0].items():
            try:actual=signature(path.lstat())
            except OSError:raise CaptureError('incremental_plan_mutated') from None
            if actual!=expected:raise CaptureError('incremental_plan_mutated')
        # Planning allocates no disk output. Check actual headroom every batch;
        # apply_table still reserves the full output before allocating any file.
        space=os.statvfs(self.root)
        if self.snapshot[2]>storage.MAX_BYTES or space.f_bavail*space.f_frsize<storage.RESERVE:
            raise CaptureError('incremental_disk_budget')
    def __exit__(self,kind,error,tb):
        if kind is None:
            self.check()
            if self.lease is not None:
                # Detect in-place growth/edits as well as path changes before a
                # plan may be persisted or any Delta output may be allocated.
                if inventory_snapshot(self.root,self.deadline)!=self.snapshot:
                    raise CaptureError('incremental_plan_mutated')
        self.snapshot=None
