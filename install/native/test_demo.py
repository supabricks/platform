import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from demo import FILES, verify


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        repo=Path(__file__).resolve().parents[2]
        for name in FILES:
            source=repo/({'CATALOG.md':'docs/handbook/catalog-demo.md','DEMO.md':'docs/handbook/local-demo.md','PROJECT-PORTABILITY.md':'docs/handbook/project-portability.md','PROJECT-CREATION.md':'docs/handbook/project-creation.md', 'PROJECT-DATA.md':'docs/handbook/project-data.md'}.get(name,name))
            destination=self.root/name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
        self.seal()

    def seal(self):
        (self.root/'release.json').write_text(json.dumps({'files':{name:{'sha256':hashlib.sha256((self.root/name).read_bytes()).hexdigest()} for name in FILES}}))

    def test_packaged_walkthrough_is_self_contained_and_unexecuted(self):
        self.assertEqual(set(verify(self.root)),set(FILES))

    def test_modified_or_missing_file_is_not_a_qualified_demo(self):
        (self.root/FILES[1]).write_text('different data')
        with self.assertRaisesRegex(ValueError,'inventory'):verify(self.root)
        (self.root/FILES[1]).unlink()
        with self.assertRaisesRegex(ValueError,'missing'):verify(self.root)

    def test_even_resealed_fixture_or_executed_notebook_is_rejected(self):
        path=self.root/FILES[2];notebook=json.loads(path.read_text());notebook['cells'][1]['execution_count']=1
        path.write_text(json.dumps(notebook));self.seal()
        with self.assertRaisesRegex(ValueError,'saved execution'):verify(self.root)
        (self.root/FILES[1]).write_bytes(b'id,amount\n1,99\n');self.seal()
        with self.assertRaisesRegex(ValueError,'browser demonstration'):verify(self.root)


if __name__ == '__main__':unittest.main()
