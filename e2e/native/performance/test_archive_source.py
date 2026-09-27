"""Stopped incomplete campaigns remain auditable without becoming accepted runs."""
import fcntl
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from archive_source import archive


class SourceArchive(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source'
        self.source.mkdir()
        (self.source/'host').mkdir()
        (self.source/'host/0000.jsonl').write_text('{"active_builds":[]}\n')
        self.harness = self.root/'harness'
        scripts = self.harness/'e2e/native/performance'
        scripts.mkdir(parents=True)
        (scripts/'source_trial.py').write_text('# frozen fixture\n')
        self.trial = self.source/'01-attempt1-cpu8-clients4-profile1'
        self.trial.mkdir()
        data = b'{"status":"measured"}\n'
        (self.trial/'trial.json').write_bytes(data)
        (self.trial/'private.log').write_text('secret fixture')
        (self.trial/'scratch').mkdir()
        (self.trial/'scratch/credentials.json').write_text('secret fixture')
        self.record = dict(state='between_blocks', config=dict(harness=str(self.harness), release='/private/runtime'),
            attempts=[dict(accepted=False, results=[dict(directory=self.trial.name,
                evidence_sha256={'trial.json':hashlib.sha256(data).hexdigest()})])], blocks=[])
        self.save()
        self.dest = self.root/'archive'

    def save(self):
        (self.source/'experiment.json').write_text(json.dumps(self.record))

    def test_partial_requires_opt_in_and_preserves_exclusions(self):
        with self.assertRaises(AssertionError):archive(self.source,self.dest)
        archive(self.source,self.dest,allow_incomplete=True)
        result=json.loads((self.dest/'experiment.json').read_text())
        self.assertEqual(result['state'],'between_blocks')
        self.assertEqual(result['blocks'],[])
        self.assertFalse(result['attempts'][0]['accepted'])
        self.assertFalse(result['export']['capacity_qualification'])
        self.assertFalse(result['export']['cleanup_established_by_export'])
        self.assertFalse((self.dest/self.trial.name/'private.log').exists())
        self.assertFalse((self.dest/self.trial.name/'scratch').exists())
        self.assertEqual(result['config']['harness'],'<frozen-harness>')

    def test_crash_before_result_keeps_unrecorded_cleanup(self):
        self.record['state']='running';self.save()
        (self.trial/'cleanup.json').write_text('{"exit_code":1}')
        archive(self.source,self.dest,allow_incomplete=True)
        result=json.loads((self.dest/'experiment.json').read_text())
        self.assertIn(self.trial.name+'/cleanup.json',result['export']['unrecorded_artifacts'])
        self.assertEqual(result['state'],'running')

    def test_active_controller_cannot_be_archived(self):
        with (self.source/'.controller.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):archive(self.source,self.dest,allow_incomplete=True)
        self.assertFalse(self.dest.exists())

    def test_corrupt_recorded_evidence_is_rejected(self):
        (self.trial/'trial.json').write_text('{"status":"changed"}')
        with self.assertRaises(AssertionError):archive(self.source,self.dest,allow_incomplete=True)

    def test_completed_export_keeps_existing_contract(self):
        self.record['state']='complete';self.save()
        archive(self.source,self.dest)
        result=json.loads((self.dest/'experiment.json').read_text())
        self.assertEqual(result['state'],'complete')
        self.assertNotIn('incomplete',result['export'])
        for line in (self.dest/'SHA256SUMS').read_text().splitlines():
            digest,name=line.split(maxsplit=1)
            self.assertEqual(hashlib.sha256((self.dest/name).read_bytes()).hexdigest(),digest)
