import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('delta_cache_key',Path(__file__).with_name('deltalake-cache-key.py'))
cache=importlib.util.module_from_spec(spec);spec.loader.exec_module(cache)


class DeltaCacheKey(unittest.TestCase):
    def test_every_build_input_and_native_environment_invalidate_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in cache.INPUTS:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('before')
            def key(target='linux-x86_64',image='ubuntu24:image1',python='3.12.15'):
                return cache.key(root,target,image,python)
            original=key();self.assertEqual(key(),original)
            for name in cache.INPUTS:
                path=root/name;path.write_text('after');self.assertNotEqual(key(),original);path.write_text('before')
            for kwargs in [dict(target='macos-arm64'),dict(image='ubuntu24:image2'),dict(python='3.12.16')]:
                self.assertNotEqual(key(**kwargs),original)
            with self.assertRaises(ValueError):key(image='')
            with self.assertRaises(ValueError):key(target='unknown')


if __name__=='__main__':unittest.main()
