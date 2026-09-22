"""Incremental verification of the private, frozen A01 baseline (never publication)."""
import hashlib
import json
from pathlib import Path
from .spool import CaptureError, canonical, lsn


def verify(config, spool):
    baseline = config['bootstrap']
    path = Path(baseline['manifest'])
    if path.is_symlink() or path.stat().st_size > 4*1024*1024:
        raise CaptureError('bootstrap_manifest_budget')
    # macOS exposes /var and /tmp through aliases. Compare one canonical path
    # spelling throughout the inventory without allowing a symlinked manifest.
    path = path.resolve(strict=True)
    manifest = json.loads(path.read_bytes())
    identity = config['identity']
    source = manifest['source']
    if (manifest['format_version'] != 1 or manifest['status'] != 'files_complete'
            or manifest['published'] or manifest['id'] != baseline['id']
            or manifest['database'] != 'postgres'
            or manifest['database_oid'] != spool.get('profile')['database_oid']
            or any(source[key] != identity[key] for key in ('project_id','branch_id','tenant_id','timeline_id'))
            or source['export_branch_id'] == identity['branch_id']
            or source['export_timeline_id'] == identity['timeline_id']
            or lsn(source['lsn']) < spool.get('start')):
        raise CaptureError('bootstrap_identity_or_boundary')
    tables = {str(t['oid']): t for t in manifest['tables']}
    relations = spool.get('schema')
    if len(tables) != len(manifest['tables']) or tables.keys() != relations.keys():
        raise CaptureError('bootstrap_schema_changed')
    for oid, (namespace, name, _, columns) in relations.items():
        table = tables[oid]
        if (table['schema'], table['name']) != (namespace, name) or [c[1:] for c in columns] != [[c['name'], c['type_oid'], c['typmod']] for c in table['columns']]:
            raise CaptureError('bootstrap_schema_changed')
    root = path.parent.resolve()
    seen = set()
    if len(manifest['files']) > 65536:
        raise CaptureError('bootstrap_file_budget')
    for entry in manifest['files']:
        relative = Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts or str(relative) in seen:
            raise CaptureError('bootstrap_path')
        seen.add(str(relative))
        file = root / relative
        if any(p.is_symlink() for p in [file, *file.parents]) or not file.is_file() or file.stat().st_size != entry['bytes']:
            raise CaptureError('bootstrap_file')
        digest = hashlib.sha256()
        with file.open('rb') as stream:
            while chunk := stream.read(1024*1024):
                digest.update(chunk)
                yield None  # Give capture/feedback/retention monitoring a turn between chunks.
        if digest.hexdigest() != entry['sha256']:
            raise CaptureError('bootstrap_checksum')
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p != path}
    if actual != seen:
        raise CaptureError('bootstrap_inventory')
    receipt = dict(id=baseline['id'], lsn=source['lsn'], manifest_sha256=hashlib.sha256(canonical(manifest)).hexdigest())
    previous = spool.get('bootstrap')
    if previous is not None and previous != receipt:
        raise CaptureError('bootstrap_changed')
    spool.set('bootstrap', receipt)
    yield receipt['lsn']
