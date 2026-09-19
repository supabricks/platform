"""Mixed source/target, incomplete portability and invented measurements fail closed."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from project_evidence import collect
from project_portability import CHECKS, TARGETS

HASH = 'a'*64
OTHER = 'b'*64


def fixture(root, targets):
    source = root / 'project-source'; source.mkdir()
    artifact = source / 'sales.sbproj'; artifact.write_bytes(b'synthetic evidence fixture')
    closures = {}
    env = dict(pyproject_sha256=HASH, lock_sha256=HASH, bundles={})
    for target in TARGETS:
        native = targets[target]
        manifest = dict(version=1, target=target, contract=native['kernel_contract_sha256'],
                        files={'pyproject.toml':HASH, 'uv.lock':HASH, 'wheels/sample-1-py3-none-any.whl':HASH})
        closures[target] = dict(status='passed', target=target, archive=native['archive'],
                               release_sha256=native['release_sha256'], bundle_sha256=HASH, manifest=manifest)
        env['bundles'][target] = dict(sha256=HASH, kernel_contract=manifest['contract'], wheels=1)
    producer = dict(status='passed', producer_target='linux-x86_64', producer_archive=targets['linux-x86_64']['archive'],
                    release_sha256=targets['linux-x86_64']['release_sha256'], package_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    content_sha256=HASH, source_sha256=HASH, environment=env, closures=closures,
                    fixtures={n:HASH for n in ('fixtures/sales.csv', 'migrations/001-marker.sql', 'migrations/002-marker.sql', 'notebooks/sales.ipynb', 'queries/sales_total.sql')},
                    measurements=dict(archive_bytes=artifact.stat().st_size, unpacked_bytes=1000, rows=2))
    (source / 'producer.json').write_text(json.dumps(producer))
    for target in TARGETS:
        native = targets[target]
        path = root / f'release-projects-{target}/projects.json'; path.parent.mkdir()
        path.write_text(json.dumps(dict(status='passed', checks=list(CHECKS), target=target, archive=native['archive'],
                         release_sha256=native['release_sha256'], producer=producer, network_evidence='loopback only',
                         previous_archive=dict(version='v0.1.0-alpha.22', target=target, sha256=HASH),
                         measurements=dict(producer['measurements'], prepare_seconds=2, start_seconds=3, peak_rss_bytes=100, disk_peak_bytes=1000))))


class ProjectEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.targets = {t:dict(release_sha256=HASH, kernel_contract_sha256=HASH,
                              archive=dict(version='alpha', target=t, sha256=HASH)) for t in TARGETS}
        fixture(self.root, self.targets)

    def change(self, name, mutate):
        path = self.root / name; data=json.loads(path.read_text()); mutate(data); path.write_text(json.dumps(data))

    def test_same_source_and_both_native_closures(self):
        result=collect(self.root,self.targets)
        self.assertEqual(result[TARGETS[0]]['package_sha256'],result[TARGETS[1]]['package_sha256'])

    def test_rebuilt_or_wrong_consumer_source_fails(self):
        self.change('release-projects-macos-arm64/projects.json',lambda d:d['producer'].update(package_sha256=OTHER))
        with self.assertRaisesRegex(ValueError,'different source'): collect(self.root,self.targets)

    def test_actual_artifact_must_match(self):
        (self.root/'project-source/sales.sbproj').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'): collect(self.root,self.targets)

    def test_missing_target_wrong_archive_contract_and_locks_fail(self):
        path=self.root/'project-source/producer.json'; original=path.read_text()
        for mutate in (lambda d:d['closures'].pop(TARGETS[1]),
                       lambda d:d['closures'][TARGETS[0]]['archive'].update(sha256=OTHER),
                       lambda d:d['closures'][TARGETS[0]]['manifest'].update(contract=OTHER),
                       lambda d:d['closures'][TARGETS[0]]['manifest']['files'].update({'uv.lock':OTHER})):
            path.write_text(original);self.change(str(path.relative_to(self.root)),mutate)
            with self.assertRaises(ValueError): collect(self.root,self.targets)

    def test_failure_partial_cleanup_and_zero_nonfinite_measurements_fail(self):
        path=self.root/'release-projects-linux-x86_64/projects.json'; original=path.read_text()
        for mutate in (lambda d:d.update(status='failed'), lambda d:d['checks'].pop(),
                       lambda d:d.update(checks=['other']*len(CHECKS)),lambda d:d.update(cleanup_failed=True),
                       lambda d:d.update(measurement_failed=True),lambda d:d.update(network_evidence='local; not isolated'),
                       lambda d:d['measurements'].update(prepare_seconds=0),lambda d:d['measurements'].update(start_seconds=float('nan')),
                       lambda d:d['measurements'].update(archive_bytes=999),lambda d:d['archive'].update(sha256=OTHER)):
            path.write_text(original);self.change(str(path.relative_to(self.root)),mutate)
            with self.assertRaises(ValueError): collect(self.root,self.targets)


if __name__ == '__main__': unittest.main()
