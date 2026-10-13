import json
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
import upgrade_control as control


class UpgradeControl(unittest.TestCase):
    def release(self, root, engine):
        root.mkdir()
        files={}
        for name,content in [('engine/manifest.json',engine),('bin/supabricks','same-candidate-platform')]:
            path=root/name;path.parent.mkdir(exist_ok=True);path.write_text(content)
            files[name]=dict(sha256=control.digest(path),executable=False)
        (root/'release.json').write_text(json.dumps(dict(version='v0.1.0-alpha.36',target='linux-x86_64',
            profile='test',files=files,provenance={})))
        return root

    def test_control_retains_exact_engine_and_candidate_platform_with_separate_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);old=self.release(root/'old','old');new=self.release(root/'new','new')
            before={p:control.digest(p/'release.json') for p in (old,new)}
            receipt=control.create(new,old,root/'control')
            manifest=json.loads((root/'control/release.json').read_text())
            self.assertEqual((root/'control/engine/manifest.json').read_text(),'old')
            self.assertEqual((root/'control/bin/supabricks').read_bytes(),(new/'bin/supabricks').read_bytes())
            self.assertEqual(receipt['control_identity'],control.digest(root/'control/release.json'))
            self.assertNotEqual(receipt['control_identity'],receipt['candidate_identity'])
            self.assertTrue(manifest['version'].endswith('.platformcontrol'))
            self.assertEqual(manifest['provenance']['engine_manifest_sha256'],control.digest(old/'engine/manifest.json'))
            self.assertEqual(before,{p:control.digest(p/'release.json') for p in (old,new)})
            with self.assertRaises(AssertionError):control.create(new,new,root/'unnecessary')

    def test_signed_control_archive_has_only_verified_regular_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);old=self.release(root/'old','old');new=self.release(root/'new','new')
            key=root/'key.pem'
            subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(key)],check=True,capture_output=True)
            receipt=control.stage_control(new,old,root/'web','http://127.0.0.1/control',key)
            archive=next((root/'web').glob('*.tar.gz'))
            with tarfile.open(archive) as tar:
                self.assertTrue(all(m.isfile() for m in tar.getmembers()))
                self.assertEqual(tar.extractfile('supabricks/engine/manifest.json').read(),b'old')
                manifest=tar.extractfile('supabricks/release.json').read()
                self.assertEqual(control.hashlib.sha256(manifest).hexdigest(),receipt['control_identity'])
            self.assertTrue(Path(str(archive)+'.sha256.sig').is_file())
            (new/'bin/supabricks').write_text('corrupted')
            with self.assertRaises(AssertionError):control.stage_control(new,old,root/'bad','http://127.0.0.1/bad',key)

    def test_rejection_requires_specific_error_and_unchanged_source_without_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';data.mkdir();(root/'current').symlink_to('old')
            for name in ('state.sqlite3','runtime.json'):(data/name).write_text('original')
            before=control.checkpoint(root,data)
            result=subprocess.CompletedProcess([],1,'','incompatible engine, library, analytical or data format')
            control.assert_refused(result,root,data,root/'backup',before)
            for result in [subprocess.CompletedProcess([],0,'',''),subprocess.CompletedProcess([],1,'','unrelated failure')]:
                with self.assertRaises(AssertionError):control.assert_refused(result,root,data,root/'backup',before)
            result=subprocess.CompletedProcess([],1,'','incompatible engine, library, analytical or data format')
            (data/'state.sqlite3').write_text('changed')
            with self.assertRaises(AssertionError):control.assert_refused(result,root,data,root/'backup',before)


if __name__=='__main__':unittest.main()
