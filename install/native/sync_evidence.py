"""SY08 accepts complete, same-archive sync evidence; performance is an envelope."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from sqlite_qualification import validate_evidence as validate_sqlite
from capture_wal_qualification import validate as validate_wal

ROOT=Path(__file__).resolve().parents[2]
NETWORK={
    'linux-x86_64':'Linux loopback-only namespace; bundled sync workers; all descendants isolated',
    'macos-arm64':'macOS Seatbelt; external network and Homebrew denied for all sync descendants',
}
TOP_CHECKS={'exact_installed_capture_wal_faults','signed_curl_install_and_bundled_sync_workers_verified','archive_and_installed_inventory_unchanged',
    *('exact_installed_'+name for name in ('triggered','continuous','maintenance','governed','composite','date','char','bulk','merge','append','capacity'))}
WORKERS=('capture/owner.py','prepare_worker.py','incremental/lookahead.py','export.py','capture_worker.py','incremental_worker.py','session.py','capture/spool.py','capture/wal.py','capture/protocol.py','capture/groups.py','capture/source.py','incremental/rows.py','incremental/storage.py','incremental/maintenance.py','incremental/planning.py','incremental/preparation.py')
REQUIRED={
    'capacity':{'large_live_set_compaction_and_append_fit_original_capacity',
        'capacity_replay_preserves_exact_source_and_old_versions',
        'append_history_512_versions_keeps_pinned_rows_and_memory_budget'},
    'append':{'proven_new_keys_append_without_target_sized_memory',
        'append_commit_replay_and_old_versions_are_exact'},
    'merge':{'bounded_merge_into_large_compacted_table_completes',
        'bounded_merge_many_small_files_keeps_source_hash_build',
        'sparse_update_delete_key_move_and_saved_commit_replay_are_exact',
        'old_delta_versions_remain_exact_after_bounded_merges'},
    'bulk':{'complete_transaction_row_prefix_drains_with_exact_equality_and_pinned_reader',
        'oversized_single_transaction_preserves_last_complete_publication'},
    'date':{'date_native_bootstrap_types_nulls_bounds_and_sail_queries',
        'date_insert_update_delete_key_move_and_old_epoch_immutability',
        'date_capture_sigkill_and_daemon_restart_replay',
        'infinite_date_fails_closed_without_publishing_or_losing_previous_epoch'},
    'char':{'all_24_native_tpcds_schemas_bootstrap_with_date_char_and_composite_keys',
        'char_native_bootstrap_types_nulls_bounds_and_sail_queries',
        'char_insert_update_delete_key_move_and_old_epoch_immutability',
        'char_capture_sigkill_and_daemon_restart_replay'},
    'composite':{'native_seven_tpcds_composite_schemas_and_reordered_include_keys_bootstrap',
        'continuous_iud_all_key_components_toast_decimal_bounds_duplicates_and_pinned_sail',
        'capture_sigkill_and_daemon_restart_preserve_composite_identity'},
    'triggered':{'triggered_fixed_barrier_idempotency_long_transaction_and_pinned_reader',
        'triggered_production_input_budget_preserves_complete_transactions','triggered_restart_reuses_checkpoint_and_delete_retires_source'},
    'continuous':{'sustained_50_rows_per_second_and_200_row_burst_meet_5s_p95_with_atomic_groups',
        'continuous_idle_and_old_epoch_remain_stable_during_workload',
        'continuous_pause_sigkill_and_daemon_restart_keep_durable_checkpoint',
        'continuous_schema_drift_preserves_epoch_and_explicit_resync_recovers'},
    'maintenance':{'automatic_64_version_rollover_keeps_pinned_sail_epoch',
        'published_spool_prefix_reclaimed_and_capture_restarts_without_gap_or_duplicate',
        'unpinned_old_generation_collected_while_capture_and_current_generation_remain_live',
        'stopped_backup_restores_compacted_epoch_and_pruned_spool_with_capture_fenced',
        'source_retirement_removes_slot_and_spool_without_deleting_retained_compacted_epoch'},
    'governed':{'read_only_inspection_separate_source_manage_executor_and_result_grants_no_leakage',
        'native_background_snapshot_finishes_after_manager_session_revocation','new_epochs_do_not_retarget_existing_sail_readers',
        'service_revocation_denies_replayed_admission_and_retained_epoch_new_readers',
        'native_rls_source_never_publishes_through_service_authority','unsupported_governed_source_is_refused_before_capture_resources_or_bootstrap',
        'governed_continuous_service_identity_survives_manager_logout_and_keeps_old_readers_pinned',
        'incremental_result_review_uses_the_scoped_service_artifact',
        'real_uc_incremental_publications_are_immutable_version_zero_views_with_pinned_sail_readers',
        'cross_project_dataset_binding_and_restarted_notebook_keep_selected_incremental_epoch',
        'stopped_backup_restore_reconciles_incremental_catalog_view_locations',
        'service_revocation_fences_incremental_capture_and_retained_epoch_new_readers',
        'restart_with_revoked_service_epochs_keeps_access_closed','admission_and_run_transition_audit_contains_identifiers_without_rows_or_tokens'},
}


def require(value,message):
    if not value:raise ValueError('sync: '+message)


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def sha(value):return isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None
def number(value):return type(value) in (int,float) and math.isfinite(value) and value>=0


def collect(data,base,revision,target):
    require(data.get('status')=='passed' and not data.get('failure'),'incomplete qualification')
    require(data.get('release_identity')==base['release_sha256'] and data.get('archive')==base['archive'],'mixed archive')
    require(sha(data.get('binary_sha256')),'missing binary identity')
    sqlite=validate_sqlite(data.get('sqlite'),data['release_identity'])
    capture_wal=validate_wal(data.get('capture_wal'),data['release_identity'])
    source=data.get('source',{})
    require(source.get('platform_commit')==revision and source.get('platform_dirty') is False,'unreviewed source')
    require(source.get('data_formats',{}).get('local_catalog')==31,'unqualified storage format')
    require(target in NETWORK and data.get('network_evidence')==NETWORK[target],'missing offline evidence')
    require(TOP_CHECKS<=set(data.get('checks',[])),'incomplete installed checks')
    require(data['archive'].get('target')==target,'mixed target')
    expected={name:digest(ROOT/'python/analytics'/name) for name in WORKERS}
    require(data.get('worker_inventory')==expected,'unreviewed workers')
    suites=data.get('suites',{});require(set(suites)==set(REQUIRED),'missing suite')
    reports={}
    for name,required in REQUIRED.items():
        value=suites[name];checks=value.get('checks',[]);cleanup=value.get('cleanup',{})
        require(value.get('status')=='PASS' and value.get('exit_code')==0 and value.get('exact_installed') is True,'failed or source-only '+name)
        require(value.get('release_identity')==data['release_identity'] and value.get('binary_sha256')==data['binary_sha256'],'mixed suite '+name)
        require(isinstance(checks,list) and all(isinstance(c,str) for c in checks)
            and len(checks)==len(set(checks)) and required<=set(checks),'incomplete '+name)
        require(cleanup.get('exit_code')==0 and cleanup.get('timed_out') is False
            and cleanup.get('leaked_descendants')==0 and cleanup.get('remaining_descendants')==0
            and cleanup.get('descendants_observed',0)>0,'unclean '+name)
        require(sha(value.get('report_sha256')),'missing report '+name)
        reports[name]=dict(sha256=value['report_sha256'],checks=sorted(required))
    append=suites['append'].get('metrics',{})
    require(append.get('rows')==16777216 and append.get('insert_rows')==16384,'changed append workload')
    append_phases={}
    for phase in ('append','replay'):
        value=append.get(phase,{})
        require(number(value.get('highwater_bytes')) and 0<value['highwater_bytes']<768*1024**2,
                'append worker memory limit or missing measurement')
        keys=('elapsed_seconds','planning_seconds','apply_seconds','highwater_bytes')
        require(all(number(value.get(k)) for k in keys),'missing append phase measurement')
        append_phases[phase]={k:value[k] for k in keys}
    require(append['append'].get('metrics',{}).get('operation')=='WRITE'
        and append['append']['metrics'].get('committed_before_receipt') is True,'append commit fault not exercised')
    require(append['replay'].get('metrics',{}).get('replayed') is True
        and append['replay']['metrics'].get('apply_kind')=='append','append commit not replayed')
    capacity=suites['capacity'].get('metrics',{})
    require(capacity.get('rows')==65536 and capacity.get('insert_rows')==16384
        and capacity.get('payload_width')==8192,'changed capacity workload')
    require(number(capacity.get('source_bytes')) and 512*1024**2<capacity['source_bytes']<1024**3,
        'capacity source does not cross old boundary')
    capacity_phases={}
    for phase in ('apply','replay'):
        value=capacity.get(phase,{})
        keys=('elapsed_seconds','initialization_seconds','source_estimate_bytes','generation_bytes','highwater_bytes')
        require(all(number(value.get(k)) for k in keys),'missing capacity phase measurement')
        require(value['source_estimate_bytes']>1024**3 and 512*1024**2<value['generation_bytes']<1024**3,
            'capacity estimate or actual output not qualified')
        require(0<value['highwater_bytes']<768*1024**2,'capacity worker memory limit')
        capacity_phases[phase]={k:value[k] for k in keys}
    history=capacity.get('history',{})
    require(history.get('last_version')==512 and history.get('appended_rows')==511,
        'incomplete append history')
    require(number(history.get('highwater_bytes')) and 0<history['highwater_bytes']<768*1024**2,
        'append history worker memory limit')
    require(number(history.get('elapsed_seconds')) and history['elapsed_seconds']>0,
        'missing append history duration')
    commits=history.get('commit_seconds',[])
    require(isinstance(commits,list) and len(commits)==511 and all(number(v) and v>=0 for v in commits),
        'incomplete append history commit timings')
    capacity_phases['history']=history
    continuous=suites['continuous'];metrics=continuous.get('metrics',{});work=metrics.get('workload',{})
    for key,expected_value in [('tables',2),('rows_per_table',10000),('transactions',750),('changed_rows',1500),('target_rows_per_second',50),('burst_changed_rows',200)]:
        require(work.get(key)==expected_value,'changed workload '+key)
    for key in ('elapsed_seconds','achieved_rows_per_second','burst_commit_to_publication_ms'):
        require(number(work.get(key)),'missing workload measurement '+key)
    require(0<work['elapsed_seconds']<=33 and work['achieved_rows_per_second']>=45,'sustained workload incomplete')
    for key in ('commit_to_publication_ms','oltp_transaction_ms'):
        values=work.get(key,{})
        require(all(number(values.get(p)) for p in ('p50','p95','p99')),'missing percentiles')
        require(values['p50']<=values['p95']<=values['p99'],'invalid percentiles')
    require(abs(work['achieved_rows_per_second']-1500/work['elapsed_seconds'])<=0.05,'inconsistent throughput')
    require(work['commit_to_publication_ms']['p95']<=5000 and work['burst_commit_to_publication_ms']<=5000,'lag threshold exceeded')
    baseline=metrics.get('baseline_oltp_transaction_ms',{})
    require(all(number(baseline.get(p)) and baseline[p]>0 for p in ('p50','p95','p99')),'missing OLTP baseline')
    require(baseline['p50']<=baseline['p95']<=baseline['p99'],'invalid baseline percentiles')
    resources=metrics.get('resources',{});storage=metrics.get('storage',{})
    resource_keys=('peak_owned_rss_bytes','peak_allocated_data_bytes','spool_bytes','retained_wal_bytes','sampled_owned_cpu_seconds_lower_bound')
    storage_keys=('input_bytes','new_parquet_bytes','write_amplification_ratio','peak_inventory_files','peak_generation_bytes','compaction_bytes')
    require(all(number(resources.get(k)) for k in resource_keys),'missing resources')
    require(all(number(storage.get(k)) for k in storage_keys) and storage['input_bytes']>0,'missing amplification')
    require(abs(storage['write_amplification_ratio']-storage['new_parquet_bytes']/storage['input_bytes'])<=0.001,'inconsistent amplification')
    host=continuous.get('host',{})
    require(host.get('system')==('Linux' if target=='linux-x86_64' else 'Darwin')
        and host.get('machine')==('x86_64' if target=='linux-x86_64' else 'arm64')
        and type(host.get('cpu_count')) is int and host['cpu_count']>0
        and type(host.get('memory_bytes')) is int and host['memory_bytes']>0,'missing target hardware')
    # Copy only fixed measurements, never arbitrary suite fields or fixture data.
    clean_work={k:work[k] for k in ('tables','rows_per_table','transactions','changed_rows','target_rows_per_second','achieved_rows_per_second','elapsed_seconds','burst_changed_rows','burst_commit_to_publication_ms')}
    clean_work.update({k:{p:work[k][p] for p in ('p50','p95','p99')} for k in ('commit_to_publication_ms','oltp_transaction_ms')})
    return dict(status='passed',release_identity=data['release_identity'],archive=data['archive'],reports=reports,sqlite=sqlite,capture_wal=capture_wal,
        worker_inventory=expected,network=data['network_evidence'],append=append_phases,capacity=capacity_phases,
        host={k:host[k] for k in ('system','machine','cpu_count','memory_bytes')},workload=clean_work,
        baseline_oltp_transaction_ms={p:baseline[p] for p in ('p50','p95','p99')},
        oltp_p95_ratio=round(work['oltp_transaction_ms']['p95']/baseline['p95'],3),
        resources={k:resources[k] for k in resource_keys},storage={k:storage[k] for k in storage_keys})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('directory','inherited','report'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--revision',required=True);args=parser.parse_args()
    inherited=json.loads(args.inherited.read_text())
    require(inherited.get('status')=='passed' and inherited.get('revision')==args.revision,'incomplete inherited release gates')
    targets={}
    for target in ('linux-x86_64','macos-arm64'):
        path=args.directory/f'release-sync-{target}/sync.json'
        targets[target]=collect(json.loads(path.read_text()),inherited['targets'][target],args.revision,target)
    args.report.write_text(json.dumps(dict(status='passed',revision=args.revision,inherited_sha256=digest(args.inherited),targets=targets),indent=2)+'\n')
