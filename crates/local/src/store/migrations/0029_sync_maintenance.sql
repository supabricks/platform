-- Gate older controllers before compacted roots or pruned spools are written.
CREATE TABLE sync_storage_roots (
 id TEXT PRIMARY KEY,
 capture_id TEXT NOT NULL REFERENCES sync_captures(id)
);
INSERT INTO sync_storage_roots SELECT id,id FROM sync_captures;
