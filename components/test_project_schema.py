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

    def test_runnable_example_has_explicit_initialization(self):
        source = tomllib.loads((ROOT / 'examples/projects/sales-runnable/supabricks.toml').read_text())
        jsonschema.validate(source, self.schema('project-v2'))
        source['resources']['fixture']['sales']['mapping']['append'] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(source, self.schema('project-v2'))

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

    def test_package_report_and_content_digest_contract(self):
        report = json.loads((ROOT / 'crates/local/tests/fixtures/project-package.json').read_text())
        jsonschema.validate(report, self.schema('project-package-report-v1'))
        content = dict(format_version=1, profile='source', inspection=report['inspection'],
                       exclusions=report['exclusions'])
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(digest, report['content_sha256'])
        metadata = dict(content=content, content_sha256=digest)
        jsonschema.validate(metadata, self.schema('project-package-v1'))
        content['hooks'] = {'install': 'sh install.sh'}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(metadata, self.schema('project-package-v1'))

    def test_destination_deployment_context_is_versioned(self):
        context = json.loads((ROOT / 'crates/local/tests/fixtures/deployment-context.json').read_text())
        jsonschema.validate(context, self.schema('deployment-context-v1'))
        self.assertNotEqual(context['definition_id'], context['runtime_project_id'])
        self.assertEqual(context['actor_id'], context['effective_principal_id'])
        context['identity_provider'] = 'caller-supplied'
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(context, self.schema('deployment-context-v1'))

    def test_destination_plan_is_typed_and_digest_bound(self):
        plan = json.loads((ROOT / 'crates/local/tests/fixtures/project-plan.json').read_text())
        jsonschema.validate(plan, self.schema('project-plan-v1'))
        expected = plan['digest']
        plan['digest'] = ''
        self.assertEqual(expected, hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        plan['context']['actor_override'] = 'admin'
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(plan, self.schema('project-plan-v1'))
