import importlib.util
import io
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("fetch_helpers", Path(__file__).with_name("fetch-native-helpers.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class HelperDownloadTests(unittest.TestCase):
    def test_valid_bytes_are_published_and_reused_without_network(self):
        data = b"a pinned archive"
        checksum = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper.tar.gz"
            with patch.object(helpers.urllib.request, "urlopen", return_value=io.BytesIO(data)):
                helpers.fetch("https://example.org/helper.tar.gz", checksum, path)
            self.assertEqual(path.read_bytes(), data)
            with patch.object(helpers.urllib.request, "urlopen", side_effect=AssertionError("unexpected download")):
                helpers.fetch("https://example.org/helper.tar.gz", checksum, path)

    def test_mismatch_leaves_no_artifact_or_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper.tar.gz"
            with patch.object(helpers.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    helpers.fetch("https://example.org/helper.tar.gz", "0" * 64, path)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_existing_mismatched_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper.tar.gz"
            path.write_bytes(b"existing")
            with self.assertRaisesRegex(ValueError, "existing file"):
                helpers.fetch("https://example.org/helper.tar.gz", "0" * 64, path)
            self.assertEqual(path.read_bytes(), b"existing")
