CREATE TABLE sync_captures (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES sync_policies(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    branch_id TEXT NOT NULL REFERENCES branches(id),
    state TEXT NOT NULL CHECK(state IN ('requested','recovering','bootstrapping','capturing','pausing','paused','unavailable','resync_required','deleting','deleted')),
    bootstrap_id TEXT REFERENCES exports(id),
    record TEXT NOT NULL
);
-- One admitted source generation bounds installation spool/WAL resource commitments.
CREATE UNIQUE INDEX capture_installation_admission ON sync_captures((1)) WHERE state!='deleted';
CREATE TABLE capture_requests (
    project_id TEXT NOT NULL REFERENCES projects(id),
    request_key TEXT NOT NULL,
    request TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY(project_id,request_key)
);
