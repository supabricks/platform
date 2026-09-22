use super::*;
use crate::{
    catalog::governance::{
        self as gov, AdminCommand, Broker, Change, Grants, Object, Origin, Plan, Principal,
        ReadCommand, Snapshot, Table,
    },
    identity::Context,
};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};

fn revision(db: &Connection) -> Result<i64> {
    Ok(db.query_row("SELECT revision FROM catalog_governance", [], |r| r.get(0))?)
}
fn audit(db: &Connection, action: &str, target: &str) -> Result<()> {
    db.execute("INSERT INTO catalog_grant_audit(at_ms,actor,action,target,revision) SELECT ?1,local_owner,?2,?3,(SELECT revision FROM catalog_governance) FROM identity_realm",params![crate::identity::now(),action,target])?;
    Ok(())
}
fn fence(db: &Connection, expected: i64) -> Result<()> {
    if revision(db)? != expected {
        return Err(gov::denied());
    }
    Ok(())
}
fn principal_exists(db: &Connection, subject: &crate::authorization::Subject) -> Result<()> {
    let exists = match subject {
        crate::authorization::Subject::Principal(p) => db
            .prepare("SELECT 1 FROM identity_principals WHERE id=?1 AND kind!='local_owner'")?
            .exists([p])?,
        crate::authorization::Subject::Group(g) => db
            .prepare("SELECT 1 FROM identity_groups WHERE id=?1")?
            .exists([g])?,
    };
    if !exists {
        return Err(gov::denied());
    }
    Ok(())
}
fn members(db: &Connection, subject: &str) -> Result<Vec<String>> {
    if let Some(p) = subject.strip_prefix("principal:") {
        Ok(db
            .prepare("SELECT id FROM identity_principals WHERE id=?1 AND disabled=0")?
            .query_map([p], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?)
    } else if let Some(g) = subject.strip_prefix("group:") {
        Ok(db.prepare("SELECT p.id FROM identity_memberships m JOIN identity_principals p ON p.id=m.principal WHERE m.group_id=?1 AND p.disabled=0")?.query_map([g],|r|r.get(0))?.collect::<rusqlite::Result<_>>()?)
    } else {
        Err(gov::denied())
    }
}
pub(super) fn snapshot(db: &Connection, broker: &Broker, changes: &[Change]) -> Result<Snapshot> {
    let mut origins=db.prepare("SELECT publication,subject,publication_revision,tables_json FROM catalog_grant_origins ORDER BY publication,subject")?
        .query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,i64>(2)?,r.get::<_,String>(3)?)))?
        .map(|r|{let(p,s,v,t)=r?;Ok(Origin{publication:p,subject:s,revision:v,tables:serde_json::from_str(&t)?})}).collect::<Result<Vec<_>>>()?;
    let principals=db.prepare("SELECT principal,subject,uc_id FROM catalog_principals WHERE provider=?1 AND state='ready' ORDER BY principal")?
        .query_map([&broker.provider],|r|Ok(Principal{principal:r.get(0)?,subject:r.get(1)?,uc_id:r.get(2)?}))?.collect::<rusqlite::Result<Vec<_>>>()?;
    let pending: i64 = db.query_row(
        "SELECT count(*) FROM catalog_principals WHERE state='ready' AND provider!=?1",
        [&broker.provider],
        |r| r.get(0),
    )?;
    if pending != 0 || principals.len() > 64 || changes.len() > 128 {
        return Err(gov::denied());
    }
    let publications = db
        .prepare(
            "SELECT record_json FROM catalog_publications WHERE state IN ('published','retiring','retired') ORDER BY id",
        )?
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| {
            Ok(serde_json::from_str::<
                crate::catalog::publication::Publication,
            >(&r?)?)
        })
        .collect::<Result<Vec<_>>>()?;
    let mut objects = BTreeMap::new();
    let metastore = Object {
        kind: "metastore".into(),
        name: broker.metastore.clone(),
        id: broker.metastore.clone(),
        allow_missing: false,
        definition: None,
    };
    objects.insert(metastore.key(), metastore);
    let mut tables = Vec::new();
    for p in publications {
        if p.namespace.provider_id != broker.provider
            || p.namespace.metastore_id != broker.metastore
        {
            return Err(gov::denied());
        }
        for (kind, name, id) in [
            (
                "catalog",
                p.namespace.catalog.clone(),
                p.namespace.catalog_id.clone(),
            ),
            (
                "schema",
                format!("{}.{}", p.namespace.catalog, p.namespace.schema),
                p.namespace.schema_id.clone(),
            ),
        ] {
            let o = Object {
                kind: kind.into(),
                name,
                id: id.ok_or_else(gov::denied)?,
                allow_missing: false,
                definition: None,
            };
            if objects
                .get(&o.key())
                .is_some_and(|old: &Object| old.id != o.id)
            {
                return Err(gov::denied());
            }
            objects.insert(o.key(), o);
        }
        for t in &p.tables {
            if p.state == "published" && t.state != "verified" {
                return Err(gov::denied());
            }
            let name = t.body["name"].as_str().ok_or_else(gov::denied)?;
            let o = Object {
                kind: "table".into(),
                name: format!("{}.{}.{}", p.namespace.catalog, p.namespace.schema, name),
                id: t.id.to_string(),
                allow_missing: p.state == "retired",
                definition: Some(t.clone()),
            };
            if objects
                .get(&o.key())
                .is_some_and(|old: &Object| old.id != o.id)
            {
                return Err(gov::denied());
            }
            objects.insert(o.key(), o.clone());
            if p.state == "published" {
                tables.push(Table {
                    publication: p.id.to_string(),
                    revision: p.revision.ok_or_else(gov::denied)?,
                    deployment: p.deployment_id.to_string(),
                    catalog: p.namespace.catalog.clone(),
                    schema: format!("{}.{}", p.namespace.catalog, p.namespace.schema),
                    object: o,
                });
            }
        }
    }
    if objects.len() > 128 {
        return Err(gov::denied());
    }
    let mut changed = BTreeSet::new();
    for change in changes {
        principal_exists(db, &change.subject)?;
        let subject = change.subject.key();
        if !changed.insert((change.publication.clone(), subject.clone())) {
            return Err(gov::denied());
        }
        origins.retain(|o| o.publication != change.publication || o.subject != subject);
        if change.present {
            let mut ids = change.tables.clone();
            ids.sort();
            ids.dedup();
            if ids.is_empty()
                || ids.len() != change.tables.len()
                || ids.iter().any(|id| {
                    !tables.iter().any(|t| {
                        t.publication == change.publication
                            && t.revision == change.publication_revision
                            && t.object.id == *id
                    })
                })
            {
                return Err(gov::denied());
            }
            origins.push(Origin {
                publication: change.publication.clone(),
                subject,
                revision: change.publication_revision,
                tables: ids,
            });
        }
    }
    if origins.len() > 512 {
        return Err(gov::denied());
    }
    origins.sort_by(|a, b| (&a.publication, &a.subject).cmp(&(&b.publication, &b.subject)));
    let mut desired: Grants = objects
        .keys()
        .map(|k| (k.clone(), BTreeMap::new()))
        .collect();
    for origin in &origins {
        // Retired/recreated publications never inherit grants. Retained intents
        // can only resolve to the original publication revision and table UUID.
        let selected: Vec<_> = tables
            .iter()
            .filter(|t| {
                t.publication == origin.publication
                    && t.revision == origin.revision
                    && origin.tables.contains(&t.object.id)
            })
            .collect();
        if selected.len() != origin.tables.len() {
            return Err(gov::denied());
        }
        for member in members(db, &origin.subject)? {
            let p = principals
                .iter()
                .find(|p| p.principal == member)
                .ok_or_else(gov::denied)?;
            for table in &selected {
                for (object, privilege) in [
                    (format!("catalog/{}", table.catalog), "USE CATALOG"),
                    (format!("schema/{}", table.schema), "USE SCHEMA"),
                    (table.object.key(), "SELECT"),
                ] {
                    desired
                        .entry(object)
                        .or_default()
                        .entry(p.subject.clone())
                        .or_default()
                        .insert(privilege.into());
                }
            }
        }
    }
    Ok(Snapshot {
        revision: revision(db)?,
        provider: broker.provider.clone(),
        principals,
        objects: objects.into_values().collect(),
        tables,
        origins,
        desired,
    })
}
impl Store {
    pub(crate) fn catalog_governance_status(&self) -> Result<serde_json::Value> {
        let revision = revision(&self.db)?;
        let state: String = self
            .db
            .query_row("SELECT state FROM catalog_governance", [], |r| r.get(0))?;
        let principals=self.db.prepare("SELECT principal,subject,uc_id,state FROM catalog_principals ORDER BY principal")?.query_map([],|r|Ok(json!({"principal":r.get::<_,String>(0)?,"subject":r.get::<_,String>(1)?,"uc_id":r.get::<_,Option<String>>(2)?,"state":r.get::<_,String>(3)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
        let plans = self
            .db
            .prepare("SELECT id,state FROM catalog_grant_plans ORDER BY rowid DESC LIMIT 50")?
            .query_map([], |r| {
                Ok(json!({"id":r.get::<_,String>(0)?,"state":r.get::<_,String>(1)?}))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(json!({"revision":revision,"state":state,"plans":plans,"principals":principals}))
    }

    pub(crate) fn invalidate_catalog_governance(&mut self) -> Result<()> {
        let tx = self.db.transaction()?;
        tx.execute(
            "UPDATE catalog_governance SET revision=revision+1,state='dirty'",
            [],
        )?;
        audit(&tx, "provider.change", "")?;
        tx.commit()?;
        Ok(())
    }

    pub(crate) fn recover_catalog_governance(&mut self) -> Result<()> {
        let tx = self.db.transaction()?;
        tx.execute(
            "UPDATE catalog_grant_plans SET state='failed' WHERE state='applying'",
            [],
        )?;
        tx.execute(
            "UPDATE catalog_governance SET revision=revision+1,state='dirty'",
            [],
        )?;
        if tx.query_row("SELECT count(*)<10000 FROM security_audit", [], |r| {
            r.get::<_, bool>(0)
        })? {
            audit(&tx, "grant.recovery_closed", "")?;
        } else {
            tx.execute(
                "UPDATE security_state SET recovery_pending=recovery_pending+1",
                [],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    pub(crate) fn catalog_governance_admin(
        &mut self,
        command: AdminCommand,
        broker: Broker,
    ) -> Result<super::identity::IdentityJob> {
        match command {
            AdminCommand::Status {} => {
                let value = self.catalog_governance_status()?;
                Ok(Box::new(move || Ok(Box::new(move |_| Ok(value)))))
            }
            AdminCommand::ResolvePrincipal {
                principal,
                expected_uc_id,
            } => {
                let subject:String=self.db.query_row("SELECT subject FROM catalog_principals WHERE principal=?1 AND provider=?2 AND state='pending'",params![principal,broker.provider],|r|r.get(0)).optional()?.ok_or_else(gov::denied)?;
                let mapped = Principal {
                    principal: principal.clone(),
                    subject,
                    uc_id: expected_uc_id,
                };
                let rev = revision(&self.db)?;
                audit(&self.db, "principal.resolve_intent", &principal)?;
                Ok(Box::new(move || {
                    broker.principal(&mapped)?;
                    Ok(Box::new(move |store| {
                        fence(&store.db, rev)?;
                        let tx = store.db.transaction()?;
                        if tx.execute("UPDATE catalog_principals SET uc_id=?1,state='ready' WHERE principal=?2 AND state='pending'",params![mapped.uc_id,principal])?!=1 {return Err(gov::denied());}
                        tx.execute(
                            "UPDATE catalog_governance SET revision=revision+1,state='dirty'",
                            [],
                        )?;
                        audit(&tx, "principal.resolved", &principal)?;
                        tx.commit()?;
                        Ok(json!({"principal":principal,"uc_id":mapped.uc_id}))
                    }))
                }))
            }
            AdminCommand::MapPrincipal { principal } => {
                let realm: String =
                    self.db
                        .query_row("SELECT id FROM identity_realm", [], |r| r.get(0))?;
                principal_exists(
                    &self.db,
                    &crate::authorization::Subject::Principal(principal.clone()),
                )?;
                let subject = gov::subject(&realm, &principal)?;
                let count: i64 =
                    self.db
                        .query_row("SELECT count(*) FROM catalog_principals", [], |r| r.get(0))?;
                if count >= 64 {
                    return Err(conflict("catalog principal limit reached"));
                }
                if self
                    .db
                    .prepare("SELECT 1 FROM catalog_principals WHERE principal=?1")?
                    .exists([&principal])?
                {
                    return Err(conflict(
                        "principal already mapped or pending; inspect catalog governance state",
                    ));
                }
                let tx = self.db.transaction()?;
                tx.execute(
                    "INSERT INTO catalog_principals VALUES (?1,?2,NULL,?3,'pending')",
                    params![principal, subject, broker.provider],
                )?;
                tx.execute(
                    "UPDATE catalog_governance SET revision=revision+1,state='dirty'",
                    [],
                )?;
                audit(&tx, "principal.intent", &principal)?;
                tx.commit()?;
                Ok(Box::new(move || {
                    let result = broker.create_principal(&principal, &subject);
                    Ok(Box::new(move |store| {
                        let mapped = result?;
                        let tx = store.db.transaction()?;
                        tx.execute("UPDATE catalog_principals SET uc_id=?1,state='ready' WHERE principal=?2 AND state='pending'",params![mapped.uc_id,principal])?;
                        tx.execute(
                            "UPDATE catalog_governance SET revision=revision+1,state='dirty'",
                            [],
                        )?;
                        audit(&tx, "principal.mapped", &principal)?;
                        tx.commit()?;
                        Ok(json!({"principal":principal,"uc_id":mapped.uc_id}))
                    }))
                }))
            }
            AdminCommand::Plan { changes } => {
                let snapshot = snapshot(&self.db, &broker, &changes)?;
                Ok(Box::new(move || {
                    let observed = broker.observe(&snapshot)?;
                    Ok(Box::new(move |store| {
                        fence(&store.db, snapshot.revision)?;
                        let plan = Plan { snapshot, observed };
                        let id = gov::digest(&plan)?;
                        let tx = store.db.transaction()?;
                        tx.execute("INSERT OR IGNORE INTO catalog_grant_plans VALUES (?1,?2,?3,'planned',NULL)",params![id,plan.snapshot.revision,serde_json::to_string(&plan)?])?;
                        audit(&tx, "grant.plan", &id)?;
                        tx.commit()?;
                        Ok(
                            json!({"id":id,"revision":plan.snapshot.revision,"observed":plan.observed,"desired":plan.snapshot.desired,"origins":plan.snapshot.origins}),
                        )
                    }))
                }))
            }
            AdminCommand::Apply { plan: id, key } => {
                crate::authorization::key(&key)?;
                let (body, state, saved): (String, String, Option<String>) = self
                    .db
                    .query_row(
                        "SELECT body,state,request_key FROM catalog_grant_plans WHERE id=?1",
                        [&id],
                        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                    )
                    .optional()?
                    .ok_or_else(gov::denied)?;
                if state == "applied" && saved.as_deref() == Some(&key) {
                    return Ok(Box::new(move || {
                        Ok(Box::new(move |_| Ok(json!({"id":id,"state":"applied"}))))
                    }));
                }
                if state != "planned"
                    || self
                        .db
                        .prepare("SELECT 1 FROM catalog_grant_plans WHERE state='applying'")?
                        .exists([])?
                {
                    return Err(gov::denied());
                }
                let plan: Plan = serde_json::from_str(&body)?;
                fence(&self.db, plan.snapshot.revision)?;
                if gov::digest(&plan)? != id || broker.provider != plan.snapshot.provider {
                    return Err(gov::denied());
                }
                let tx = self.db.transaction()?;
                tx.execute(
                    "UPDATE catalog_grant_plans SET state='applying',request_key=?1 WHERE id=?2",
                    params![key, id],
                )?;
                tx.execute(
                    "UPDATE catalog_governance SET revision=revision+1,state='applying'",
                    [],
                )?;
                // Desired administrative intents survive partial UC application.
                tx.execute("DELETE FROM catalog_grant_origins", [])?;
                for origin in &plan.snapshot.origins {
                    tx.execute(
                        "INSERT INTO catalog_grant_origins VALUES (?1,?2,?3,?4)",
                        params![
                            origin.publication,
                            origin.subject,
                            origin.revision,
                            serde_json::to_string(&origin.tables)?
                        ],
                    )?;
                }
                audit(&tx, "grant.apply_intent", &id)?;
                tx.commit()?;
                Ok(Box::new(move || {
                    let result = broker.apply(&plan);
                    Ok(Box::new(move |store| {
                        let ok =
                            result.is_ok() && revision(&store.db)? == plan.snapshot.revision + 1;
                        let tx = store.db.transaction()?;
                        tx.execute(
                            "UPDATE catalog_grant_plans SET state=?1 WHERE id=?2",
                            params![if ok { "applied" } else { "failed" }, id],
                        )?;
                        tx.execute(
                            "UPDATE catalog_governance SET revision=revision+1,state=?1",
                            [if ok { "ready" } else { "dirty" }],
                        )?;
                        audit(
                            &tx,
                            if ok {
                                "grant.applied"
                            } else {
                                "grant.unresolved"
                            },
                            &id,
                        )?;
                        tx.commit()?;
                        if !ok {
                            return Err(gov::denied());
                        }
                        Ok(json!({"id":id,"state":"applied"}))
                    }))
                }))
            }
        }
    }
    pub(crate) fn catalog_governance_read(
        &mut self,
        ctx: Context,
        token_hash: String,
        command: ReadCommand,
        broker: Broker,
    ) -> Result<super::identity::IdentityJob> {
        super::authorization::validate_context(&self.db, &ctx)?;
        if let ReadCommand::List { search } = &command {
            if search.len() > 256 {
                return Err(gov::denied());
            }
        }
        let state: String = self
            .db
            .query_row("SELECT state FROM catalog_governance", [], |r| r.get(0))?;
        if state != "ready" {
            return Err(gov::denied());
        }
        if !self.db.prepare("SELECT 1 FROM catalog_principals WHERE principal=?1 AND state='ready' AND provider=?2")?.exists(params![ctx.actor_id,broker.provider])? {return Err(gov::denied());}
        let snapshot = snapshot(&self.db, &broker, &[])?;
        let rev = snapshot.revision;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(25);
        Ok(Box::new(move || {
            let result = broker.read(&snapshot, &ctx.actor_id, ctx.expires_ms, &command);
            Ok(Box::new(move |store| {
                if std::time::Instant::now() >= deadline {
                    return Err(gov::denied());
                }
                fence(&store.db, rev)?;
                super::authorization::validate_context(&store.db, &ctx)?;
                super::security::session(&store.db, &ctx, &token_hash)?;
                if result.is_err() {
                    let tx = store.db.transaction()?;
                    tx.execute(
                        "UPDATE catalog_governance SET revision=revision+1,state='dirty'",
                        [],
                    )?;
                    tx.execute("INSERT INTO catalog_grant_audit(at_ms,actor,action,target,revision) VALUES (?1,?2,'grant.drift_or_unavailable','',?3)", params![crate::identity::now(),ctx.actor_id,revision(&tx)?])?;
                    tx.commit()?;
                }
                result
            }))
        }))
    }
}

#[cfg(test)]
#[path = "catalog_governance_tests.rs"]
mod tests;
