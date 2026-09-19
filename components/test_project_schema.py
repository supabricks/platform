"""Checked PK01 schemas agree with the Rust output fixture and source example."""
import json
import hashlib
from pathlib import Path
import tomllib
import unittest
import jsonschema

ROOT = Path(__file__).resolve().parents[1]


class ProjectSchemas(unittest.TestCase):
    def schema(self, name):
        return json.loads((ROOT / 'schemas' / f'{name}.schema.json').read_text())

    def test_example_and_resource_fragment(self):
        root = ROOT / 'examples/projects/sales'
        for file, schema in [('supabricks.toml', 'project-v2'),
                             ('resources/database.toml', 'project-resources-v1')]:
            jsonschema.validate(tomllib.loads((root / file).read_text()), self.schema(schema))

    def test_rust_output_matches_public_schema(self):
        report = json.loads((ROOT / 'crates/local/tests/fixtures/project-inspection.json').read_text())
        jsonschema.validate(report, self.schema('project-inspection-v1'))
        self.assertEqual(report['source_sha256'], hashlib.sha256(
            json.dumps(report['files'], sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        del report['unresolved_bindings']
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(report, self.schema('project-inspection-v1'))

    def test_security_extensions_and_executable_resources_are_not_reserved_success(self):
        source = tomllib.loads((ROOT / 'examples/projects/sales/supabricks.toml').read_text())
        for patch in ({'run_as': 'admin'}, {'hooks': {'install': 'sh install.sh'}},
                      {'requires': {'capabilities': ['unity-catalog']}},
                      {'resources': {'jobs': {'unimplemented': {'kind': 'job'}}}}):
            with self.subTest(patch=patch), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(dict(source, **patch), self.schema('project-v2'))
