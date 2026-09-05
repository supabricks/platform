CREATE TABLE processes (
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id), role TEXT NOT NULL,
    generation INTEGER NOT NULL, revision INTEGER NOT NULL, pid INTEGER NOT NULL CHECK(pid > 0),
    process_group INTEGER NOT NULL CHECK(process_group > 0), start_identity TEXT NOT NULL,
    PRIMARY KEY(endpoint_id, role)
);
CREATE TABLE epochs (
    id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES branches(id), source_lsn TEXT NOT NULL,
    UNIQUE(id, branch_id)
);
CREATE TABLE table_mappings (
    epoch_id TEXT NOT NULL REFERENCES epochs(id), source_oid INTEGER NOT NULL,
    table_name TEXT NOT NULL, object_path TEXT NOT NULL, PRIMARY KEY(epoch_id, source_oid)
);
CREATE TABLE leases (
    id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES branches(id), epoch_id TEXT,
    holder TEXT NOT NULL, generation INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL,
    FOREIGN KEY(epoch_id, branch_id) REFERENCES epochs(id, branch_id)
);
CREATE INDEX leases_branch_expiry ON leases(branch_id, expires_at_ms);
