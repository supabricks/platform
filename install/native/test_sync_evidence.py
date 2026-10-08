import copy
import json
import hashlib
from sqlite_qualification import POLICY, CHECKS
from capture_wal_qualification import CHECKS as WAL_CHECKS, TEST as WAL_TEST, digest as wal_digest
import unittest
from sync_evidence import collect, digest, NETWORK, REQUIRED, ROOT, TOP_CHECKS, WORKERS

HASH='a'*64
REVISION='b'*40


def fixture(target='linux-x86_64'):
    base=dict(release_sha256=HASH,archive=dict(version='candidate',target=target,sha256=HASH))
    suites={name:dict(status='PASS',exit_code=0,exact_installed=True,checks=sorted(checks),
        release_identity=HASH,binary_sha256=HASH,report_sha256=HASH,
        cleanup=dict(exit_code=0,timed_out=False,descendants_observed=10,leaked_descendants=0,remaining_descendants=0))
        for name,checks in REQUIRED.items()}
    suites['append']['metrics']=dict(rows=16777216,insert_rows=16384,
        append=dict(elapsed_seconds=1,planning_seconds=.9,apply_seconds=.1,highwater_bytes=400000000,
                    metrics=dict(operation='WRITE',committed_before_receipt=True)),
        replay=dict(elapsed_seconds=.1,planning_seconds=.01,apply_seconds=.09,highwater_bytes=200000000,
                    metrics=dict(replayed=True,apply_kind='append')))
    suites['capacity']['metrics']=dict(rows=65536,insert_rows=16384,payload_width=8192,source_bytes=600*1024**2,
        history=dict(last_version=512,appended_rows=511,highwater_bytes=300*1024**2,elapsed_seconds=10,commit_seconds=[.01]*511),
        **{phase:dict(elapsed_seconds=10,initialization_seconds=9,source_estimate_bytes=1200*1024**2,
            generation_bytes=601*1024**2,highwater_bytes=300*1024**2) for phase in ('apply','replay')})
    suites['continuous'].update(host=dict(system='Linux' if target=='linux-x86_64' else 'Darwin',
        machine='x86_64' if target=='linux-x86_64' else 'arm64',cpu_count=4,memory_bytes=16*1024**3),
        metrics=dict(workload=dict(tables=2,rows_per_table=10000,transactions=750,changed_rows=1500,
            target_rows_per_second=50,achieved_rows_per_second=50,elapsed_seconds=30,
            commit_to_publication_ms=dict(p50=1000,p95=2500,p99=3500),
            oltp_transaction_ms=dict(p50=1,p95=2,p99=3),burst_changed_rows=200,burst_commit_to_publication_ms=2000),
            baseline_oltp_transaction_ms=dict(p50=.5,p95=1,p99=2),
            resources=dict(peak_owned_rss_bytes=1000000000,peak_allocated_data_bytes=2000000000,
                spool_bytes=1000000,retained_wal_bytes=1000000,sampled_owned_cpu_seconds_lower_bound=20),
            storage=dict(input_bytes=1000,new_parquet_bytes=2000,write_amplification_ratio=2,
                peak_inventory_files=50,peak_generation_bytes=1000000,compaction_bytes=0)))
    data=dict(status='passed',checks=sorted(TOP_CHECKS),release_identity=HASH,archive=base['archive'],binary_sha256=HASH,
        source=dict(platform_commit=REVISION,platform_dirty=False,data_formats=dict(local_catalog=31)),
        network_evidence=NETWORK[target],worker_inventory={name:digest(ROOT/'python/analytics'/name) for name in WORKERS},suites=suites)
    policy=json.loads(POLICY.read_text())
    identities={role:dict(version=pin['version'],source_id=pin['source_id'],compile_options=['THREADSAFE=1'],
        journal_mode='delete',synchronous=2,checks=sorted(CHECKS)) for role,pin in policy['runtimes'].items()}
    data['capture_wal']=dict(status='passed',release_identity=HASH,test_sha256=wal_digest(WAL_TEST),checks=sorted(WAL_CHECKS),journal_mode='wal',synchronous=2)
    data['sqlite']=dict(format_version=1,status='passed',release_identity=HASH,
        policy_sha256=hashlib.sha256(POLICY.read_bytes()).hexdigest(),**identities,
        qualification_reader=dict(identity=identities['python'],wal_reset_fixed=True),
        probe_journal_mode='delete',probe_synchronous=2,wal_mode_qualified=False)
    return data,base


class SyncEvidence(unittest.TestCase):
    def test_both_complete_targets_and_sanitized_measurements(self):
        for target in NETWORK:
            with self.subTest(target=target):
                data,base=fixture(target)
                data['private']='private-row-or-token'
                data['sqlite']['python']['private']='private-row-or-token'
                data['suites']['continuous']['metrics']['private']='private-row-or-token'
                data['suites']['continuous']['host']['private']='private-row-or-token'
                data['suites']['append']['metrics']['append']['private']='private-row-or-token'
                result=collect(data,base,REVISION,target)
                self.assertEqual(result['status'],'passed')
                self.assertEqual(result['oltp_p95_ratio'],2)
                self.assertNotIn('private-row-or-token',json.dumps(result))

    def test_partial_mixed_source_override_or_leaked_evidence_is_rejected(self):
        mutations=[
            lambda d:d.pop('capture_wal'),lambda d:d['capture_wal'].update(release_identity='c'*64),lambda d:d['capture_wal'].update(checks=[]),
            lambda d:d.pop('sqlite'),lambda d:d['sqlite'].update(release_identity='c'*64),
            lambda d:d['sqlite']['python'].update(source_id='unreviewed'),
            lambda d:d['sqlite'].update(policy_sha256='c'*64),
            lambda d:d['sqlite'].update(wal_mode_qualified=True),
            lambda d:d['sqlite']['qualification_reader'].update(wal_reset_fixed=False),
            lambda d:d.update(status='partial'),lambda d:d.update(failure='failed'),
            lambda d:d.update(release_identity='c'*64),lambda d:d.update(archive={}),
            lambda d:d.update(binary_sha256='missing'),lambda d:d['source'].update(platform_commit='c'*40),
            lambda d:d['source'].update(platform_dirty=True),lambda d:d['source']['data_formats'].update(local_catalog=28),
            lambda d:d.update(network_evidence='host online'),lambda d:d['checks'].clear(),
            lambda d:d['worker_inventory'].update({'capture_worker.py':'c'*64}),
            lambda d:d['worker_inventory'].pop('incremental/planning.py'),
            lambda d:d['worker_inventory'].update({'incremental/planning.py':'c'*64}),
            lambda d:d['suites'].pop('capacity'),lambda d:d['suites']['capacity']['checks'].pop(),
            lambda d:d['suites']['capacity'].pop('metrics'),
            lambda d:d['suites']['capacity']['metrics'].update(source_bytes=1024),
            lambda d:d['suites']['capacity']['metrics']['apply'].update(generation_bytes=1024**3),
            lambda d:d['suites']['capacity']['metrics']['replay'].update(highwater_bytes=768*1024**2),
            lambda d:d['suites']['capacity']['metrics']['history'].update(last_version=64),
            lambda d:d['suites']['capacity']['metrics']['history'].update(highwater_bytes=768*1024**2),
            lambda d:d['suites']['capacity']['metrics']['history']['commit_seconds'].pop(),
            lambda d:d['suites'].pop('append'),lambda d:d['suites']['append']['checks'].pop(),
            lambda d:d['suites']['append'].pop('metrics'),
            lambda d:d['suites']['append']['metrics'].update(rows=100),
            lambda d:d['suites']['append']['metrics']['append'].update(highwater_bytes=768*1024**2),
            lambda d:d['suites']['append']['metrics']['append'].update(planning_seconds=None),
            lambda d:d['suites']['append']['metrics']['append']['metrics'].update(operation='MERGE'),
            lambda d:d['suites']['append']['metrics']['replay']['metrics'].update(replayed=False),
            lambda d:d['suites'].pop('merge'),lambda d:d['suites']['merge']['checks'].pop(),
            lambda d:d['suites'].pop('bulk'),lambda d:d['suites']['bulk']['checks'].pop(),
            lambda d:d['suites'].pop('date'),lambda d:d['suites']['char']['checks'].pop(),
            lambda d:d['suites'].pop('composite'),lambda d:d['suites']['composite']['checks'].pop(),
            lambda d:d['worker_inventory'].pop('capture/source.py'),lambda d:d['worker_inventory'].pop('incremental/rows.py'),
            lambda d:d['suites'].pop('maintenance'),lambda d:d['suites']['triggered'].update(exact_installed=False),
            lambda d:d['suites']['triggered'].update(status='FAIL'),lambda d:d['suites']['governed'].update(exit_code=1),
            lambda d:d['suites']['continuous'].update(release_identity='c'*64),
            lambda d:d['suites']['maintenance'].update(binary_sha256='c'*64),
            lambda d:d['suites']['maintenance']['checks'].pop(),
            lambda d:d['suites']['maintenance']['checks'].append(d['suites']['maintenance']['checks'][0]),
            lambda d:d['suites']['maintenance']['cleanup'].update(leaked_descendants=1),
            lambda d:d['suites']['maintenance']['cleanup'].update(timed_out=True),
            lambda d:d['suites']['maintenance']['cleanup'].update(descendants_observed=0),
            lambda d:d['suites']['maintenance'].update(report_sha256=None),
        ]
        self.reject(mutations)

    def test_missing_measurements_changed_workload_and_missed_thresholds_are_rejected(self):
        def work(d):return d['suites']['continuous']['metrics']['workload']
        def metrics(d):return d['suites']['continuous']['metrics']
        self.reject([
            lambda d:work(d).update(transactions=749),lambda d:work(d).update(rows_per_table=100),
            lambda d:work(d).update(elapsed_seconds=34),lambda d:work(d).update(achieved_rows_per_second=100),
            lambda d:work(d).update(burst_commit_to_publication_ms=5001),
            lambda d:work(d)['commit_to_publication_ms'].update(p95=5001,p99=6000),
            lambda d:work(d)['commit_to_publication_ms'].update(p95=float('nan')),
            lambda d:work(d)['oltp_transaction_ms'].update(p99=float('inf')),
            lambda d:work(d)['oltp_transaction_ms'].update(p50=-1),
            lambda d:work(d)['oltp_transaction_ms'].update(p50=5),
            lambda d:metrics(d)['baseline_oltp_transaction_ms'].update(p95=0),
            lambda d:metrics(d)['resources'].pop('spool_bytes'),
            lambda d:metrics(d)['storage'].update(input_bytes=0),
            lambda d:metrics(d)['storage'].update(write_amplification_ratio=4),
            lambda d:d['suites']['continuous']['host'].update(machine='arm64'),
            lambda d:d['suites']['continuous']['host'].update(memory_bytes=0),
        ])

    def reject(self,mutations):
        original,base=fixture()
        for index,mutate in enumerate(mutations):
            with self.subTest(case=index):
                data=copy.deepcopy(original);mutate(data)
                with self.assertRaises(ValueError):collect(data,base,REVISION,'linux-x86_64')


if __name__=='__main__':unittest.main()
