-- AP08: a backup is a point of a branch that storage keeps. It is held by a
-- branch record that never has a compute, ports or connections, so nothing can
-- write to it, and it keeps the history it stands on from being collected.
CREATE TABLE backups (
 branch_id TEXT PRIMARY KEY REFERENCES branches(id),
 source_id TEXT NOT NULL REFERENCES branches(id),
 label TEXT NOT NULL,
 origin TEXT NOT NULL CHECK(origin IN ('manual','scheduled')),
 created_at_ms INTEGER NOT NULL
);
CREATE INDEX backups_source ON backups(source_id);
-- One schedule per database, named by its first branch.
CREATE TABLE backup_policies (
 database_id TEXT PRIMARY KEY REFERENCES branches(id),
 every_hours INTEGER NOT NULL CHECK(every_hours BETWEEN 1 AND 168),
 keep INTEGER NOT NULL CHECK(keep BETWEEN 1 AND 30),
 revision INTEGER NOT NULL DEFAULT 1,
 updated_at_ms INTEGER NOT NULL,
 -- When the schedule last tried, so a refusal is not repeated every tick.
 attempted_at_ms INTEGER
);
