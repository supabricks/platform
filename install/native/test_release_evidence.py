"""Completion cannot be inferred from green but partial or mixed-archive reports."""
import json
from pathlib import Path
import tempfile
import unittest

from demo import FILES
from catalog_evidence import expected_build, REQUIRED, MINIMUM, digest, ROOT
from test_sail import sample_report
from environment_evidence import SUITES, MACOS_NETWORK_TRANSITION
from release_evidence import collect, markdown
from test_project_evidence import fixture as project_fixture
from test_governed_evidence import fixture as governed_fixture

HASH = 'a' * 64
OTHER = 'b' * 64


class ReleaseEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target in ('linux-x86_64', 'macos-arm64'):
            for suite, files in SUITES.items():
                for name, minimum in files.items():
                    data = dict(status='passed', release_sha256=HASH)
                    if isinstance(minimum, dict):
                        data.update({k:dict(status='passed',checks=self.checks(v)) for k,v in minimum.items()})
                    else:
                        data['checks'] = self.checks(minimum)
                    if suite == 'release-environment-lifecycle' and name == 'qualification.json':
                        data.update(target=target, source=dict(sail=sample_report(target),platform_commit='reviewed',platform_dirty=False,
                            console=dict(manifest_sha256=HASH,package_lock_sha256=HASH,source=dict(commit='console',dirty=False,manifest_sha256=HASH,package_lock_sha256=HASH)),
                            unity_catalog=dict(build_sha256=HASH,source_commit=expected_build(target)['source_commit'],metadata_backend='h2-2.2.224',profile='local-owner-files'),
                            ingestion=dict(worker_sha256=HASH),data_formats=dict(local_catalog=25,postgres_major=17)),
                            archives=dict(new=dict(version='alpha',target=target,sha256=HASH),old={}),
                            release_identity=HASH,python_version='3.12',kernel_contract_sha256=HASH,
                            wheels={},notices={'licenses/platform.txt':HASH},measurements={},project_bundle={})
                        if target == 'macos-arm64':
                            data['network_transition'] = dict(MACOS_NETWORK_TRANSITION)
                    self.write(target,suite,name,data)
            self.write(target,'release-console','console.json',dict(status='passed',checks=self.checks(39)+['PK06 check-'+str(n) for n in range(10)],
                release_sha256=HASH,release_identity=HASH,release_version='alpha',browser='153.0.1.2',network_qualification='isolated',demo={name:HASH for name in FILES}))
            measured=dict(source_bytes=100,rows=2,duration_ms=10,peak_rss_bytes=100)
            self.write(target,'release-ingest','ingest.json',dict(status='passed',checks=self.checks(39),
                release=dict(identity=HASH,version='alpha',verified=True),network_evidence='isolated',qualification=dict(source_sha256=HASH,**measured),
                formats={name:dict(source_sha256=HASH,budget=dict(source_sha256=HASH,**measured)) for name in ('jsonl','json','parquet')}))
            self.write(target,'release-recovery','recovery.json',dict(status='passed',checks=self.checks(18),release_identity=HASH,network_qualification='isolated'))
            for name in ('qualification.json','benchmarks.json'):
                self.write(target,'release-qualification',name,dict(status='passed',checks=self.checks(12),release_identity=HASH,network_qualification='isolated',
                    measurements={f'snapshot_{size}_bytes{suffix}':dict(elapsed_seconds=1,peak_rss_bytes=100,logical_cpus=4,host_memory_bytes=1000) for size in (10000000,100000000,1000000000) for suffix in ('','_query')}))
            self.write(target,'release-qualification','network.json',dict(status='passed',observed_destinations=123,external_destinations=[]))

        for target in ('linux-x86_64', 'macos-arm64'):
            env=json.loads((self.root/f'release-environment-lifecycle-{target}/qualification.json').read_text())
            build=expected_build(target)
            suites={name:dict(status='PASS',exit_code=0,report_sha256=HASH,
                cleanup=dict(exit_code=0,timed_out=False,leaked_descendants=0,remaining_descendants=0,descendants_observed=5),
                checks=sorted(required)+self.checks(MINIMUM[name])) for name,required in REQUIRED.items()}
            suites['recovery']['checks'].append('bounded_filesystem_enospc_publishes_no_backup_and_preserves_source')
            self.write(target,'release-catalog','catalog.json',dict(status='passed',checks=self.checks(5),
                release_identity=HASH,archive=env['archives']['new'],source=env['source'],
                network_evidence='isolated',unity_catalog=build,uc_build_sha256=HASH,
                demo={'CATALOG-DEMO.md':digest(ROOT/'docs/handbook/catalog-demo.md')},
                contract=dict(backend_schema=1,publication_manifest=1,server_commit=build['source_commit'],capability_profile='local-owner-files-v1'),
                suites=suites,measurements=dict(peak_rss_bytes=100,catalog_peak_rss_bytes=50,catalog_processes_observed=2,duration_seconds=3)))

        project_fixture(self.root, {t:dict(release_sha256=HASH, kernel_contract_sha256=HASH, archive=dict(version='alpha',target=t,sha256=HASH)) for t in ('linux-x86_64','macos-arm64')})

        env=json.loads((self.root/'release-environment-lifecycle-linux-x86_64/qualification.json').read_text())
        self.write('linux-x86_64','release-governed','governed.json',governed_fixture(dict(release_sha256=HASH,archive=env['archives']['new'],source=env['source'])))

    @staticmethod
    def checks(count):
        return [f'check-{n}' for n in range(count)]

    def write(self,target,suite,name,data):
        path=self.root/f'{suite}-{target}'/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(data))

    def change(self,suite,name,transform):
        path=self.root/f'{suite}-linux-x86_64'/name
        data=json.loads(path.read_text()); transform(data); path.write_text(json.dumps(data))

    def collect(self):
        return collect(self.root,'reviewed','console',HASH,'alpha')

    def test_complete_release_includes_all_suites_and_sanitized_metrics(self):
        self.change('release-ingest','ingest.json',lambda d:d['formats']['json']['budget'].update(sql='secret SQL',rows_payload=['secret rows'],path='/private/source'))
        report=self.collect()
        self.assertEqual(len(report['targets']['linux-x86_64']['reports']),17)
        self.assertEqual(len(report['targets']['macos-arm64']['reports']),14)
        self.assertNotIn('secret SQL',json.dumps(report))
        self.assertNotIn('secret rows',json.dumps(report))
        self.assertNotIn('/private',json.dumps(report))
        self.assertIn('worker interval',markdown(report))

    def test_mixed_ingestion_recovery_baseline_or_benchmark_fails(self):
        for suite,name,mutate in [
            ('release-ingest','ingest.json',lambda d:d['release'].update(identity=OTHER)),
            ('release-recovery','recovery.json',lambda d:d.update(release_identity=OTHER)),
            ('release-qualification','qualification.json',lambda d:d.update(release_identity=OTHER)),
            ('release-qualification','benchmarks.json',lambda d:d.update(release_identity=OTHER))]:
            with self.subTest(suite=suite,name=name):
                path=self.root/f'{suite}-linux-x86_64'/name; old=path.read_text()
                self.change(suite,name,mutate)
                with self.assertRaisesRegex(ValueError,'mixed release'):self.collect()
                path.write_text(old)

    def test_old_or_duplicate_browser_coverage_cannot_pass(self):
        for checks in (self.checks(39), ['same']*49):
            self.change('release-console','console.json',lambda d:d.update(checks=checks))
            with self.assertRaisesRegex(ValueError,'incomplete checks'):self.collect()

    def test_packaging_coverage_cannot_be_replaced_by_other_checks(self):
        self.change('release-console','console.json',lambda d:d.update(checks=self.checks(49)))
        with self.assertRaisesRegex(ValueError,'missing PK06'):self.collect()

    def test_wrong_console_worker_version_and_demo_are_rejected(self):
        path=self.root/'release-environment-lifecycle-linux-x86_64/qualification.json'
        old=path.read_text()
        for mutate in (lambda d:d['source']['console']['source'].update(commit='stale'),
                       lambda d:d['source']['console']['source'].update(dirty=True),
                       lambda d:d['source']['console']['source'].update(manifest_sha256=OTHER),
                       lambda d:d['source']['sail'].update(commit='stale'),
                       lambda d:d['source']['ingestion'].update(worker_sha256=OTHER),
                       lambda d:d['archives']['new'].update(version='old')):
            self.change('release-environment-lifecycle','qualification.json',mutate)
            with self.assertRaises(ValueError):self.collect()
            path.write_text(old)
        self.change('release-console','console.json',lambda d:d.update(demo={'DEMO.md':HASH}))
        with self.assertRaisesRegex(ValueError,'demo'):self.collect()

    def test_cleanup_network_and_measurement_failures_are_fatal(self):
        for suite,name,mutate in [
            ('release-notebooks','notebooks.json',lambda d:d['product'].update(cleanup_errors=['failed'])),
            ('release-recovery','recovery.json',lambda d:d.update(cleanup_errors=['owned process still alive'])),
            ('release-qualification','network.json',lambda d:d.update(external_destinations=['1.1.1.1'])),
            ('release-qualification','network.json',lambda d:d.update(observed_destinations=0)),
            ('release-ingest','ingest.json',lambda d:d['formats']['json']['budget'].update(source_sha256='missing')),
            ('release-ingest','ingest.json',lambda d:d['formats']['json']['budget'].update(duration_ms=0)),
            ('release-ingest','ingest.json',lambda d:d['formats']['json']['budget'].update(peak_rss_bytes=float('nan')))]:
            with self.subTest(suite=suite,name=name):
                path=self.root/f'{suite}-linux-x86_64'/name;old=path.read_text();self.change(suite,name,mutate)
                with self.assertRaises(ValueError):self.collect()
                path.write_text(old)

    def test_catalog_cannot_qualify_mixed_partial_or_unmeasured_archive(self):
        path=self.root/'release-catalog-linux-x86_64/catalog.json'
        original=path.read_text()
        mutations=[
            lambda d:d.update(release_identity=OTHER),
            lambda d:d.update(demo={}),
            lambda d:d['demo'].update({'CATALOG-DEMO.md':OTHER}),
            lambda d:d['archive'].update(sha256=OTHER),
            lambda d:d['unity_catalog'].update(source_commit='stale'),
            lambda d:d['unity_catalog']['java'].update(version='system Java'),
            lambda d:d['contract'].update(backend_schema=999),
            lambda d:d['suites']['browser'].update(checks=self.checks(50)),
            lambda d:d['suites']['recovery']['checks'].remove('bounded_filesystem_enospc_publishes_no_backup_and_preserves_source'),
            lambda d:d['measurements'].update(catalog_processes_observed=0),
            lambda d:d.update(failure={'gate':'recovery'}),
            lambda d:d['suites']['service']['cleanup'].update(leaked_descendants=1),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.change('release-catalog','catalog.json',mutate)
                with self.assertRaises(ValueError): self.collect()
                path.write_text(original)

    def test_missing_report_or_benchmark_cannot_pass(self):
        path = self.root/'release-qualification-linux-x86_64/benchmarks.json'
        original = path.read_text()
        self.change('release-qualification','benchmarks.json',lambda d:d.update(measurements={}))
        with self.assertRaisesRegex(ValueError,'benchmark'):self.collect()
        path.write_text(original)
        (self.root/'release-ingest-macos-arm64/ingest.json').unlink()
        with self.assertRaises(FileNotFoundError):self.collect()


if __name__ == '__main__': unittest.main()
