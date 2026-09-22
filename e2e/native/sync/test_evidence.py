from copy import deepcopy
import unittest
from qualify import REQUIRED, qualify


def report():
    return dict(format_version=1,status='PASS',slice='SY00',limitations=['probe only'],
        inputs=dict(release_manifest_sha256='a'*64,binary_sha256='b'*64,
                    engine_manifest=dict(neon_commit='1c6fa095261112aae239beef5a221b484703d49a')),
        checks=[dict(name=n,status='PASS') for n in sorted(REQUIRED|{'atomic_fixture_transaction_1'})],
        metrics=dict(columnar=dict(status='PASS',tables=[dict(apply_ms=10)]),
            **{key:dict(samples=30,p50_ms=1,p95_ms=2,p99_ms=3) for key in
               ('write_without_consumption','write_with_consumption','commit_ack_to_reference_apply')}))


class EvidenceTests(unittest.TestCase):
    def test_complete_report(self):self.assertGreater(qualify(report()),20)

    def test_missing_or_failed_checks(self):
        for name in REQUIRED:
            value=report();value['checks']=[c for c in value['checks'] if c['name']!=name]
            with self.subTest(name=name),self.assertRaises(ValueError):qualify(value)
        value=report();value['checks'][0]['status']='FAIL'
        with self.assertRaises(ValueError):qualify(value)

    def test_duplicate_checks_and_wrong_engine(self):
        value=report();value['checks'].append(deepcopy(value['checks'][0]))
        with self.assertRaises(ValueError):qualify(value)
        value=report();value['inputs']['engine_manifest']['neon_commit']='different'
        with self.assertRaises(ValueError):qualify(value)

    def test_failed_run_missing_limits_and_performance_budget(self):
        value=report();value['status']='FAIL'
        with self.assertRaises(ValueError):qualify(value)
        value=report();value['limitations']=[]
        with self.assertRaises(ValueError):qualify(value)
        value=report();value['metrics']['commit_ack_to_reference_apply'].update(p95_ms=1001,p99_ms=1002)
        with self.assertRaises(ValueError):qualify(value)
        value=report();value['metrics']['columnar']['tables'][0]['apply_ms']=5001
        with self.assertRaises(ValueError):qualify(value)


if __name__=='__main__':unittest.main()
