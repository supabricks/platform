"""UC00 source artifact boundaries; never package developer authentication state."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import concurrent.futures
import hashlib
import io
import threading

spec = importlib.util.spec_from_file_location('build_uc', Path(__file__).with_name('build-unity-catalog.py'))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class SourceArtifactTests(unittest.TestCase):
    def test_parallel_pom_and_jar_downloads_do_not_share_temporary_files(self):
        barrier=threading.Barrier(2)
        class Download(io.BytesIO):
            first=True
            def read(self,*args):
                if self.first:self.first=False;barrier.wait(timeout=3)
                return super().read(*args)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(build.urllib.request,'urlopen',side_effect=lambda url,**_:Download(url.encode())):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    tasks=[pool.submit(build.fetch,dict(url=kind,sha256=hashlib.sha256(kind.encode()).hexdigest()),root/('artifact.'+kind)) for kind in ('jar','pom')]
                    for task in tasks:task.result()
            self.assertEqual((root/'artifact.jar').read_bytes(),b'jar')
            self.assertEqual((root/'artifact.pom').read_bytes(),b'pom')

    def test_only_tracked_configuration_is_packaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'source'; source.mkdir()
            subprocess.run(['git','init','-q',str(source)],check=True)
            conf = source/'etc/conf'; conf.mkdir(parents=True)
            for name in ('server.properties','hibernate.properties'):
                (conf/name).write_text('fixture=true\n')
            subprocess.run(['git','add','etc/conf'],cwd=source,check=True)
            (source/'.gitignore').write_text('etc/conf/token.txt\netc/conf/private_key.der\n')
            (conf/'token.txt').write_text('private developer token')
            (conf/'private_key.der').write_bytes(b'private developer key')
            output = root/'templates'
            inventory = build.stage_configuration(source, output)
            self.assertEqual(set(inventory), {'server.properties','hibernate.properties'})
            self.assertEqual({p.name for p in output.iterdir()}, set(inventory))

    def test_corrupt_cached_download_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cached = Path(tmp)/'runtime.tar.gz'; cached.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                build.fetch(dict(url='https://invalid.example/',sha256='0'*64),cached)


if __name__ == '__main__': unittest.main()
