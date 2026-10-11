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
 created_at_ms INTEGER NOT NULL
);
INSERT INTO project_details
 SELECT id,'',CAST(strftime('%s','now') AS INTEGER)*1000 FROM deployments;
