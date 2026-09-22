-- Managed triggered runs own their incremental batches through immutable JSON IDs.
CREATE INDEX incremental_sync_owner ON incremental_runs(json_extract(record,'$.sync_run_id'));
