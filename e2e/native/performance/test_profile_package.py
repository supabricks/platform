import json
from pathlib import Path
import tempfile
import unittest

from profile_package import build, sha


class ProfilePackageTests(unittest.TestCase):
    def test_reapplying_overlay_keeps_one_hook_and_preserves_each_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=root/'base'
            payloads={
                'bin/supabricks':b'original binary',
                'python/analytics/python':b'#!/bin/bash\nexec "$directory/../runtime/bin/python3.12" -E -s -B "$@"\n',
            }
            for name in ('capture_worker.py','incremental_worker.py','export.py'):
                payloads['python/analytics/'+name]=b'#!/usr/bin/env python3\n"""Fixture."""\nif __name__==\'__main__\':\n    pass\n'
            for relative,data in payloads.items():
                path=base/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            manifest=dict(files={name:dict(sha256=sha(base/name),executable=False) for name in payloads})
            (base/'release.json').write_text(json.dumps(manifest))
            binary=root/'binary';binary.write_bytes(b'diagnostic binary')
            library=root/'io.so';library.write_bytes(b'diagnostic library')
            for name in ('first','second'):
                previous={p.relative_to(base):sha(p) for p in base.rglob('*') if p.is_file()}
                target=root/name;build(base,target,binary,library)
                for relative,digest in previous.items():self.assertEqual(sha(base/relative),digest)
                self.assertEqual((target/'python/analytics/python').read_text().count('for profile_depth in'),1)
                for worker in ('capture_worker.py','incremental_worker.py','export.py'):
                    source=(target/'python/analytics'/worker).read_text()
                    self.assertEqual(source.count('import worker_profile\n'),1)
                    self.assertEqual(source.count('worker_profile.install('),1)
                for relative,entry in json.loads((target/'release.json').read_text())['files'].items():
                    self.assertEqual(sha(target/relative),entry['sha256'])
                base=target


if __name__=='__main__':unittest.main()
