-- AP02: facts about a branch that the platform API reports and the lifecycle
-- does not need. A database is its root branch, so these serve both.
-- Earlier catalogs did not record when a branch was created; those rows carry
-- the time of this upgrade.
CREATE TABLE branch_details (
 branch_id TEXT PRIMARY KEY REFERENCES branches(id),
 created_at_ms INTEGER NOT NULL,
 -- Changes that do not move the lifecycle revision: a rename or a new expiry. The
 -- API's revision is the lifecycle revision plus this, so it rises with both.
 edits INTEGER NOT NULL DEFAULT 0
);
INSERT INTO branch_details(branch_id,created_at_ms)
 SELECT id,CAST(strftime('%s','now') AS INTEGER)*1000 FROM branches;
-- Triggers keep it current so no writer has to know about it.
CREATE TRIGGER branch_details_created AFTER INSERT ON branches BEGIN
 INSERT INTO branch_details(branch_id,created_at_ms)
  VALUES (NEW.id,CAST((julianday('now')-2440587.5)*86400000 AS INTEGER));
END;
CREATE TRIGGER branch_details_edited AFTER UPDATE OF name,expires_at_ms ON branches
 WHEN OLD.name != NEW.name OR OLD.expires_at_ms IS NOT NEW.expires_at_ms BEGIN
 UPDATE branch_details SET edits=edits+1 WHERE branch_id=NEW.id;
END;
