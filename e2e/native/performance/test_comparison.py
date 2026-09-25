"""Mutation checks for attribution, immutable pairing, and failure denominators."""
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import time

from compare import pair_order, validate_resume
from comparison import compare_records, load_trial, profile_metrics, validate_trial
from host_monitor import active_builds, HostMonitor
from matrix import cells, trial_order

ARCHIVE = Path(__file__).resolve().parents[3]/'docs/architecture/sync-performance-evidence/2026-09-24-workflow-profile/matrices/08-matrix-cpu4-rate50-r1-p1-attempt1'


class Comparisons(unittest.TestCase):
    def setUp(self):
        data=json.loads(gzip.decompress((ARCHIVE/'raw-reports.json.gz').read_bytes()))
        self.manifest=data['matrix'];self.entry=self.manifest['trials'][0]
        row=data['trials'][self.entry['name']]
        self.trial=row['trial'];self.cleanup=row['cleanup']
        self.expected=dict(release_identity=self.manifest['release_identity'],binary_sha256=self.manifest['binary_sha256'],
                           runtime_revision=self.manifest['runtime_revision'],image_id=self.manifest['image_id'],
                           cpus=4,rate=50,affinity=self.manifest['affinity']['4'],memory_gib=16,
                           parameters=dict(rate=50,seconds=45,clients=4,rows=10000,baseline=5,warmup=5,profile=True))

    def validate(self):
        return validate_trial(self.manifest,self.entry,self.trial,self.cleanup,self.expected)

    def test_original_receipt_is_accepted(self):
        self.assertEqual(self.validate()['lag_p95_ms'],4360.628)
        self.assertEqual(load_trial(ARCHIVE,self.expected)['status'],'measured')

    def test_changed_workload_package_image_affinity_or_rate_is_rejected(self):
        for field in ('release_identity','binary_sha256','image_id','runtime_revision','affinity','rate','memory_gib'):
            with self.subTest(field=field):
                original=copy.deepcopy(self.expected)
                self.expected[field]='changed'
                with self.assertRaises(ValueError):self.validate()
                self.expected=original
        self.trial['parameters']['clients']=8
        with self.assertRaisesRegex(ValueError,'workload'):self.validate()

    def test_missing_markers_cannot_produce_success(self):
        self.trial['observed_transactions']=0
        with self.assertRaisesRegex(ValueError,'markers'):self.validate()

    def test_inconsistent_cleanup_or_missing_correctness_is_rejected(self):
        self.cleanup['leaked_descendants']=1
        with self.assertRaises(ValueError):self.validate()
        self.cleanup['leaked_descendants']=0
        self.trial['checks'].remove('both_published_tables_equal_frozen_postgres_source')
        with self.assertRaisesRegex(ValueError,'correctness'):self.validate()

    def test_runtime_failure_cannot_retain_success_percentiles(self):
        self.entry.update(status='runtime_failed',exit_code=1)
        self.cleanup['exit_code']=2;self.entry['cleanup']['exit_code']=2
        self.trial.update(status='runtime_failed',runtime_error='publication_drain_timeout')
        with self.assertRaisesRegex(ValueError,'incomplete'):self.validate()
        del self.trial['stages_ms'];self.trial.pop('within_5s_p95')
        self.assertIsNone(self.validate()['lag_p95_ms'])

    def test_false_input_or_freshness_flags_are_rejected(self):
        self.trial['offered_load_met']=False
        with self.assertRaisesRegex(ValueError,'input-rate'):self.validate()
        self.trial['offered_load_met']=True;self.trial['within_5s_p95']=False
        with self.assertRaisesRegex(ValueError,'freshness'):self.validate()

    def test_source_accounting_is_checked(self):
        self.trial['source']['achieved_rows_per_second']=1000
        with self.assertRaises(ValueError):self.validate()

    def test_corrupt_profile_and_missing_native_hook_are_rejected(self):
        profile=json.loads(gzip.decompress((ARCHIVE/'profile.json.gz').read_bytes()))
        baseline=profile_metrics(profile,self.trial)
        self.assertGreater(baseline['capture_commit_ms'],0)
        profile['workers']['daemon.jsonl'][-1]['final']=False
        with self.assertRaisesRegex(ValueError,'final'):profile_metrics(profile,self.trial)
        profile['workers']['daemon.jsonl'][-1]['final']=True
        name=next(k for k in profile['workers'] if k.startswith('capture-'))
        profile['workers'][name][0]['native_io']=None
        with self.assertRaisesRegex(ValueError,'native'):profile_metrics(profile,self.trial)

    def test_failure_to_success_does_not_create_aggregate_latency_speedup(self):
        ok=dict(status='measured',lag_p95_ms=4000)
        failed=dict(status='runtime_failed',lag_p95_ms=None)
        pairs=[dict(cpus=8,rate=1000,repeat=1,predecessor=failed,candidate=ok),
               dict(cpus=8,rate=1000,repeat=2,predecessor=ok,candidate=dict(ok,lag_p95_ms=3000))]
        group=compare_records(pairs)['groups'][0]
        self.assertEqual(group['predecessor']['failures'],1)
        self.assertIsNone(group['marginal']['lag_p95_ms']['percent'])
        self.assertEqual(group['paired']['lag_p95_ms'][1]['percent'],-25)

    def test_explicit_cells_and_balanced_repeatable_pairs(self):
        selected=cells('4:50,16:50,8:1000,16:1000')
        order=trial_order(selected,3,42)
        self.assertEqual(len(order),12)
        self.assertEqual({(c,r) for _,r,c in order},set(selected))
        a=pair_order(selected,3,42)
        self.assertEqual(a,pair_order(selected,3,42))
        self.assertEqual(sum(p['arms'][0]=='candidate' for p in a),6)
        for cpu,rate in selected:
            self.assertEqual({p['arms'][0] for p in a if (p['cpus'],p['rate'])==(cpu,rate)}, {'candidate','predecessor'})
        for bad in ('4:50,4:50','0:50','4','4:50:2',''):
            with self.assertRaises(Exception):cells(bad)

    def test_resume_rejects_changed_identity_duplicate_pair_and_uncertain_cleanup(self):
        config=dict(order=pair_order([(4,50)],1,1),fingerprint='a')
        prior=dict(config=config,state='between_pairs',pairs=[])
        validate_resume(prior,config)
        with self.assertRaisesRegex(ValueError,'settings'):validate_resume(prior,dict(config,fingerprint='b'))
        prior['state']='running_trial'
        with self.assertRaisesRegex(ValueError,'cleanup'):validate_resume(prior,config)
        prior['state']='between_pairs'
        row=dict(index=0,pair=config['order'][0],results=dict(candidate={},predecessor={}))
        prior['pairs']=[row,row]
        with self.assertRaisesRegex(ValueError,'duplicate'):validate_resume(prior,config)
        prior['pairs']=[copy.deepcopy(row)]
        prior['pairs'][0]['pair']=dict(config['order'][0],repeat=2)
        with self.assertRaisesRegex(ValueError,'pair identity'):validate_resume(prior,config)

    def test_archive_round_trip_redacts_paths_and_checks_artifacts(self):
        from archive_comparison import archive
        from compare import comparison_report, expected, sha
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'source';source.mkdir();(source/'host').mkdir()
            (source/'host'/'0000.jsonl').write_text('{"active_builds":[]}\n')
            pair=pair_order([(4,50)],1,1)[0]
            identity=dict(revision=self.manifest['harness_revision'],files={
                'e2e/native/performance/'+k:v for k,v in self.manifest['harness_sha256'].items()})
            config=dict(order=[pair],slice='test',hypothesis='archive preserves accounting',
                        image_id=self.manifest['image_id'],affinity={'4':self.expected['affinity']},
                        memory_gib=16,parameters={k:v for k,v in self.expected['parameters'].items() if k!='rate'},arms={})
            results={}
            for arm in ('predecessor','candidate'):
                config['arms'][arm]=dict(harness='/private/user/'+arm,release='/private/runtime',
                    harness_identity=identity,package={k:self.expected[k] for k in ('release_identity','binary_sha256','runtime_revision')})
                directory=source/arm;directory.mkdir()
                trial_root=directory/self.entry['name'];trial_root.mkdir()
                manifest=dict(self.manifest,local_harness_path='/private/user/'+arm)
                for path,value in ((directory/'matrix.json',manifest),(trial_root/'trial.json',self.trial),(trial_root/'cleanup.json',self.cleanup)):
                    path.write_text(json.dumps(value))
                (trial_root/'profile.json.gz').write_bytes((ARCHIVE/'profile.json.gz').read_bytes())
                results[arm]=dict(directory=arm,metrics=load_trial(directory,expected(config,arm,pair)),
                    evidence_sha256={str(p.relative_to(directory)):sha(p) for p in directory.rglob('*') if p.is_file()})
            item=dict(index=0,pair=pair,results=results,accepted=True)
            record=dict(config=config,state='complete',historical=[],pairs=[item],attempts=[item])
            (source/'experiment.json').write_text(json.dumps(record))
            original=comparison_report(source,record)
            destination=Path(temporary)/'archive'
            archive(source,destination)
            exported=json.loads((destination/'experiment.json').read_text())
            self.assertEqual(comparison_report(destination,exported),original)
            self.assertNotIn('/private/',(destination/'experiment.json').read_text())
            self.assertNotIn('/private/',(destination/'candidate'/'matrix.json').read_text())
            self.assertTrue((destination/'host'/'0000.jsonl.gz').exists())
            self.assertTrue((destination/'analysis-source.json.gz').exists())
            path=destination/'candidate'/'matrix.json'
            path.write_text(path.read_text()+' ')
            with self.assertRaisesRegex(ValueError,'checksum'):
                comparison_report(destination,exported)

    def test_completed_resume_cannot_omit_pairs(self):
        config=dict(order=pair_order([(4,50)],1,1))
        with self.assertRaisesRegex(ValueError,'missing pairs'):
            validate_resume(dict(config=config,state='complete',pairs=[]),config)

    def test_host_evidence_rotates_and_exhaustion_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=HostMonitor(directory,interval=.001,segment_bytes=180,total_bytes=500)
            def sample(*args):
                return dict(at_ms=time.time()*1000,monotonic=time.monotonic(),builds={},active_builds=[])
            with patch('host_monitor.observe',sample):
                monitor.start()
                monitor.thread.join(timeout=2)
                self.assertFalse(monitor.thread.is_alive())
            self.assertGreater(len(list(Path(directory,'host').glob('*.jsonl'))),1)
            self.assertLessEqual(sum(p.stat().st_size for p in Path(directory,'host').glob('*')),500)
            with self.assertRaisesRegex(RuntimeError,'budget'):monitor.check()
            with self.assertRaisesRegex(RuntimeError,'budget'):monitor.close()

    def test_host_sampling_gap_and_read_errors_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=HostMonitor(directory)
            monitor.last_sample=time.monotonic()-21
            with self.assertRaisesRegex(RuntimeError,'gap'):monitor.check()
            with patch('host_monitor.observe',side_effect=PermissionError('denied')):
                monitor.start();monitor.thread.join(timeout=2)
            with self.assertRaisesRegex(RuntimeError,'PermissionError'):monitor.close()

    def test_parked_build_process_and_pid_reuse(self):
        previous={'1:100':dict(user_ticks=10,system_ticks=1)}
        self.assertEqual(active_builds(previous,copy.deepcopy(previous)),[])
        self.assertEqual(active_builds(previous,{'1:200':dict(user_ticks=10,system_ticks=1)}),['1:200'])
        self.assertEqual(active_builds(previous,{'1:100':dict(user_ticks=11,system_ticks=1)}),['1:100'])


if __name__=='__main__':unittest.main()
