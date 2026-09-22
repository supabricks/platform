import copy
import json
import unittest
from governed_evidence import collect, REQUIRED, PROFILE, ROOT, TOP_CHECKS, digest

HASH='a'*64

def fixture(env):
    pins=json.loads((ROOT/'components/execution-runtime.lock.json').read_text())
    suites={name:dict(status='passed',exit_code=0,checks=sorted(required),release_identity=env['release_sha256'],
        binary_sha256=HASH,report_sha256=HASH,cleanup=dict(exit_code=0,timed_out=False,leaked_descendants=0,
            remaining_descendants=0,descendants_observed=5)) for name,required in REQUIRED.items()}
    suites['upgrade']['predecessor_identity']=pins['platform']['inventory_sha256']
    suites['identity']['measurements']=dict(idp_disable=dict(acknowledged_deny_monotonic=10,observed_closed_monotonic=12))
    suites['browser'].update(tls=True,exact_installed=True,console_manifest_sha256=env['source']['console']['manifest_sha256'],
        execution_memory=[dict(current_bytes=800000000,peak_bytes=1000000000)]*2,
        revocation_observed_ms=1200,resource_envelope=dict(concurrent_executions=2,memory_bytes=2147483648,cpu_count=2,pids=512,scratch_bytes=536870912))
    return dict(schema_version=1,status='passed',profile=PROFILE,target='linux-x86_64',
        checks=sorted(TOP_CHECKS),host_capacity=dict(cpu_count=4,memory_bytes=16*1024**3),
        release_identity=env['release_sha256'],archive=env['archive'],source=env['source'],binary_sha256=HASH,
        console_manifest_sha256=env['source']['console']['manifest_sha256'],qualifier_image='sha256:'+HASH,
        network=dict(mode='none',external_attempts=2,external_denials=2),cleanup=dict(leaked_containers=0,remaining_containers=0),
        components=dict(keycloak=json.loads((ROOT/'e2e/native/iam/pins.lock.json').read_text())['keycloak']['image'],
            rootfs_image=pins['rootfs'],rootfs_sha256=HASH,gvisor_inventory_sha256=pins['gvisor']['inventory_sha256'],
            execution_config_sha256=HASH,execution_release_identity=env['release_sha256'],
            execution_pin_sha256=digest(ROOT/'components/execution-runtime.lock.json'),
            installer_template_sha256=digest(ROOT/'install/native/install.sh.in'),
            supervisor_sha256=digest(ROOT/'python/execution/supervisor.py'),workload_sha256=digest(ROOT/'python/execution/workload.py')),
        suites=suites)


class GovernedEvidence(unittest.TestCase):
    def setUp(self):
        self.env=dict(release_sha256=HASH,archive=dict(target='linux-x86_64',version='candidate',sha256=HASH),
                      source=dict(console=dict(manifest_sha256=HASH)))
        self.data=fixture(self.env)

    def test_complete_evidence_is_sanitized(self):
        self.data['private_token']='qualification-private-token'
        self.data['suites']['data']['sql']='qualification-private-token'
        self.data['host_capacity']['private_token']='qualification-private-token'
        self.data['suites']['browser']['execution_memory'][0]['private_token']='qualification-private-token'
        result=collect(self.data,self.env)
        self.assertEqual(result['status'],'passed')
        self.assertNotIn('qualification-private-token',json.dumps(result))

    def test_stale_archive_inherited_runtime_partial_or_failed_evidence_is_rejected(self):
        mutations=[
            lambda d:d.update(release_identity='b'*64),
            lambda d:d['components'].update(execution_release_identity='b'*64),
            lambda d:d['components'].update(gvisor_inventory_sha256='b'*64),
            lambda d:d['components'].update(workload_sha256='b'*64),
            lambda d:d['components'].update(installer_template_sha256='b'*64),
            lambda d:d['suites']['browser'].update(binary_sha256='b'*64),
            lambda d:d['suites']['browser'].update(tls=False),
            lambda d:d['suites']['browser'].update(exact_installed=False),
            lambda d:d['suites']['browser']['checks'].remove('sandbox_bypass_and_scratch_quota_denied'),
            lambda d:d['suites']['data']['checks'].remove('audit_append_failure_rolls_back_identity_mutation'),
            lambda d:d['suites']['identity'].update(exit_code=1),
            lambda d:d['suites']['browser'].update(revocation_observed_ms=60001),
            lambda d:d['suites']['browser'].update(revocation_observed_ms=float('nan')),
            lambda d:d['suites']['browser']['resource_envelope'].update(memory_bytes=4*1024**3),
            lambda d:d['suites']['identity']['measurements']['idp_disable'].update(observed_closed_monotonic=310),
            lambda d:d['suites']['data']['cleanup'].update(leaked_descendants=1),
            lambda d:d['cleanup'].update(leaked_containers=1),
            lambda d:d['network'].update(mode='bridge'),
            lambda d:d['network'].update(external_denials=1),
            lambda d:d.update(target='macos-arm64'),
            lambda d:d['checks'].remove('installed_private_runtime_preparation_without_downloads'),
            lambda d:d['host_capacity'].update(cpu_count=8),
            lambda d:d['host_capacity'].update(memory_bytes=32*1024**3),
            lambda d:d['suites']['browser'].update(execution_memory=[]),
            lambda d:d['suites']['upgrade'].update(predecessor_identity='b'*64),
        ]
        for i,mutate in enumerate(mutations):
            with self.subTest(case=i):
                data=copy.deepcopy(self.data);mutate(data)
                with self.assertRaises(ValueError):collect(data,self.env)

if __name__=='__main__':unittest.main()
