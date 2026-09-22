-- Publication identities now include real exports and private incremental runs.
-- Executed only in the stopped schema transaction, with a final foreign_key_check.
CREATE TABLE analytical_artifacts (
 ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES projects(id),
 branch_id TEXT NOT NULL REFERENCES branches(id),
 kind TEXT NOT NULL CHECK(kind IN ('snapshot','incremental'))
);
INSERT INTO analytical_artifacts SELECT o.rowid,e.id,e.project_id,e.source_id,'snapshot' FROM exports e JOIN operations o ON o.id=e.id ORDER BY o.rowid;
CREATE TRIGGER export_artifact AFTER INSERT ON exports BEGIN
 INSERT INTO analytical_artifacts(id,project_id,branch_id,kind) VALUES (new.id,new.project_id,new.source_id,'snapshot');
END;
CREATE TABLE publications_new (
 ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
 export_id TEXT NOT NULL UNIQUE REFERENCES analytical_artifacts(id),
 epoch_id TEXT NOT NULL UNIQUE,
 branch_id TEXT NOT NULL REFERENCES branches(id),
 source_revision INTEGER NOT NULL,export_order INTEGER NOT NULL,requested_at_ms INTEGER NOT NULL,published_at_ms INTEGER,
 state TEXT NOT NULL CHECK(state IN ('requested','files_complete','published','failed','cancelled')),
 descriptor TEXT,error TEXT
);
INSERT INTO publications_new SELECT * FROM publications;
DROP TABLE publications;
ALTER TABLE publications_new RENAME TO publications;
CREATE UNIQUE INDEX publication_in_flight ON publications(branch_id) WHERE state IN ('requested','files_complete');
CREATE TABLE analytics_gc_new (
 export_id TEXT PRIMARY KEY REFERENCES analytical_artifacts(id),
 epoch_id TEXT REFERENCES snapshots(epoch_id),
 state TEXT NOT NULL CHECK(state IN ('pending','done'))
);
INSERT INTO analytics_gc_new SELECT * FROM analytics_gc;
DROP TABLE analytics_gc;
ALTER TABLE analytics_gc_new RENAME TO analytics_gc;
CREATE TABLE incremental_runs (
 id TEXT PRIMARY KEY REFERENCES analytical_artifacts(id),
 capture_id TEXT NOT NULL REFERENCES sync_captures(id),
 state TEXT NOT NULL CHECK(state IN ('requested','running','ready','succeeded','failed','cancelled')),
 record TEXT NOT NULL
);
CREATE UNIQUE INDEX incremental_writer ON incremental_runs((1)) WHERE state IN ('requested','running','ready');
CREATE TABLE incremental_heads (
 capture_id TEXT PRIMARY KEY REFERENCES sync_captures(id),
 epoch_id TEXT NOT NULL REFERENCES snapshots(epoch_id),
 published_lsn TEXT NOT NULL
);
CREATE TABLE incremental_requests (
 project_id TEXT NOT NULL REFERENCES projects(id),request_key TEXT NOT NULL,request TEXT NOT NULL,response TEXT NOT NULL,
 PRIMARY KEY(project_id,request_key)
);
