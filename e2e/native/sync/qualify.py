#!/usr/bin/env python3
"""Reject incomplete SY00 reports; performance observations are not production SLOs."""
import json
from pathlib import Path
import sys

REQUIRED = {
    'shipped_pg17_logical_capture_settings','qualified_two_table_key_schema','pgoutput_slot_created',
    'ordinary_role_cannot_decode','capture_precedes_frozen_boundary','isolated_bootstrap_excludes_inflight',
    'branch_capture_identity_is_distinct','bootstrap_plus_commit_tail_matches_source',
    'exact_numeric_key_change_delete_and_toast','commit_order_not_xid_order',
    'unacknowledged_replay_is_idempotent','checkpoint_rejects_other_lineage','frozen_reader_remains_pinned',
    'versioned_delta_roots_and_atomic_epoch_map','slot_survives_suspend_wake',
    'unacknowledged_commits_survive_compute_kill','post_restart_tail_matches_source','benchmark_oracle',
    'ddl_fence_add_column','blocks_publication_add_column','ddl_fence_new_empty_table',
    'blocks_publication_new_empty_table','ddl_fence_drop_empty_table','blocks_publication_drop_empty_table',
    'changed_relation_rejected','keyless_delete_rejected_by_source','owned_capture_slot_removed',
}


def qualify(report):
    if report.get('status')!='PASS' or report.get('slice')!='SY00' or report.get('format_version')!=1:
        raise ValueError('failed or incompatible report')
    checks=report.get('checks',[]);names=[c['name'] for c in checks]
    if len(set(names))!=len(names) or not REQUIRED <= set(names) or any(c.get('status')!='PASS' for c in checks):
        raise ValueError('missing, duplicate or failed checks')
    if not any(n.startswith('atomic_fixture_transaction_') for n in names):raise ValueError('no atomic transactions')
    inputs=report['inputs']
    for key in ('release_manifest_sha256','binary_sha256'):
        if len(inputs[key])!=64:raise ValueError('missing input identity')
    if inputs['engine_manifest']['neon_commit']!='1c6fa095261112aae239beef5a221b484703d49a':
        raise ValueError('unqualified engine')
    if not report['limitations']:raise ValueError('qualification limits omitted')
    metrics=report['metrics']
    if metrics['columnar']['status']!='PASS':raise ValueError('columnar probe failed')
    for key in ('write_without_consumption','write_with_consumption','commit_ack_to_reference_apply'):
        if metrics[key]['samples']<30:raise ValueError('insufficient measurements')
        values=[metrics[key][f'p{p}_ms'] for p in (50,95,99)]
        if any(not isinstance(v,(int,float)) or v<0 for v in values) or values!=sorted(values):
            raise ValueError('invalid latency measurements')
    # Engineering envelope only. Cold-start and release SLOs are separate.
    if metrics['commit_ack_to_reference_apply']['p95_ms']>1000:raise ValueError('capture probe budget exceeded')
    if any(t['apply_ms']>5000 for t in metrics['columnar']['tables']):raise ValueError('columnar probe budget exceeded')
    return len(checks)


if __name__=='__main__':
    print(f"SY00 evidence accepted: {qualify(json.loads(Path(sys.argv[1]).read_text()))} checks")
