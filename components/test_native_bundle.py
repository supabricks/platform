import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from validate import ROOT, read_json

spec = importlib.util.spec_from_file_location("verify_bundle", Path(__file__).with_name("verify-native-bundle.py"))
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class NativeBundleContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        self.lock = read_json(ROOT / "components/components.lock.json")
        self.manifest = dict(neon_commit=self.lock["components"][0]["selection"]["commit"],
            neon_dirty=False, postgres_major=17, sources=[dict(path="vendor/postgres-v17",
                commit=self.lock["engine_pair"]["gitlink"], role="runtime")],
            bundle=dict(files={}, symlinks={}))
        names = [f"bin/{n}" for n in ("pageserver", "safekeeper", "storage_broker", "compute_ctl")]
        names += [f"pg_install/v17/bin/{n}" for n in ("postgres", "psql", "initdb", "pg_ctl", "pg_dump")]
        for name in names:
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            self.manifest["bundle"]["files"][name] = hashlib.sha256(b"fixture").hexdigest()

    def check(self):
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))
        verifier.verify(self.bundle, self.lock)

    def test_matching_inventory_passes(self):
        self.check()

    def test_mixed_or_dirty_engine_source_fails(self):
        self.manifest["neon_dirty"] = True
        with self.assertRaisesRegex(ValueError, "clean Neon"):
            self.check()
        self.manifest["neon_dirty"] = False
        self.manifest["sources"][0]["commit"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "Postgres source"):
            self.check()

    def test_tampered_and_extra_files_fail(self):
        (self.bundle / "bin/psql-extra").write_text("unlisted")
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.check()
        (self.bundle / "bin/psql-extra").unlink()
        (self.bundle / "bin/pageserver").write_text("changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.check()

    def test_escaping_symlink_fails(self):
        path = self.bundle / "external"
        path.symlink_to(self.bundle.parent)
        self.manifest["bundle"]["symlinks"]["external"] = str(self.bundle.parent)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.check()

    def test_dangling_symlink_fails(self):
        (self.bundle / "missing").symlink_to("not-present")
        self.manifest["bundle"]["symlinks"]["missing"] = "not-present"
        with self.assertRaisesRegex(ValueError, "dangling"):
            self.check()

    def test_symlink_cannot_bypass_required_binary_hash(self):
        path = self.bundle / "bin/pageserver"
        path.unlink()
        path.symlink_to("safekeeper")
        self.manifest["bundle"]["symlinks"]["bin/pageserver"] = "safekeeper"
        with self.assertRaisesRegex(ValueError, "both file and symlink"):
            self.check()
