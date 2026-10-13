-- Existing branches keep running until their owner explicitly opts into idle suspension.
CREATE TABLE branch_compute (
 branch_id TEXT PRIMARY KEY REFERENCES branches(id),
 size TEXT NOT NULL DEFAULT 'm' CHECK(size IN ('s','m','l')),
 idle_timeout_seconds INTEGER CHECK(idle_timeout_seconds BETWEEN 60 AND 86400),
 last_suspend_reason TEXT CHECK(last_suspend_reason IN ('manual','idle_timeout')),
 last_suspended_at_ms INTEGER
);
INSERT INTO branch_compute(branch_id) SELECT id FROM branches;
CREATE TRIGGER branch_compute_create AFTER INSERT ON branches BEGIN
 INSERT INTO branch_compute(branch_id) VALUES (NEW.id);
END;
CREATE TABLE branch_storage_samples (
 branch_id TEXT PRIMARY KEY REFERENCES branches(id),
 generation INTEGER NOT NULL,
 sampled_at_ms INTEGER NOT NULL,
 logical_bytes INTEGER,
 physical_bytes INTEGER,
 logical_size_accurate INTEGER NOT NULL,
 available INTEGER NOT NULL
);
-- Observe brief connections/jobs even if they both start and finish between
-- scheduler ticks. This is a counter, never a wall-clock deadline.
ALTER TABLE branch_compute ADD COLUMN activity_epoch INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER compute_connection_activity AFTER INSERT ON connection_leases BEGIN
 UPDATE branch_compute SET activity_epoch=activity_epoch+1 WHERE branch_id=NEW.branch_id;
END;
CREATE TRIGGER compute_connection_release AFTER DELETE ON connection_leases BEGIN
 UPDATE branch_compute SET activity_epoch=activity_epoch+1 WHERE branch_id=OLD.branch_id;
END;
CREATE TRIGGER compute_work_activity AFTER INSERT ON leases BEGIN
 UPDATE branch_compute SET activity_epoch=activity_epoch+1 WHERE branch_id=NEW.branch_id;
END;
CREATE TRIGGER compute_work_release AFTER DELETE ON leases BEGIN
 UPDATE branch_compute SET activity_epoch=activity_epoch+1 WHERE branch_id=OLD.branch_id;
END;
CREATE TRIGGER compute_settings_activity AFTER UPDATE OF size,idle_timeout_seconds ON branch_compute BEGIN
 UPDATE branch_compute SET activity_epoch=activity_epoch+1 WHERE branch_id=NEW.branch_id;
END;
