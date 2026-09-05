use super::error::{Result, conflict};
use rusqlite::{Connection, TransactionBehavior};
const MIGRATIONS: &[&str] = &[
    include_str!("migrations/0001_state.sql"),
    include_str!("migrations/0002_work.sql"),
];
pub const SCHEMA_VERSION: u32 = MIGRATIONS.len() as u32;

pub(super) fn migrate(db: &mut Connection) -> Result<()> {
    apply(db, MIGRATIONS)
}
fn apply(db: &mut Connection, migrations: &[&str]) -> Result<()> {
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
