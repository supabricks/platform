CREATE TABLE analytics_installation (id TEXT PRIMARY KEY);
INSERT INTO analytics_installation VALUES (lower(hex(randomblob(16))));
CREATE TABLE publications (
    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
    export_id TEXT NOT NULL UNIQUE REFERENCES exports(id),
    epoch_id TEXT NOT NULL UNIQUE,
    branch_id TEXT NOT NULL REFERENCES branches(id),
    source_revision INTEGER NOT NULL,
    export_order INTEGER NOT NULL,
    requested_at_ms INTEGER NOT NULL,
    published_at_ms INTEGER,
    state TEXT NOT NULL CHECK(state IN ('requested','files_complete','published','failed','cancelled')),
    descriptor TEXT,
    error TEXT
);
CREATE UNIQUE INDEX publication_in_flight ON publications(branch_id)
  WHERE state IN ('requested','files_complete');
CREATE TABLE snapshots (
    epoch_id TEXT PRIMARY KEY REFERENCES epochs(id),
    export_id TEXT NOT NULL UNIQUE REFERENCES publications(export_id),
    state TEXT NOT NULL CHECK(state IN ('available','unavailable','deleting','deleted')),
    error TEXT
);
CREATE TABLE snapshot_heads (
    branch_id TEXT PRIMARY KEY REFERENCES branches(id),
    epoch_id TEXT NOT NULL UNIQUE REFERENCES snapshots(epoch_id)
);
CREATE TABLE snapshot_leases (
    id TEXT PRIMARY KEY,
    epoch_id TEXT NOT NULL REFERENCES snapshots(epoch_id),
    expires_at_ms INTEGER NOT NULL
);
CREATE INDEX snapshot_lease_expiry ON snapshot_leases(epoch_id,expires_at_ms);
CREATE TABLE analytics_gc (
    export_id TEXT PRIMARY KEY REFERENCES exports(id),
    epoch_id TEXT REFERENCES snapshots(epoch_id),
    state TEXT NOT NULL CHECK(state IN ('pending','done'))
);
