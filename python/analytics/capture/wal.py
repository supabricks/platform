"""Capture-only journal policy, cooperative migration leases and physical bounds."""
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time
from .spool import CaptureError, RESERVE, private_file, fault

PAGE = 4096
CHECKPOINT_BYTES = 4 * 1024 * 1024
SIDECARS = ('', '-wal', '-shm', '-journal')


class SpoolBackpressure(CaptureError):
    def __init__(self):super().__init__('spool_backpressure')


def private_sizes(path):
    """Do not open SQLite until every extant database/sidecar is private and regular."""
    sizes = {}
    for suffix in SIDECARS:
        item = Path(str(path) + suffix)
        try:meta = item.lstat()
        except FileNotFoundError:
            sizes[suffix] = 0
            continue
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or meta.st_nlink != 1:
            raise CaptureError('unsafe_spool_path')
        sizes[suffix] = meta.st_size
    return sizes


def reader_lease(path):
    """Held only through materialization; callers close it before Delta work."""
    root = Path(path).parent
    meta = root.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
        raise CaptureError('unsafe_spool_directory')
    fd = private_file(root / 'readers.lock')
    try:fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        error = sqlite3.OperationalError('capture migration busy')
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error from None
    try:private_sizes(path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def fixed_sqlite():
    major, minor, patch = sqlite3.sqlite_version_info
    return major == 3 and (minor >= 53 or (minor == 51 and patch >= 3) or (minor == 50 and patch >= 7) or (minor == 44 and patch >= 6))


class Journal:
    def __init__(self, spool, mode):
        if not 16*1024*1024 <= spool.limit <= 512*1024*1024:raise CaptureError('invalid_spool_budget')
        if not hasattr(sqlite3.Connection,'setconfig') or not hasattr(sqlite3,'SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE'):
            raise CaptureError('sqlite_wal_unqualified')
        if mode not in ('wal', 'delete'):raise CaptureError('invalid_journal_mode')
        if not fixed_sqlite():raise CaptureError('sqlite_wal_unqualified')
        self.spool, self.mode = spool, mode
        # Reserve room for a complete transaction image, sidecars and checkpoint
        # growth. This is a physical envelope, not a promise of limit bytes of rows.
        self.pages = max(1, (spool.limit - 1024 * 1024) // (3 * PAGE))
        self.threshold = min(CHECKPOINT_BYTES, max(PAGE, spool.limit // 16))
        self.last_checkpoint = 0
        self.stats = dict(checkpoints=0, busy=0, checkpoint_ms=0.0, log_frames=0,
                          checkpointed_frames=0, mode=None, backpressure_events=0,
                          backpressured=False)

    @property
    def db(self):return self.spool.db

    def configure(self):
        db = self.db
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA temp_store=MEMORY')
        db.execute('PRAGMA cache_spill=OFF')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('PRAGMA busy_timeout=50')
        if not hasattr(db, 'setconfig') or not hasattr(sqlite3, 'SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE'):
            raise CaptureError('sqlite_wal_unqualified')
        db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        if not db.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE):
            raise CaptureError('spool_checkpoint_owner')
        if db.execute('PRAGMA page_size').fetchone()[0] != PAGE:
            raise CaptureError('spool_page_size')
        if db.execute('PRAGMA page_count').fetchone()[0] > self.pages:
            raise CaptureError('spool_migration_budget')
        if db.execute(f'PRAGMA max_page_count={self.pages}').fetchone()[0] != self.pages:
            raise CaptureError('spool_migration_budget')
        if (db.execute('PRAGMA synchronous').fetchone()[0] != 2 or
                db.execute('PRAGMA cache_spill').fetchone()[0] != 0 or
                db.execute('PRAGMA wal_autocheckpoint').fetchone()[0] != 0 or
                db.execute('PRAGMA temp_store').fetchone()[0] != 2):
            raise CaptureError('spool_durability')

    def activate(self):
        """The owner already verified identity and the complete durable chain."""
        current = self.db.execute('PRAGMA journal_mode').fetchone()[0]
        if current != self.mode:
            lease = private_file(self.spool.root / 'readers.lock')
            try:
                try:fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:raise CaptureError('spool_migration_busy') from None
                self.before_write()
                if current == 'wal':
                    busy, _, _ = self.checkpoint(force=True)
                    if busy:raise CaptureError('spool_migration_busy')
                fault('before_spool_migration')
                try:actual = self.db.execute('PRAGMA journal_mode=' + self.mode).fetchone()[0]
                except sqlite3.OperationalError as error:
                    if getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_BUSY:
                        raise CaptureError('spool_migration_busy') from None
                    raise
                if actual != self.mode:raise CaptureError('spool_journal_mode')
                fault('after_spool_migration')
                directory = os.open(self.spool.root, os.O_RDONLY)
                try:os.fsync(directory)
                finally:os.close(directory)
            finally:os.close(lease)
        if self.db.execute('PRAGMA journal_mode').fetchone()[0] != self.mode:
            raise CaptureError('spool_journal_mode')
        private_sizes(self.spool.path)

    def physical(self):
        return sum(private_sizes(self.spool.path).values())

    def checkpoint(self, force=False):
        # The sole writer owns every explicit checkpoint. Never wait for a reader.
        if self.db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':return (0, 0, 0)
        sizes = private_sizes(self.spool.path)
        now = time.monotonic()
        if not force and (sizes['-wal'] < self.threshold or now - self.last_checkpoint < 1):
            return (0, self.stats['log_frames'], self.stats['checkpointed_frames'])
        mode = 'TRUNCATE' if force else 'PASSIVE'
        timeout = self.db.execute('PRAGMA busy_timeout').fetchone()[0]
        started = time.monotonic()
        self.db.execute('PRAGMA busy_timeout=0')
        fault('before_spool_checkpoint')
        try:result = self.db.execute('PRAGMA wal_checkpoint(' + mode + ')').fetchone()
        finally:self.db.execute(f'PRAGMA busy_timeout={timeout}')
        fault('after_spool_checkpoint')
        self.last_checkpoint = now
        busy, frames, checkpointed = result
        self.stats['checkpoints'] += 1
        self.stats['busy'] += int(bool(busy) or (frames > checkpointed))
        self.stats['checkpoint_ms'] += (time.monotonic() - started) * 1000
        self.stats.update(mode=mode, log_frames=frames, checkpointed_frames=checkpointed)
        private_sizes(self.spool.path)
        return result

    def before_append(self, reservation):
        used = (self.db.execute('PRAGMA page_count').fetchone()[0] -
                self.db.execute('PRAGMA freelist_count').fetchone()[0]) * PAGE
        # Preserve metadata/pruning headroom inside the capped database as well.
        if used + reservation + 256*1024 > self.pages * PAGE:
            self.stats['backpressure_events'] += 1
            self.stats['backpressured'] = True
            raise SpoolBackpressure()

    def before_write(self):
        # No cache spilling means each dirty page has only one final frame per
        # transaction. Reserve even the complete capped database as dirty. This
        # also bounds checkpoint DB growth when the main file lags the WAL.
        sizes = private_sizes(self.spool.path)
        image = self.pages * (PAGE + 24) + 32
        shm = max(1024 * 1024, sizes['-shm'])
        db_bound = self.pages * PAGE
        needed = db_bound + sizes['-wal'] + sizes['-journal'] + image + shm
        if needed > self.spool.limit:
            self.checkpoint(force=True)
            sizes = private_sizes(self.spool.path)
            needed = db_bound + sizes['-wal'] + sizes['-journal'] + image + shm
        space = os.statvfs(self.spool.root)
        if needed > self.spool.limit or space.f_bavail * space.f_frsize < RESERVE + image + db_bound + shm:
            self.stats['backpressure_events'] += 1
            self.stats['backpressured'] = True
            raise SpoolBackpressure()
        self.stats['backpressured'] = False

    def after_write(self):
        if self.physical() > self.spool.limit:raise CaptureError('spool_budget')
        self.checkpoint()

    def progress(self):
        sizes = private_sizes(self.spool.path)
        return dict(self.stats, journal_mode=self.mode, synchronous=2,
                    database_bytes=sizes[''], wal_bytes=sizes['-wal'], shm_bytes=sizes['-shm'],
                    rollback_journal_bytes=sizes['-journal'], physical_bytes=sum(sizes.values()),
                    physical_limit=self.spool.limit, database_page_limit=self.pages,
                    checkpoint_threshold_bytes=self.threshold)
