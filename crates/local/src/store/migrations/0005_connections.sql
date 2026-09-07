CREATE TABLE connection_endpoints (
    branch_id TEXT PRIMARY KEY REFERENCES branches(id),
    port INTEGER NOT NULL UNIQUE CHECK(port BETWEEN 1 AND 65535)
);
CREATE TABLE connection_leases (
    id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES branches(id),
    generation INTEGER NOT NULL
);
CREATE INDEX connection_leases_branch ON connection_leases(branch_id);
ALTER TABLE branches ADD COLUMN suspend_lsn TEXT;
ALTER TABLE branches ADD COLUMN suspend_revision INTEGER;
-- Preserve the old StopCompute checkpoint index while adding the new durable
-- boundary and retirement steps to P04 suspends that were still in flight.
UPDATE operations SET steps='["stop_compute","capture_suspend","retire_compute"]'
WHERE status='pending' AND steps='["stop_compute"]';
