-- A session is a durable reader reference until its entire owned process group
-- has stopped. Wall-clock lease expiry alone cannot authorize deleting its files.
CREATE TABLE analytical_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    branch_id TEXT NOT NULL REFERENCES branches(id),
    request_key TEXT NOT NULL,
    request TEXT NOT NULL,
    epoch_id TEXT REFERENCES snapshots(epoch_id),
    refresh_id TEXT REFERENCES exports(id),
    state TEXT NOT NULL CHECK(state IN ('waiting','starting','ready','closing','closed','failed')),
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    record TEXT NOT NULL,
    UNIQUE(project_id, request_key)
);
CREATE INDEX analytical_session_references ON analytical_sessions(epoch_id,state);
CREATE TABLE analytical_refreshes (
    export_id TEXT PRIMARY KEY REFERENCES exports(id),
    error TEXT
);
