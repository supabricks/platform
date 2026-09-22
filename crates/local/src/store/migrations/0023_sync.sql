-- Group identity is immutable; each full snapshot discovers the whole A01 table set.
CREATE TABLE sync_policies (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    branch_id TEXT NOT NULL REFERENCES branches(id),
    state TEXT NOT NULL CHECK(state IN ('active','paused','blocked','deleted')),
    record TEXT NOT NULL
);
CREATE UNIQUE INDEX sync_group ON sync_policies(branch_id) WHERE state!='deleted';
CREATE TABLE sync_runs (
    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    policy_id TEXT NOT NULL REFERENCES sync_policies(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    branch_id TEXT NOT NULL REFERENCES branches(id),
    state TEXT NOT NULL CHECK(state IN ('queued','starting','running','succeeded','failed','cancelled')),
    refresh_id TEXT UNIQUE REFERENCES exports(id),
    record TEXT NOT NULL
);
CREATE UNIQUE INDEX sync_writer ON sync_runs(branch_id) WHERE state IN ('queued','starting','running');
CREATE TABLE sync_requests (
    project_id TEXT NOT NULL REFERENCES projects(id),
    request_key TEXT NOT NULL,
    request TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY(project_id,request_key)
);
