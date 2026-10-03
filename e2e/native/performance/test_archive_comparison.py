"""Failed comparisons retain diagnostics without becoming accepted results."""
import fcntl
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive_comparison import archive


class ComparisonArchive(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root/'source'
        self.source.mkdir()
        (self.source/'host').mkdir()
        self.host = b'{"free_bytes":0}\n{"partial":'
        (self.source/'host/0000.jsonl').write_bytes(self.host)
        self.folder = self.source/'01-attempt01-predecessor'
        self.trial = self.folder/'01-cpu16-rate1250-r1'
        self.trial.mkdir(parents=True)
        (self.folder/'matrix.json').write_text('{"state":"interrupted","release":"/private/runtime"}')
        (self.trial/'cleanup.json').write_text('{"partial":')
        (self.trial/'private.log').write_text('secret')
        (self.folder/'scratch').mkdir()
        (self.folder/'scratch/credentials.json').write_text('secret')
        self.record = dict(state='running_trial', config=dict(arms=dict(predecessor=dict(
            harness='/private/harness', release='/private/runtime', package=dict(release_identity='abc123')))),
            attempts=[dict(accepted=False,results={})],pairs=[])
        self.save()
        self.dest = self.root/'archive'

    def save(self):
        (self.source/'experiment.json').write_text(json.dumps(self.record))

    def test_incomplete_is_opt_in_and_preserves_failure(self):
        with self.assertRaisesRegex(ValueError, 'explicit'):archive(self.source, self.dest)
        with patch('archive_comparison.comparison_report') as report:
            archive(self.source, self.dest, allow_incomplete=True)
            report.assert_not_called()
        result=json.loads((self.dest/'experiment.json').read_text())
        self.assertEqual(result['state'],'running_trial')
        self.assertEqual(result['pairs'],[])
        self.assertFalse(result['export']['performance_qualification'])
        self.assertFalse(result['export']['cleanup_established_by_export'])
        self.assertEqual(gzip.decompress((self.dest/'host/0000.jsonl.gz').read_bytes()), self.host)
        self.assertFalse((self.dest/'comparison.json').exists())
        self.assertFalse(any(p.name in ('private.log','scratch') for p in self.dest.rglob('*')))
        matrix=self.dest/self.folder.name/'matrix.json'
        self.assertNotIn('/private/runtime',matrix.read_text())
        partial=self.dest/self.trial.relative_to(self.source)/'cleanup.json'
        self.assertEqual(partial.read_text(),'{"partial":')
        for line in (self.dest/'SHA256SUMS').read_text().splitlines():
            digest,relative=line.split(maxsplit=1)
            self.assertEqual(hashlib.sha256((self.dest/relative).read_bytes()).hexdigest(),digest)

    def test_live_comparison_cannot_be_exported(self):
        with (self.source/'.comparison.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):archive(self.source,self.dest,True)
        self.assertFalse(self.dest.exists())

    def test_quiet_continuity_evidence_is_exported_and_path_redacted(self):
        self.record['config']['host_continuity']='/private/campaign/host/quiet-state.json';self.save()
        evidence=dict(source=self.record['config']['host_continuity'],accepted=True,
                      checkpoint=dict(last_active=600,last_sample=998))
        (self.source/'host/quiet-continuity.json').write_text(json.dumps(evidence))
        archive(self.source,self.dest,True)
        copied=json.loads((self.dest/'host/quiet-continuity.json').read_text())
        self.assertEqual(copied['source'],'<campaign-quiet-checkpoint>')
        self.assertEqual(copied['checkpoint'],evidence['checkpoint'])
        self.assertIn('host/quiet-continuity.json',(self.dest/'SHA256SUMS').read_text())

    def test_recorded_evidence_corruption_rejected(self):
        self.record['attempts'][0]['results']['predecessor']=dict(directory=self.folder.name,
            evidence_sha256={'matrix.json':'0'*64})
        self.save()
        with self.assertRaisesRegex(ValueError,'changed'):archive(self.source,self.dest,True)

    def test_unrecorded_symlink_escape_rejected(self):
        (self.folder/'matrix.json').unlink()
        private=self.root/'private.json';private.write_text('{"secret":true}')
        (self.folder/'matrix.json').symlink_to(private)
        with self.assertRaisesRegex(ValueError,'escapes'):archive(self.source,self.dest,True)

    def test_complete_exports_still_require_report_validation(self):
        self.record['state']='complete';self.save()
        with patch('archive_comparison.comparison_report',side_effect=ValueError('invalid receipts')):
            with self.assertRaisesRegex(ValueError,'invalid receipts'):archive(self.source,self.dest)
        self.assertFalse(self.dest.exists())
