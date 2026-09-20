import json
from pathlib import Path
import tempfile
import unittest
from unity_catalog import verify, digest


class CatalogArtifactTests(unittest.TestCase):
    def fixture(self, root):
        components=root/'components';components.mkdir()
        runtime=root/'runtime';runtime.mkdir()
        payload={'java/bin/java':b'java','classpath.json':b'["jars/unitycatalog-server-0.6.0.jar"]',
                 'jars/unitycatalog-server-0.6.0.jar':b'jar','dependencies.json':b'[]',
                 'licenses/LICENSE':b'license','licenses/NOTICE':b'notice'}
        for name,data in payload.items():
            path=runtime/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        pin=dict(commit='reviewed',java={'version':'17'},sbt={'version':'1.9.9'},inputs={n:digest(runtime/'licenses'/n) for n in ['LICENSE','NOTICE']})
        (components/'unity-catalog-source.lock.json').write_text(json.dumps(pin))
        (components/'unity-catalog-maven.lock.json').write_text('{}')
        (components/'build-unity-catalog.py').write_text('reviewed builder')
        report=dict(schema_version=2,target='linux-x86_64',source_commit=pin['commit'],source_dirty=False,java=pin['java'],sbt=pin['sbt'],source_inputs=pin['inputs'],
            source_pin_sha256=digest(components/'unity-catalog-source.lock.json'),dependency_lock_sha256=digest(components/'unity-catalog-maven.lock.json'),builder_script_sha256=digest(components/'build-unity-catalog.py'),
            files={name:digest(runtime/name) for name in payload},jars=[dict(path='jars/unitycatalog-server-0.6.0.jar',source_name='unitycatalog-server-0.6.0.jar',sha256=digest(runtime/'jars/unitycatalog-server-0.6.0.jar'))])
        (runtime/'build.json').write_text(json.dumps(report));return runtime,report

    def test_missing_dirty_wrong_target_and_tampered_artifacts_fail_assembly(self):
        for change in ['missing','dirty','target','source','dependency_lock','tampered','extra','symlink']:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);runtime,report=self.fixture(root)
                verify(runtime,'linux-x86_64',root=root)
                if change=='missing':(runtime/'java/bin/java').unlink()
                if change=='dirty':report['source_dirty']=True
                if change=='target':report['target']='macos-arm64'
                if change=='source':report['source_commit']='other'
                if change=='dependency_lock':report['dependency_lock_sha256']='other'
                if change=='tampered':(runtime/'java/bin/java').write_text('different')
                if change=='extra':(runtime/'extra').write_text('unexpected')
                if change=='symlink':
                    (runtime/'java/bin/java').unlink();(runtime/'java/bin/java').symlink_to(runtime/'licenses/LICENSE')
                (runtime/'build.json').write_text(json.dumps(report))
                with self.assertRaises(ValueError):verify(runtime,'linux-x86_64',root=root)


if __name__=='__main__':unittest.main()
