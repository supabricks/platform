-- AP01: a project's name is its own and unique in the installation, without
-- regard to case. The same source can be created again under another name.
-- Earlier catalogs allowed duplicates; the oldest keeps its name.
UPDATE projects SET name = name || '-' || substr(id,1,8)
 WHERE rowid NOT IN (SELECT min(rowid) FROM projects GROUP BY lower(name));
CREATE UNIQUE INDEX project_names ON projects(lower(name));
-- Per-instance facts the platform API reports. Earlier catalogs did not record
-- when a project was created; those rows carry the time of this upgrade.
CREATE TABLE project_details (
 deployment_id TEXT PRIMARY KEY REFERENCES deployments(id),
 description TEXT NOT NULL DEFAULT '' CHECK(length(description) <= 500),
 created_at_ms INTEGER NOT NULL,
 -- Set when the project is deleted. The rows stay as a tombstone, as deleted
 -- branches do; the project is hidden and its name is free at once.
 deleted_at_ms INTEGER
);
INSERT INTO project_details(deployment_id,description,created_at_ms)
 SELECT id,'',CAST(strftime('%s','now') AS INTEGER)*1000 FROM deployments;
-- The durable record of deleting a project: what was asked, and how far it is.
CREATE TABLE project_deletions (
 deployment_id TEXT PRIMARY KEY REFERENCES deployments(id),
 operation_id TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('running','succeeded')),
 former_name TEXT NOT NULL,
 -- Folders under the data directory to remove at the end, as a JSON array.
 owned_paths TEXT NOT NULL,
 -- Why the last attempt to advance could not, if it could not.
 waiting_on TEXT
);
-- One ordered feed of every operation in a project, whatever recorded it.
-- Triggers keep it current so no writer has to know about it. Operations from
-- earlier catalogs carry the time of this upgrade.
CREATE TABLE operation_index (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE,
 deployment_id TEXT NOT NULL REFERENCES deployments(id),
 source TEXT NOT NULL CHECK(source IN ('native','apply','delete')),
 created_at_ms INTEGER NOT NULL,
 finished_at_ms INTEGER,
 -- The Idempotency-Key of the API request that started it, if one did.
 api_key TEXT
);
CREATE INDEX operation_index_project ON operation_index(deployment_id, seq);
INSERT INTO operation_index(id,deployment_id,source,created_at_ms,finished_at_ms)
 SELECT o.id,d.id,'native',CAST(strftime('%s','now') AS INTEGER)*1000,
  CASE WHEN o.status='pending' AND NOT EXISTS (SELECT 1 FROM operation_errors e WHERE e.operation_id=o.id AND e.terminal=1)
   THEN NULL ELSE CAST(strftime('%s','now') AS INTEGER)*1000 END
 FROM operations o JOIN deployments d ON d.runtime_project_id=o.project_id ORDER BY o.rowid;
INSERT INTO operation_index(id,deployment_id,source,created_at_ms,finished_at_ms)
 SELECT a.id,a.deployment_id,'apply',CAST(strftime('%s','now') AS INTEGER)*1000,
  CASE WHEN a.state IN ('queued','preparing','activating') THEN NULL ELSE CAST(strftime('%s','now') AS INTEGER)*1000 END
 FROM project_applies a ORDER BY a.rowid;
CREATE TRIGGER operation_index_native AFTER INSERT ON operations BEGIN
 INSERT INTO operation_index(id,deployment_id,source,created_at_ms)
  SELECT NEW.id,d.id,'native',CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)
  FROM deployments d WHERE d.runtime_project_id=NEW.project_id;
END;
CREATE TRIGGER operation_index_native_done AFTER UPDATE OF status ON operations
 WHEN OLD.status='pending' AND NEW.status!='pending' BEGIN
 UPDATE operation_index SET finished_at_ms=CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)
  WHERE id=NEW.id AND finished_at_ms IS NULL;
END;
CREATE TRIGGER operation_index_native_failed AFTER INSERT ON operation_errors
 WHEN NEW.terminal=1 BEGIN
 UPDATE operation_index SET finished_at_ms=CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)
  WHERE id=NEW.operation_id AND finished_at_ms IS NULL;
END;
CREATE TRIGGER operation_index_native_failed_later AFTER UPDATE OF terminal ON operation_errors
 WHEN NEW.terminal=1 BEGIN
 UPDATE operation_index SET finished_at_ms=CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)
  WHERE id=NEW.operation_id AND finished_at_ms IS NULL;
END;
CREATE TRIGGER operation_index_apply AFTER INSERT ON project_applies BEGIN
 INSERT INTO operation_index(id,deployment_id,source,created_at_ms)
  VALUES (NEW.id,NEW.deployment_id,'apply',CAST((julianday('now')-2440587.5)*86400000 AS INTEGER));
END;
CREATE TRIGGER operation_index_apply_done AFTER UPDATE OF state ON project_applies
 WHEN NEW.state NOT IN ('queued','preparing','activating') BEGIN
 UPDATE operation_index SET finished_at_ms=CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)
  WHERE id=NEW.id AND finished_at_ms IS NULL;
END;
CREATE TRIGGER operation_index_delete AFTER INSERT ON project_deletions BEGIN
 INSERT INTO operation_index(id,deployment_id,source,created_at_ms)
  VALUES (NEW.operation_id,NEW.deployment_id,'delete',CAST((julianday('now')-2440587.5)*86400000 AS INTEGER));
END;
CREATE TRIGGER operation_index_delete_done AFTER UPDATE OF state ON project_deletions
 WHEN NEW.state='succeeded' BEGIN
 UPDATE operation_index SET finished_at_ms=CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)
  WHERE id=NEW.operation_id AND finished_at_ms IS NULL;
END;
-- Replies to API requests that carry an Idempotency-Key, so a retry returns the
-- original result. Rows older than a day may be removed.
CREATE TABLE api_idempotency (
 scope TEXT NOT NULL, request_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 reply_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
 PRIMARY KEY(scope, request_key)
);
