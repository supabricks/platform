import copy
from pathlib import Path
import tempfile
import unittest

from validate import ROOT, read_json, unique_object, validate


class ComponentContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest = read_json(ROOT / "components/components.lock.json")

    def component(self, name):
        return next(c for c in self.manifest["components"] if c["id"] == name)

    def assert_invalid(self, fragment):
        errors = validate(self.manifest)
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_inventory_passes_but_is_not_release_qualified(self):
        self.assertEqual(validate(self.manifest), [])
        errors = validate(self.manifest, require_qualified=True)
        self.assertTrue(any("neon-engine/linux-x86_64" in error for error in errors))

    def test_floating_source_is_rejected(self):
        self.component("neon-engine")["selection"]["commit"] = "sb/main"
        self.assert_invalid("selection")

    def test_postgres_cannot_silently_follow_maintained_branch(self):
        self.component("postgres16")["selection"]["commit"] = self.manifest[
            "engine_pair"]["maintained_candidate"]["commit"]
        self.assert_invalid("differs from Neon gitlink")

    def test_duplicate_or_missing_components_fail(self):
        self.manifest["components"].append(copy.deepcopy(self.component("neon-engine")))
        self.assert_invalid("duplicate component")
        self.manifest["components"] = [c for c in self.manifest["components"]
                                       if c["id"] != "seaweedfs"]
        self.assert_invalid("missing initial component: seaweedfs")

    def test_probe_cannot_be_promoted_to_native_qualification(self):
        self.component("pysail")["qualification"]["linux-x86_64"]["state"] = "qualified"
        self.assert_invalid("qualified component has no artifact")
        self.assert_invalid("claim exceeds")

    def test_probe_cannot_be_reused_for_a_different_version_or_target(self):
        self.component("pysail")["selection"]["version"] = "0.7.2"
        self.assert_invalid("claim exceeds")
        self.component("pysail")["selection"]["version"] = "0.7.1"
        q = self.component("pysail")["qualification"]
        q["macos-arm64"] = copy.deepcopy(q["linux-x86_64"])
        self.assert_invalid("pysail/macos-arm64: claim exceeds")

    def test_unknown_fields_and_targets_fail(self):
        self.component("neon-engine")["qualifed"] = True
        self.assert_invalid("qualifed")
        del self.component("neon-engine")["qualifed"]
        del self.component("neon-engine")["qualification"]["macos-arm64"]
        self.assert_invalid("macos-arm64")

    def test_evidence_must_exist_and_cannot_escape_repo(self):
        q = self.component("pysail")["qualification"]["linux-x86_64"]
        q["evidence"] = ["missing-report.json"]
        self.assert_invalid("evidence must be a file")
        q["evidence"] = ["../outside.json"]
        self.assert_invalid("evidence")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            (Path(temp) / "outside.json").write_text("{}")
            (root / "report.json").symlink_to(Path(temp) / "outside.json")
            # Use the real fixture files for the other inventory checks.
            (root / "chart").symlink_to(ROOT / "chart", target_is_directory=True)
            (root / "spikes").symlink_to(ROOT / "spikes", target_is_directory=True)
            q["evidence"] = ["report.json"]
            errors = validate(self.manifest, root=root)
            self.assertIn("evidence must be a file inside the repo: report.json", errors)

    def test_legacy_image_drift_fails(self):
        self.manifest["legacy_images"]["images"][0]["reference"] = "example/neon@sha256:" + "0" * 64
        self.assert_invalid("digest inventory differs")

    def test_artifact_checksums_and_target_uniqueness(self):
        artifacts = self.component("neon-engine")["artifacts"]
        artifact = dict(target="linux-x86_64", url="https://example.org/engine.tar.gz",
                        sha256="pending", provenance="docs/plans/repository-map.md")
        artifacts.append(artifact)
        self.assert_invalid("sha256")
        artifact["sha256"] = "0" * 64
        artifacts.append(copy.deepcopy(artifact))
        self.assert_invalid("duplicate artifact target")

    def test_structurally_qualified_record_requires_pin_and_evidence(self):
        component = self.component("process-compose")
        q = component["qualification"]["linux-x86_64"]
        q.update(state="qualified", evidence=["docs/plans/repository-map.md"])
        component["artifacts"] = [dict(target="linux-x86_64",
            url="https://example.org/process-compose.tar.gz", sha256="0" * 64,
            provenance="docs/plans/repository-map.md")]
        self.assert_invalid("qualified component has no pin")
        component["selection"] = dict(kind="git", commit="1" * 40)
        self.assertEqual(validate(self.manifest), [])
        q["evidence"] = []
        self.assert_invalid("evidence")

    def test_duplicate_json_keys_fail(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            unique_object([("schema_version", 1), ("schema_version", 2)])


if __name__ == "__main__":
    unittest.main()
