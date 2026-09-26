#!/usr/bin/env python3
"""Execute WAL fault tests using the verified installed capture implementation."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
TEST=ROOT/'python/analytics/test_spool_wal.py'
CHECKS=frozenset((
    'test_checked_full_policy_and_read_only_snapshot_does_not_block_append',
    'test_pinned_reader_causes_bounded_backpressure_then_recovers_same_group',
    'test_mode_changes_wait_for_reader_lease_and_preserve_committed_prefix',
    'test_legacy_unleased_sqlite_reader_also_prevents_downgrade',
    'test_wrong_identity_never_changes_wal_mode_or_discards_wal',
    'test_stopped_copy_preserves_committed_wal_then_sqlite_downgrades',
    'test_sidecar_symlinks_hardlinks_and_public_permissions_fail_before_open',
    'test_all_database_sidecars_are_counted_and_temp_spilling_disabled',
    'test_process_death_at_migration_and_checkpoint_boundaries',
    'test_unqualified_sqlite_is_rejected_before_a_wal_connection_opens',
))


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def execute(release):
    sys.path.insert(0,str(release/'python/analytics'))
    import capture.spool
    import capture.wal
    import incremental.storage
    for module in (capture.spool,capture.wal,incremental.storage):
        assert Path(module.__file__).resolve().is_relative_to(release/'python/analytics')
    manifest=json.loads((release/'release.json').read_text())
    for name in ('capture/spool.py','capture/wal.py','incremental/storage.py'):
        assert digest(release/'python/analytics'/name)==manifest['files']['python/analytics/'+name]['sha256']
    spec=importlib.util.spec_from_file_location('wal_fault_tests',TEST)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(module.WalTests)
    assert {test._testMethodName for test in suite}==CHECKS
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    assert result.wasSuccessful() and result.testsRun==len(CHECKS)
    return dict(status='passed',release_identity=digest(release/'release.json'),
        test_sha256=digest(TEST),checks=sorted(CHECKS),journal_mode='wal',synchronous=2)


def collect(release):
    result=subprocess.run([str(release/'python/analytics/python'),'-I','-B',str(Path(__file__).resolve()),'--release',str(release)],text=True,capture_output=True,timeout=120)
    if result.returncode:raise RuntimeError('installed capture WAL qualification failed: '+result.stderr[-8000:])
    value=json.loads(result.stdout);return validate(value,digest(release/'release.json'))


def validate(value,identity):
    if not isinstance(value,dict) or value.get('status')!='passed' or value.get('release_identity')!=identity or value.get('test_sha256')!=digest(TEST) or value.get('checks')!=sorted(CHECKS) or value.get('journal_mode')!='wal' or value.get('synchronous')!=2:
        raise ValueError('missing or mixed installed capture WAL fault evidence')
    return {key:value[key] for key in ('status','release_identity','test_sha256','checks','journal_mode','synchronous')}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--release',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(execute(args.release.resolve())))
