import importlib.util
import marshal
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from compile_sync import compile_sources


class ImmutableBytecode(unittest.TestCase):
    def test_relocation_and_changed_source_without_runtime_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'builder';root.mkdir();source=root/'fixture.py'
            source.write_text('VALUE=1\n')
            result=compile_sources(root,[source]);cache=Path(importlib.util.cache_from_source(str(source)))
            original=cache.read_bytes()
            self.assertEqual(result['modules'],1)
            self.assertEqual(result['bytes'],len(original))
            self.assertEqual(struct.unpack('<I',original[4:8])[0],3)  # checked hash
            self.assertEqual(marshal.loads(original[16:]).co_filename,'fixture.py')
            relocated=Path(directory)/'installed with spaces';shutil.copytree(root,relocated)
            def value():
                return subprocess.check_output([sys.executable,'-I','-B','-c',
                    'import sys;sys.path.insert(0,sys.argv[1]);import fixture;print(fixture.VALUE)',str(relocated)],text=True).strip()
            self.assertEqual(value(),'1')
            (relocated/'fixture.py').write_text('VALUE=2\n')
            self.assertEqual(value(),'2')
            self.assertEqual((relocated/cache.relative_to(root)).read_bytes(),original)
            self.assertEqual(len(list(relocated.rglob('*.pyc'))),1)

    def test_reproducible_across_build_roots_and_no_external_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            roots=[Path(directory)/name for name in ('first','second')];caches=[]
            for root in roots:
                root.mkdir();source=root/'fixture.py';source.write_text('VALUE=1\n')
                compile_sources(root,[source]);caches.append(Path(importlib.util.cache_from_source(str(source))).read_bytes())
            self.assertEqual(*caches)
            with self.assertRaises(ValueError):compile_sources(roots[0],[roots[1]/'fixture.py'])


if __name__=='__main__':unittest.main()
