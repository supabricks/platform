-- Fence older runtimes before admitting continuous supervision and pause intent.
CREATE INDEX sync_policy_runs ON sync_runs(policy_id,state,ordinal);
