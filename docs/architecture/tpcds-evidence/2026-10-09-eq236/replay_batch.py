"""Replay both diagnostic batch joins using public, minimal archived inputs."""
import gzip
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from analyze_batch_profile import analyze

base = Path(__file__).resolve().parent
for label in ('c128', 'c1024'):
    archived = base / label
    with tempfile.TemporaryDirectory(prefix='eq236-replay-') as directory:
        root = Path(directory)
        profile = root / 'state/sync-profile'
        profile.mkdir(parents=True)
        for source in (archived / 'batch-profile').glob('*.jsonl.gz'):
            (profile / source.stem).write_bytes(gzip.decompress(source.read_bytes()))
        (root / 'commits.jsonl').write_bytes(gzip.decompress((archived / 'commits.jsonl.gz').read_bytes()))
        shutil.copyfile(archived / 'result.json', root / 'result.json')
        with sqlite3.connect(root / 'state/state.sqlite3') as db:
            db.execute('CREATE TABLE publications (published_at_ms, descriptor, export_id, state, ordinal)')
            db.execute('CREATE TABLE incremental_runs (id, record)')
            for ordinal, (at, record, descriptor) in enumerate(json.loads((archived / 'batch-profile/publication-inputs.json').read_text())):
                db.execute('INSERT INTO publications VALUES (?, ?, ?, ?, ?)',
                           (at, json.dumps(descriptor), record['id'], 'published', ordinal))
                db.execute('INSERT INTO incremental_runs VALUES (?, ?)', (record['id'], json.dumps(record)))
        actual = json.dumps(analyze(root, profile), indent=2) + '\n'
        assert actual == (base / (label + '-batch.json')).read_text(), label
        print(label + ': batch join replays byte identically')
