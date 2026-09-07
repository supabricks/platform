CREATE TABLE exports (
    id TEXT PRIMARY KEY REFERENCES operations(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    source_id TEXT NOT NULL REFERENCES branches(id),
    child_id TEXT NOT NULL UNIQUE REFERENCES branches(id),
    limits TEXT NOT NULL,
    deadline_ms INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'preparing'
      CHECK(state IN ('preparing','exporting','cleaning','complete','failed','cancelled')),
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    cleanup_id TEXT REFERENCES operations(id),
    lease_id TEXT NOT NULL
);
CREATE INDEX exports_active ON exports(state);
