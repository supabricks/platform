#!/usr/bin/env python3
"""Fail-closed release identity checks and real read-only/rollback qualification."""
import json
import unittest
from sqlite_probe import probe
from sqlite_qualification import CHECKS, POLICY, validate, wal_reset_fixed


class SQLiteQualification(unittest.TestCase):
    def identity(self, role):
        pin=json.loads(POLICY.read_text())['runtimes'][role]
        return dict(version=pin['version'], source_id=pin['source_id'],
                    compile_options=['THREADSAFE=1'], journal_mode='delete', synchronous=2,
                    checks=sorted(CHECKS))

    def test_advisory_thresholds_and_backports_are_branch_specific(self):
        for version in ('3.44.6','3.50.7','3.51.3','3.53.1','3.53.2'):
            self.assertTrue(wal_reset_fixed(version),version)
        for version in ('3.44.5','3.45.99','3.50.6','3.51.2','3.52.0','unknown','3.51.3-custom',None):
            self.assertFalse(wal_reset_fixed(version),version)

    def test_reviewed_runtime_identities_pass(self):
        for role in ('python','rust'):
            identity=self.identity(role)
            self.assertEqual(validate(identity,role),identity)

    def test_version_is_not_enough_without_the_exact_source_id(self):
        for role in ('python','rust'):
            for key,value in [('source_id','unreviewed build'),('version','3.53.4'),('compile_options',[]),('compile_options',['THREADSAFE=0']),('compile_options',['THREADSAFE=1','OMIT_WAL'])]:
                with self.subTest(role=role,key=key,value=value):
                    identity=self.identity(role);identity[key]=value
                    with self.assertRaises(ValueError):validate(identity,role)

    def test_python_cannot_change_mode_or_skip_functional_checks(self):
        for key,value in [('journal_mode','wal'),('synchronous',1),('checks',[])]:
            identity=self.identity('python');identity[key]=value
            with self.assertRaises(ValueError):validate(identity,'python')

    def test_real_probe_reopens_committed_state_and_refuses_read_only_writes(self):
        identity=probe()
        self.assertEqual(set(identity['checks']),CHECKS)
        self.assertEqual(identity['journal_mode'],'delete')
        self.assertEqual(identity['synchronous'],2)
        self.assertEqual(len(identity['python_executable_sha256']),64)
        self.assertTrue(identity['source_id'])
        self.assertTrue(identity['compile_options'])


if __name__=='__main__':unittest.main()
