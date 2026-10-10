#!/usr/bin/env python3
"""Reconcile all four installed cache trials before reporting a measured effect."""
import argparse
import json
from pathlib import Path
from analyze_source_profile import analyze, compare, rows


def run(base, labels):
    reference = None; identity = None; results = {}; comparisons = []
    for label, arm, profile, blocks in zip(labels,
            ('source_only', 'source_only', 'concurrent', 'concurrent'),
            ('compact', 'source-load', 'compact', 'source-load'),
            ('16384', '131072', '16384', '131072')):
        root = base / label
        report = json.loads((root / 'result.json').read_text())
        verification = base / ('v' + label) / 'result.json'
        qualified = json.loads(verification.read_text())
        assert qualified['status'] == ('SOURCE_EXACT_PASS' if arm == 'source_only' else 'PREFIX_EXACT_PASS')
        assert len(qualified['tables']) == 24 and all(t['status'] == 'PASS' for t in qualified['tables'])
        control = base / (label + '-control')
        launch = json.loads((control / 'launch.json').read_text())
        assert launch['status'] == 'FINISHED' and launch['exit_code'] == 0
        assert launch['compute_cache_profile'] == profile and launch['arm'] == arm
        container = json.loads((control / 'container-final.json').read_text())
        assert container['limits'] == dict(CpusetCpus='0-7', Memory=16*1024**3, MemorySwap=16*1024**3, NetworkMode='none')
        assert not container['state']['OOMKilled'] and container['state']['ExitCode'] == 0
        data = analyze(root, verification if arm == 'source_only' else None)
        resource_samples = list(rows(control / 'resources.jsonl'))
        events = [dict(line.split() for line in sample['memory.events'].splitlines())
                  for sample in resource_samples if 'memory.events' in sample]
        assert events and all(int(event['oom']) == int(event['oom_kill']) == 0 for event in events)
        data['sampled_cell_memory_peak_bytes'] = max(int(sample['memory.peak']) for sample in resource_samples if 'memory.peak' in sample)
        data['whole_trial_observed_compilers'] = sorted({(p['pid'], p['name']) for sample in resource_samples for p in sample['observed_compilers']})
        settings = dict(data['observer']['settings'])
        assert settings.pop('shared_buffers') == blocks
        assert settings['fsync'] == settings['full_page_writes'] == settings['synchronous_commit'] == 'on'
        assert settings['wal_level'] == 'logical'
        if reference is None:
            reference = settings; identity = data['release_identity']
        assert settings == reference, 'another PostgreSQL setting changed'
        assert data['release_identity'] == launch['release_identity'] == identity
        assert not data['late_resources']['memory_event_delta']['oom']
        assert not data['late_resources']['memory_event_delta']['oom_kill']
        comparisons.append(compare(base / labels[0], root))
        data['end_to_end_rows_s'] = report['committed_rows'] / (report['load_seconds'] + report.get('drain_seconds', 0))
        data['end_to_end_scope'] = 'source load plus drain' if arm == 'concurrent' else 'source load only'
        results[label] = data
    effects = {}
    for arm, before, after in [('source_only', labels[0], labels[1]), ('concurrent', labels[2], labels[3])]:
        a, b = results[before], results[after]
        effects[arm] = dict(control=before, candidate=after,
            overall_throughput_ratio=b['end_to_end_rows_s']/a['end_to_end_rows_s'],
            late_source_throughput_ratio=b['late']['rows_s']/a['late']['rows_s'],
            copy_completion_mean_ratio=b['late']['phases_ms']['copy_finish']['mean']/a['late']['phases_ms']['copy_finish']['mean'],
            commit_mean_ratio=b['late']['phases_ms']['commit']['mean']/a['late']['phases_ms']['commit']['mean'])
    return dict(status='MATCHED_MEASUREMENT', labels=labels, results=results, effects=effects, exact_copy_comparisons=comparisons,
                scope='One four-arm prefix comparison, no confidence interval or full SF100 qualification. Source acknowledgment cohorts and narrower counter windows retain separate endpoints.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('base', type=Path)
    p.add_argument('--labels', nargs=4, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.write_text(json.dumps(run(args.base, args.labels), indent=2, sort_keys=True) + '\n')
