"""Archive and source-policy attacks, without any network or installed runtime."""
import importlib.util
import io
from pathlib import Path
import stat
import unittest
from unittest.mock import patch
import tempfile
import urllib.error
import zipfile

spec=importlib.util.spec_from_file_location('packages',Path(__file__).with_name('environment-packages.py'))
packages=importlib.util.module_from_spec(spec)
spec.loader.exec_module(packages)


class Packages(unittest.TestCase):
    def test_archive_paths_links_duplicates_and_limits(self):
        for name in ['../outside','/absolute','a/../outside','a\\outside','a//outside']:
            with self.subTest(name=name):
                stream=io.BytesIO()
                with zipfile.ZipFile(stream,'w') as archive:archive.writestr(name,b'bad')
                with zipfile.ZipFile(stream) as archive,self.assertRaises(packages.Failure):packages.wheel_entries(archive)
        stream=io.BytesIO()
        link=zipfile.ZipInfo('link');link.external_attr=(stat.S_IFLNK|0o777)<<16
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr(link,b'../../outside')
        with zipfile.ZipFile(stream) as archive,self.assertRaises(packages.Failure):packages.wheel_entries(archive)
        class Large:
            def infolist(self):
                info=zipfile.ZipInfo('file');info.file_size=packages.MAX_EXPANDED+1
                return [info]
        with self.assertRaises(packages.Failure):packages.wheel_entries(Large())

    def test_registry_artifact_urls_cannot_redirect_to_local_or_credentialed_sources(self):
        for url in ['file:///etc/passwd','http://files.pythonhosted.org/x.whl','https://127.0.0.1/x','https://example.org/x','https://secret@files.pythonhosted.org/x','https://files.pythonhosted.org:8080/x','https://files.pythonhosted.org/x?token=secret']:
            with self.subTest(url=url),self.assertRaises(packages.Failure):packages.checked_url(url)
        self.assertEqual(packages.checked_url('https://files.pythonhosted.org/a.whl'),'https://files.pythonhosted.org/a.whl')

    def test_network_loss_and_download_budget_fail_with_bounded_public_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'download'
            with patch.object(packages.urllib.request, 'build_opener') as opener:
                opener.return_value.open.side_effect=urllib.error.URLError('private diagnostic')
                with self.assertRaises(packages.Failure) as caught:
                    packages.download('https://files.pythonhosted.org/a.whl',path,100,lambda:None)
                self.assertEqual(str(caught.exception),'network_failed')
            with patch.object(packages.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value=io.BytesIO(b'1234')
                with self.assertRaises(packages.Failure) as caught:
                    packages.download('https://files.pythonhosted.org/a.whl',path,3,lambda:None)
                self.assertEqual(str(caught.exception),'artifact_limit')

    def test_workspace_boundary_is_private_and_removed_after_resolver_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            documents=Path(directory)
            path=documents/'pyproject.toml'
            original=b'[project]\nname="example"\n'
            path.write_bytes(original)
            with self.assertRaises(RuntimeError):
                with packages.standalone_project(documents):
                    self.assertIn(b'[tool.uv.workspace]',path.read_bytes())
                    raise RuntimeError('resolver conflict')
            self.assertEqual(path.read_bytes(),original)


if __name__=='__main__':unittest.main()
