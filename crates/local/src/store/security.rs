//! Security controls owned by the single writer. No user data or bearer secrets
//! belong in the audit stream; exports are private operator assets.
use super::*;
use serde_json::{Value, json};
use std::sync::OnceLock;
use std::time::Instant;

pub(crate) const FRESH_MS: i64 = 30_000;
const CLOCK_SKEW_MS: i64 = 5_000;

fn clock_consistent(wall: i64, origin: i64, elapsed: i64) -> bool {
    wall.abs_diff(origin.saturating_add(elapsed)) <= CLOCK_SKEW_MS as u64
}
pub(super) fn clock(db: &Connection) -> Result<()> {
    static ORIGIN: OnceLock<(i64, Instant)> = OnceLock::new();
    let wall = crate::identity::now();
    let (origin, monotonic) = ORIGIN.get_or_init(|| (wall, Instant::now()));
    let persisted: i64 = db.query_row("SELECT clock_ms FROM security_state", [], |r| r.get(0))?;
    if !clock_consistent(wall, *origin, monotonic.elapsed().as_millis() as i64)
        || wall.saturating_add(CLOCK_SKEW_MS) < persisted
    {
        return Err(conflict(
            "security clock changed; correct the clock before admitting work",
        ));
    }
    Ok(())
}
pub(super) fn checkpoint_clock(db: &Connection) -> Result<()> {
    clock(db)?;
    db.execute(
        "UPDATE security_state SET clock_ms=max(clock_ms,?1)",
        [crate::identity::now()],
    )?;
    Ok(())
}
pub(super) fn admission(db: &Connection) -> Result<()> {
    clock(db)?;
    let (closed, full): (bool, bool) = db.query_row(
        "SELECT restore_closed,(SELECT count(*)>=10000 FROM security_audit) FROM security_state",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if closed || full {
        return Err(conflict(
            "governed admission closed; reconcile restore or export audit",
        ));
    }
    Ok(())
}
pub(super) fn session(db: &Connection, ctx: &crate::identity::Context, hash: &str) -> Result<()> {
    admission(db)?;
    if !db.prepare("SELECT 1 FROM identity_sessions s JOIN identity_realm r ON r.session_epoch=s.epoch JOIN identity_principals p ON p.id=s.principal WHERE s.token_hash=?1 AND s.principal=?2 AND s.expires_ms>?3 AND p.disabled=0 AND (s.provider IS NULL OR s.authoritative_until_ms>?3)")?
        .exists(params![hash,ctx.actor_id,crate::identity::now()])? {
        return Err(crate::identity::denied());
    }
    Ok(())
}

pub(super) fn policy_change(
    db: &Connection,
    ctx: &crate::identity::Context,
    deployment: &str,
    change: Value,
) -> Result<()> {
    let revision = super::authorization::policy(db, deployment)?;
    db.execute("INSERT INTO security_audit(at_ms,event) VALUES(?1,?2)",params![crate::identity::now(),json!({"stream":"authorization","action":"policy.change","realm_id":ctx.realm_id,"deployment_id":deployment,"actor_id":ctx.actor_id,"effective_principal_id":ctx.effective_principal_id,"policy_revision":revision,"change":change,"outcome":"committed"}).to_string()])?;
    Ok(())
}

pub(super) fn export(db: &Connection, after: i64) -> Result<Value> {
    if after < 0 {
        return Err(invalid("audit cursor must be nonnegative"));
    }
    let mut query = db.prepare("SELECT sequence,at_ms,event FROM security_audit WHERE sequence>?1 ORDER BY sequence LIMIT 200")?;
    let mut events = Vec::new();
    let mut bytes = 0;
    for row in query.query_map([after], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, String>(2)?,
        ))
    })? {
        let (sequence, at_ms, body) = row?;
        bytes += body.len();
        if bytes > 24 * 1024 {
            break;
        }
        events.push(json!({"sequence":sequence,"at_ms":at_ms,"event":serde_json::from_str::<Value>(&body)?}));
    }
    let through = events
        .last()
        .and_then(|v| v["sequence"].as_i64())
        .unwrap_or(after);
    let sha256 = crate::identity::hash(&serde_json::to_string(&events)?);
    let oldest: Option<i64> =
        db.query_row("SELECT min(sequence) FROM security_audit", [], |r| r.get(0))?;
    Ok(
        json!({"events":events,"after":after,"through":through,"sha256":sha256,"oldest_retained":oldest,"capacity":10000}),
    )
}
pub(super) fn acknowledge(
    db: &mut Connection,
    after: i64,
    through: i64,
    sha256: &str,
) -> Result<Value> {
    let tx = db.transaction()?;
    let page = export(&tx, after)?;
    // Only the oldest contiguous page can be released; a later cursor cannot
    // erase unseen events. The operator must archive these exact bytes first.
    let oldest = page["oldest_retained"]
        .as_i64()
        .ok_or_else(|| invalid("audit export is empty"))?;
    if after >= oldest || through <= after || page["through"] != through || page["sha256"] != sha256
    {
        return Err(conflict(
            "audit acknowledgement differs from the oldest retained export",
        ));
    }
    let count = tx.execute("DELETE FROM security_audit WHERE sequence<=?1", [through])?;
    let pending: i64 = tx.query_row("SELECT recovery_pending FROM security_state", [], |r| {
        r.get(0)
    })?;
    if pending > 0 {
        tx.execute(
            "INSERT INTO security_audit(at_ms,event) VALUES (?1,?2)",
            params![
                crate::identity::now(),
                json!({"stream":"recovery","action":"recovery.closed_audit_full","count":pending})
                    .to_string()
            ],
        )?;
        tx.execute("UPDATE security_state SET recovery_pending=0", [])?;
    }
    tx.commit()?;
    Ok(json!({"released":count,"through":through}))
}
pub(super) fn restore_status(db: &Connection) -> Result<Value> {
    Ok(db.query_row("SELECT s.restore_closed,s.restore_id,s.source_realm,r.id FROM security_state s CROSS JOIN identity_realm r", [], |r| Ok(json!({"closed":r.get::<_,bool>(0)?,"restore_id":r.get::<_,Option<String>>(1)?,"source_realm":r.get::<_,Option<String>>(2)?,"realm_id":r.get::<_,String>(3)?})))?)
}
pub(super) fn reconcile(db: &mut Connection, restore_id: &str, realm: &str) -> Result<Value> {
    let tx = db.transaction()?;
    checkpoint_clock(&tx)?;
    let state = restore_status(&tx)?;
    if state["closed"] != true
        || state["restore_id"] != restore_id
        || state["source_realm"] != realm
        || state["realm_id"] != realm
    {
        return Err(conflict(
            "restore identity differs; cross-realm activation requires an explicit identity remapping workflow",
        ));
    }
    if tx.prepare("SELECT 1 FROM catalog_principals")?.exists([])?
        && !tx
            .prepare("SELECT 1 FROM catalog_governance WHERE state='ready'")?
            .exists([])?
    {
        return Err(conflict(
            "reconcile current catalog identities and grants before opening restore",
        ));
    }
    tx.execute("DELETE FROM identity_sessions", [])?;
    tx.execute("DELETE FROM identity_logins", [])?;
    tx.execute(
        "UPDATE identity_realm SET session_epoch=session_epoch+1",
        [],
    )?;
    tx.execute("INSERT INTO identity_audit(at_ms,actor,effective_principal,action,target) SELECT ?1,local_owner,local_owner,'restore.reconciled',?2 FROM identity_realm",params![crate::identity::now(),restore_id])?;
    tx.execute("UPDATE security_state SET restore_closed=0", [])?;
    tx.commit()?;
    Ok(json!({"reconciled":true,"realm_id":realm,"sessions_rotated":true}))
}

/// Called only on a fresh, stopped restore destination, under restore-incomplete.
/// Local-owner backups preserve their original credentials and profile.
pub(crate) fn restore(db: &Connection, restore_id: &str) -> Result<bool> {
    let schema: u32 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
    if schema < 21 {
        return Ok(false);
    }
    let governed = db.prepare("SELECT 1 FROM identity_principals WHERE kind!='local_owner' UNION ALL SELECT 1 FROM identity_providers UNION ALL SELECT 1 FROM governed_branches")?.exists([])?;
    if !governed {
        return Ok(false);
    }
    let tx = db.unchecked_transaction()?;
    tx.execute("UPDATE security_state SET restore_closed=1,restore_id=?1,source_realm=(SELECT id FROM identity_realm)",[restore_id])?;
    tx.execute_batch("DELETE FROM identity_sessions; DELETE FROM identity_logins;
        UPDATE identity_realm SET session_epoch=session_epoch+1,bootstrap_principal=NULL;
        UPDATE identity_principals SET disabled=1 WHERE kind!='local_owner';
        DELETE FROM identity_memberships; DELETE FROM identity_providers;
        DELETE FROM authorization_roles; DELETE FROM authorization_grants; DELETE FROM data_grants;
        DELETE FROM authorization_mutations; DELETE FROM catalog_grant_origins;
        DELETE FROM catalog_grant_plans;
        UPDATE authorization_policy SET revision=revision+1;
        UPDATE authorization_executions SET state='cancelled';
        UPDATE isolated_executions SET state='failed',result_json=NULL;
        UPDATE data_operations SET state=CASE WHEN state='committing' THEN 'uncertain' ELSE 'interrupted' END,result_json=NULL;
        UPDATE catalog_governance SET revision=revision+1,state='dirty';
        INSERT OR IGNORE INTO governed_branches SELECT id,'ready' FROM branches;")?;
    // compute_ctl installs the new control password before PG accepts connections;
    // governed enrollment also retires every copied non-control login on startup.
    let endpoints = tx
        .prepare("SELECT endpoint_id FROM credentials")?
        .query_map([], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for endpoint in endpoints {
        tx.execute(
            "UPDATE credentials SET password=?2 WHERE endpoint_id=?1",
            params![endpoint, crate::identity::secret()?],
        )?;
        tx.execute(
            "UPDATE app_credentials SET password=?2 WHERE endpoint_id=?1",
            params![endpoint, crate::identity::secret()?],
        )?;
    }
    // Restoration must still close when the source audit was full. It does not
    // admit work; the durable closed marker is the recovery record until export.
    tx.commit()?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wall_clock_steps_cannot_extend_monotonic_lease_freshness() {
        assert!(clock_consistent(40_000, 10_000, 30_000));
        assert!(!clock_consistent(10_000, 10_000, 30_000));
        assert!(!clock_consistent(70_000, 10_000, 30_000));
    }
}

#[cfg(test)]
mod operational_tests {
    use super::*;
    use crate::identity::{self, AdminCommand as A, AuthCommand, Channel};
    fn fixture() -> (tempfile::TempDir, Store, String, String) {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(&dir.path().join("data")).unwrap();
        let id = store
            .identity_admin(A::Service {
                label: "worker".into(),
            })
            .unwrap()["principal_id"]
            .as_str()
            .unwrap()
            .to_owned();
        let token = store
            .identity_admin(A::IssueService {
                principal: id.clone(),
                scopes: vec![
                    identity::SELF_SCOPE.into(),
                    crate::authorization::CONTROL_SCOPE.into(),
                ],
                ttl_seconds: 300,
            })
            .unwrap()["token"]
            .as_str()
            .unwrap()
            .to_owned();
        (dir, store, id, token)
    }
    fn authenticate(store: &mut Store, token: &str) -> Result<Value> {
        store.identity_auth(AuthCommand::Authenticate {
            token: token.into(),
            channel: Channel::Service,
            csrf: None,
        })
    }
    #[test]
    fn bounded_audit_exhaustion_rolls_back_mutations_and_remains_exportable_after_restart() {
        let (_dir, mut store, id, token) = fixture();
        store.db.execute("DELETE FROM security_audit", []).unwrap();
        store.db.execute_batch("WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<10000) INSERT INTO security_audit(at_ms,event) SELECT i,'{}' FROM n;").unwrap();
        assert!(
            store
                .identity_admin(A::Disable {
                    principal: id.clone(),
                    disabled: true
                })
                .is_err()
        );
        assert!(
            !store
                .db
                .query_row(
                    "SELECT disabled FROM identity_principals WHERE id=?1",
                    [&id],
                    |r| r.get::<_, bool>(0)
                )
                .unwrap()
        );
        assert!(authenticate(&mut store, &token).is_err());
        // Audit failure cannot prevent crash recovery or operator export.
        store.recover_catalog_governance().unwrap();
        let first = export(&store.db, 0).unwrap();
        assert_eq!(first["events"].as_array().unwrap().len(), 200);
        assert!(
            acknowledge(
                &mut store.db,
                0,
                first["through"].as_i64().unwrap(),
                "tampered"
            )
            .is_err()
        );
        let later = export(&store.db, first["through"].as_i64().unwrap()).unwrap();
        assert!(
            acknowledge(
                &mut store.db,
                later["after"].as_i64().unwrap(),
                later["through"].as_i64().unwrap(),
                later["sha256"].as_str().unwrap()
            )
            .is_err()
        );
        acknowledge(
            &mut store.db,
            0,
            first["through"].as_i64().unwrap(),
            first["sha256"].as_str().unwrap(),
        )
        .unwrap();
        assert!(authenticate(&mut store, &token).is_ok());
        assert_eq!(store.db.query_row("SELECT count(*) FROM security_audit WHERE event LIKE '%recovery.closed_audit_full%'",[],|r|r.get::<_,i64>(0)).unwrap(),1);
        store
            .identity_admin(A::Disable {
                principal: id,
                disabled: true,
            })
            .unwrap();
        assert!(authenticate(&mut store, &token).is_err());
    }
    #[test]
    fn sqlite_full_rolls_back_security_changes_and_audit_does_not_contain_secrets() {
        let (_dir, mut store, id, token) = fixture();
        let text = export(&store.db, 0).unwrap().to_string();
        assert!(!text.contains(&token));
        assert!(!text.contains("token_hash"));
        store
            .db
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;")
            .unwrap();
        let pages: i64 = store
            .db
            .query_row("PRAGMA page_count", [], |r| r.get(0))
            .unwrap();
        store
            .db
            .pragma_update(None, "max_page_count", pages)
            .unwrap();
        let mut full = false;
        for _ in 0..100 {
            let result = store.db.execute(
                "INSERT INTO security_audit(at_ms,event) VALUES(1,?1)",
                [json!({"padding":"x".repeat(8000)}).to_string()],
            );
            if let Err(rusqlite::Error::SqliteFailure(e, _)) = result {
                assert_eq!(e.code, rusqlite::ErrorCode::DiskFull);
                full = true;
                break;
            }
        }
        assert!(full);
        // Force the very next audit append to require a page, so a mutation's
        // transaction cannot acknowledge success while its audit fails.
        store.db.execute_batch("CREATE TEMP TRIGGER force_full BEFORE INSERT ON identity_audit BEGIN INSERT INTO security_audit(at_ms,event) VALUES(1,json_object('padding',printf('%08000d',0))); END;").unwrap();
        assert!(
            store
                .identity_admin(A::Disable {
                    principal: id.clone(),
                    disabled: true
                })
                .is_err()
        );
        assert!(
            !store
                .db
                .query_row(
                    "SELECT disabled FROM identity_principals WHERE id=?1",
                    [id],
                    |r| r.get::<_, bool>(0)
                )
                .unwrap()
        );
    }
    #[test]
    fn expired_provider_freshness_and_clock_rollback_close_existing_leases() {
        let (_dir, mut store, _, token) = fixture();
        let ctx: identity::Context =
            serde_json::from_value(authenticate(&mut store, &token).unwrap()).unwrap();
        assert!(session(&store.db, &ctx, &identity::hash(&token)).is_ok());
        // Model a formerly verified provider session without network access.
        store
            .db
            .execute(
                "INSERT INTO identity_providers(id,config) VALUES('test','{}')",
                [],
            )
            .unwrap();
        store
            .db
            .execute(
                "UPDATE identity_sessions SET provider='test',authoritative_until_ms=?1",
                [identity::now() - 1],
            )
            .unwrap();
        assert!(session(&store.db, &ctx, &identity::hash(&token)).is_err());
        store
            .db
            .execute(
                "UPDATE identity_sessions SET authoritative_until_ms=?1",
                [identity::now() + FRESH_MS],
            )
            .unwrap();
        assert!(session(&store.db, &ctx, &identity::hash(&token)).is_ok());
        store
            .db
            .execute(
                "UPDATE security_state SET clock_ms=?1",
                [identity::now() + 60_000],
            )
            .unwrap();
        assert!(session(&store.db, &ctx, &identity::hash(&token)).is_err());
        assert!(authenticate(&mut store, &token).is_err());
    }
}

impl Store {
    pub(crate) fn audit_denial(
        &self,
        ctx: &crate::identity::Context,
        deployment: Option<&str>,
    ) -> Result<()> {
        self.db.execute("INSERT INTO security_audit(at_ms,event) VALUES(?1,?2)",params![crate::identity::now(),json!({"stream":"authorization","action":"access.denied","actor_id":ctx.actor_id,"effective_principal_id":ctx.effective_principal_id,"realm_id":ctx.realm_id,"deployment_id":deployment,"outcome":"denied"}).to_string()])?;
        Ok(())
    }
    pub(crate) fn audit_authorized_job(
        &self,
        ctx: crate::identity::Context,
        deployment: Option<String>,
        job: Result<super::identity::IdentityJob>,
    ) -> Result<super::identity::IdentityJob> {
        let job = match job {
            Ok(job) => job,
            Err(error) => {
                let _ = self.audit_denial(&ctx, deployment.as_deref());
                return Err(error);
            }
        };
        Ok(Box::new(move || {
            let pending = job();
            Ok(Box::new(move |store| {
                let result = pending.and_then(|commit| commit(store));
                if result.is_err() {
                    let _ = store.audit_denial(&ctx, deployment.as_deref());
                }
                result
            }))
        }))
    }
}
