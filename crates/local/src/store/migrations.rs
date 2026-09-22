use super::error::{Result, conflict};
use rusqlite::{Connection, TransactionBehavior};
const MIGRATIONS: &[&str] = &[
    include_str!("migrations/0001_state.sql"),
    include_str!("migrations/0002_work.sql"),
    include_str!("migrations/0003_native_processes.sql"),
    include_str!("migrations/0004_branches.sql"),
    include_str!("migrations/0005_connections.sql"),
    include_str!("migrations/0006_exports.sql"),
    include_str!("migrations/0007_analytics.sql"),
    include_str!("migrations/0008_sessions.sql"),
    include_str!("migrations/0009_ingest.sql"),
    include_str!("migrations/0010_environments.sql"),
    include_str!("migrations/0011_deployments.sql"),
    include_str!("migrations/0012_project_apply.sql"),
    include_str!("migrations/0013_project_initialization.sql"),
    include_str!("migrations/0014_catalog_assets.sql"),
    include_str!("migrations/0015_catalog_publications.sql"),
    include_str!("migrations/0016_identity.sql"),
    include_str!("migrations/0017_authorization.sql"),
    include_str!("migrations/0018_catalog_governance.sql"),
    include_str!("migrations/0019_isolated_execution.sql"),
    include_str!("migrations/0020_governed_data.sql"),
    include_str!("migrations/0021_governed_recovery.sql"),
    include_str!("migrations/0022_governed_console.sql"),
    include_str!("migrations/0023_sync.sql"),
    include_str!("migrations/0024_capture.sql"),
    include_str!("migrations/0025_incremental.sql"),
    include_str!("migrations/0026_triggered.sql"),
];
pub const SCHEMA_VERSION: u32 = MIGRATIONS.len() as u32;

pub(super) fn migrate(db: &mut Connection) -> Result<()> {
    apply(db, MIGRATIONS)
}
fn foreign_key_migration(
    db: &mut Connection,
    f: impl FnOnce(&mut Connection) -> Result<()>,
) -> Result<()> {
    let enabled: bool = db.pragma_query_value(None, "foreign_keys", |r| r.get(0))?;
    db.pragma_update(None, "foreign_keys", false)?;
    let result = f(db);
    db.pragma_update(None, "foreign_keys", enabled)?;
    result
}
fn apply(db: &mut Connection, migrations: &[&str]) -> Result<()> {
    foreign_key_migration(db, |db| apply_inner(db, migrations))
}
fn apply_inner(db: &mut Connection, migrations: &[&str]) -> Result<()> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let current: u32 = tx.pragma_query_value(None, "user_version", |r| r.get(0))?;
    if current as usize > migrations.len() {
        return Err(conflict(
            "state schema is newer than this Supabricks binary",
        ));
    }
    for (version, sql) in migrations.iter().enumerate().skip(current as usize) {
        tx.execute_batch(sql)?;
        tx.pragma_update(None, "user_version", (version + 1) as u32)?;
    }
    if tx.prepare("PRAGMA foreign_key_check")?.exists([])? {
        return Err(conflict("state contains invalid resource references"));
    }
    tx.commit()?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn schema_twenty_five_preserves_populated_publications_capture_and_foreign_keys() {
        let mut db = Connection::open_in_memory().unwrap();
        db.pragma_update(None, "foreign_keys", true).unwrap();
        apply(&mut db, &MIGRATIONS[..24]).unwrap();
        db.execute_batch("INSERT INTO projects VALUES ('p','project');
            INSERT INTO branches(id,project_id,name,tenant_id,timeline_id,revision,desired) VALUES ('b','p','main','tenant','timeline',1,'running'),('child','p','frozen','tenant','childline',1,'running');
            INSERT INTO operations(rowid,id,project_id,request_key,request,branch_id,revision,steps) VALUES (37,'export','p','export','{}','child',1,'[]');
            INSERT INTO exports(id,project_id,source_id,child_id,limits,deadline_ms,state,lease_id) VALUES ('export','p','b','child','{}',0,'complete','lease');
            INSERT INTO publications(export_id,epoch_id,branch_id,source_revision,export_order,requested_at_ms,published_at_ms,state,descriptor) VALUES ('export','epoch','b',1,37,10,20,'published','{\"preserve\":true}');
            INSERT INTO epochs VALUES ('epoch','b','0/100');
            INSERT INTO snapshots VALUES ('epoch','export','available',NULL);
            INSERT INTO snapshot_heads VALUES ('b','epoch');
            INSERT INTO snapshot_leases VALUES ('reader','epoch',9000000);
            INSERT INTO analytics_gc VALUES ('export','epoch','done');
            INSERT INTO sync_policies VALUES ('policy','p','b','active','{\"preserve\":true}');
            INSERT INTO sync_captures VALUES ('capture','policy','p','b','capturing','export','{\"cursor\":\"0/100\"}');").unwrap();
        catalog_upgrade(&mut db, 24, "backup", "release").unwrap();
        let value:(String,String,i64)=db.query_row("SELECT p.descriptor,c.record,a.ordinal FROM publications p JOIN analytical_artifacts a ON a.id=p.export_id JOIN sync_captures c ON c.bootstrap_id=p.export_id",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
        assert_eq!(
            value,
            (
                r#"{"preserve":true}"#.into(),
                r#"{"cursor":"0/100"}"#.into(),
                37
            )
        );
        assert!(
            !db.prepare("PRAGMA foreign_key_check")
                .unwrap()
                .exists([])
                .unwrap()
        );
        assert!(
            db.pragma_query_value(None, "foreign_keys", |r| r.get::<_, bool>(0))
                .unwrap()
        );
        assert!(
            db.execute("DELETE FROM analytical_artifacts WHERE id='export'", [])
                .is_err()
        );
        assert_eq!(
            db.query_row(
                "SELECT epoch_id FROM snapshot_leases WHERE id='reader'",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
            "epoch"
        );
    }
    #[test]
    fn upgrades_pending_p04_suspension_without_reindexing_its_stop_checkpoint() {
        let mut db = Connection::open_in_memory().unwrap();
        db.pragma_update(None, "foreign_keys", true).unwrap();
        apply(&mut db, &MIGRATIONS[..4]).unwrap();
        db.execute_batch("INSERT INTO projects VALUES ('p','project');
            INSERT INTO branches(id,project_id,name,tenant_id,timeline_id,revision,desired) VALUES ('b','p','main','tenant','timeline',2,'suspended');
            INSERT INTO operations(id,project_id,request_key,request,branch_id,revision,steps) VALUES ('o','p','suspend','{}','b',2,'[\"stop_compute\"]');").unwrap();
        migrate(&mut db).unwrap();
        let (steps, next): (String, i64) = db
            .query_row(
                "SELECT steps,next_step FROM operations WHERE id='o'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(
            steps,
            "[\"stop_compute\",\"capture_suspend\",\"retire_compute\"]"
        );
        assert_eq!(next, 0);
    }
    #[test]
    fn upgrades_preserve_existing_rows_and_failed_upgrades_roll_back() {
        let mut db = Connection::open_in_memory().unwrap();
        db.pragma_update(None, "foreign_keys", true).unwrap();
        apply(&mut db, &MIGRATIONS[..1]).unwrap();
        db.execute("INSERT INTO projects VALUES ('existing', 'prod')", [])
            .unwrap();
        let mut broken = MIGRATIONS.to_vec();
        broken.push("CREATE TABLE should_rollback(id); THIS IS INVALID SQL;");
        assert!(apply(&mut db, &broken).is_err());
        assert_eq!(
            db.pragma_query_value(None, "user_version", |r| r.get::<_, u32>(0))
                .unwrap(),
            1
        );
        assert!(
            !db.prepare(
                "SELECT name FROM sqlite_master WHERE name IN ('epochs','should_rollback')"
            )
            .unwrap()
            .exists([])
            .unwrap()
        );
        migrate(&mut db).unwrap();
        assert_eq!(
            db.query_row("SELECT name FROM projects", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "prod"
        );
        migrate(&mut db).unwrap();
        db.pragma_update(None, "user_version", 99).unwrap();
        assert!(migrate(&mut db).is_err());
        assert_eq!(
            db.query_row("SELECT count(*) FROM projects", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
    }
}

/// Existing roots migrate only through the verified stopped-backup upgrade.
fn catalog_upgrade_inner(
    db: &mut Connection,
    from: u32,
    source: &str,
    release: &str,
) -> Result<()> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let version: u32 = tx.pragma_query_value(None, "user_version", |r| r.get(0))?;
    if version != from
        || !matches!(
            from,
            8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 | 25
        )
    {
        return Err(conflict(
            "catalog migration requires the backed-up source schema",
        ));
    }
    for index in from as usize..MIGRATIONS.len() {
        tx.execute_batch(MIGRATIONS[index])?;
        tx.execute(
            "INSERT INTO catalog_migrations VALUES (?1,?2,?3)",
            rusqlite::params![(index + 1) as u32, source, release],
        )?;
    }
    tx.pragma_update(None, "user_version", SCHEMA_VERSION)?;
    if tx.prepare("PRAGMA foreign_key_check")?.exists([])? {
        return Err(conflict("migration contains invalid resource references"));
    }
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
mod deployment_tests {
    use super::*;
    fn source() -> Connection {
        let mut db = Connection::open_in_memory().unwrap();
        db.pragma_update(None, "foreign_keys", true).unwrap();
        apply(&mut db, &MIGRATIONS[..10]).unwrap();
        db.execute_batch("INSERT INTO projects VALUES ('original','old-name');
            INSERT INTO branches(id,project_id,name,tenant_id,timeline_id,revision,desired) VALUES ('branch','original','main','tenant','timeline',7,'suspended');
            INSERT INTO worktrees VALUES ('/original/checkout','original','branch');
            INSERT INTO environment_generations VALUES ('generation','original','/original/checkout','ready','{\"preserved\":\"record bytes\"}');
            INSERT INTO environment_active VALUES ('original','/original/checkout','generation');
            INSERT INTO environment_operations VALUES ('operation','original','/other/checkout','key','ready','{\"preserved\":true}');").unwrap();
        db
    }
    #[test]
    fn catalog_ten_migration_preserves_runtime_state_and_maps_known_worktrees() {
        let mut db = source();
        catalog_upgrade(&mut db, 10, "backup-sha", "release").unwrap();
        let record: String = db
            .query_row("SELECT record_json FROM environment_generations", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(record, "{\"preserved\":\"record bytes\"}");
        let (runtime, definition): (String, String) = db
            .query_row(
                "SELECT runtime_project_id,definition_id FROM deployments",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(runtime, "original");
        assert_eq!(definition, "original");
        assert_eq!(
            db.query_row("SELECT count(*) FROM worktree_bindings", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            2
        );
        assert_eq!(
            db.query_row("SELECT branch_id FROM worktrees", [], |r| r
                .get::<_, String>(0))
                .unwrap(),
            "branch"
        );
        assert_eq!(
            db.query_row("SELECT revision FROM branches", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            7
        );
        assert_eq!(db.query_row("SELECT count(*) FROM catalog_migrations WHERE version=11 AND source_sha256='backup-sha'",[],|r|r.get::<_,i64>(0)).unwrap(),1);
        assert_eq!(db.query_row("SELECT count(*) FROM deployments WHERE actor_id=effective_principal_id AND actor_id IN (SELECT id FROM principals WHERE provider='local-owner')",[],|r|r.get::<_,i64>(0)).unwrap(),1);
    }
    #[test]
    fn conflicting_legacy_worktrees_abort_the_entire_migration() {
        let mut db = source();
        db.execute_batch("INSERT INTO projects VALUES ('other','other'); INSERT INTO environment_generations VALUES ('foreign','other','/original/checkout','ready','{}');").unwrap();
        assert!(catalog_upgrade(&mut db, 10, "backup", "release").is_err());
        assert_eq!(
            db.pragma_query_value(None, "user_version", |r| r.get::<_, u32>(0))
                .unwrap(),
            10
        );
        assert!(
            !db.prepare("SELECT 1 FROM sqlite_master WHERE name='deployments'")
                .unwrap()
                .exists([])
                .unwrap()
        );
        assert_eq!(
            db.query_row("SELECT count(*) FROM projects", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            2
        );
    }
}

pub(crate) fn catalog_upgrade(
    db: &mut Connection,
    from: u32,
    source: &str,
    release: &str,
) -> Result<()> {
    foreign_key_migration(db, |db| catalog_upgrade_inner(db, from, source, release))
}
