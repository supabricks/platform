#!/usr/bin/env python3
"""Inspect this interpreter's loaded SQLite and exercise a private DELETE database."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import _sqlite3


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def probe():
    with closing(sqlite3.connect(':memory:')) as db:
        version, source_id = db.execute('SELECT sqlite_version(), sqlite_source_id()').fetchone()
        options = sorted(row[0] for row in db.execute('PRAGMA compile_options'))
    checks = []
    with tempfile.TemporaryDirectory(prefix='sqlite-qualification-') as temporary:
        path = Path(temporary) / 'probe.sqlite3'
        db = sqlite3.connect(path)
        try:
            assert db.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
            db.execute('PRAGMA synchronous=FULL')
            assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
            db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
            db.executemany('INSERT INTO records VALUES (?,?)', [(1, 'first'), (2, 'second')])
            db.commit()
            db.execute("UPDATE records SET value='uncommitted'")
            db.rollback()
        finally:
            db.close()
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            assert db.execute('SELECT * FROM records ORDER BY id').fetchall() == [(1, 'first'), (2, 'second')]
            assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            checks.extend(['full_delete_roundtrip', 'rollback_preserves_committed', 'integrity_check'])
            try:
                db.execute("INSERT INTO records VALUES (3,'forbidden')")
            except sqlite3.OperationalError as error:
                assert error.sqlite_errorcode == sqlite3.SQLITE_READONLY
            else:
                raise AssertionError('read-only connection accepted mutation')
            checks.append('read_only_refuses_write')
        finally:
            db.close()
    extension = getattr(_sqlite3, '__file__', None)
    return dict(version=version, source_id=source_id, compile_options=options,
                python_version=sys.version, python_executable_sha256=sha(sys.executable),
                binding='python/_sqlite3', module_origin='extension' if extension else _sqlite3.__spec__.origin,
                extension_sha256=sha(extension) if extension else None,
                journal_mode='delete', synchronous=2, checks=checks)


if __name__ == '__main__':
    print(json.dumps(probe(), indent=2, sort_keys=True))
