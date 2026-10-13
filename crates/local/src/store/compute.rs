use super::{
    Result, Store, branch,
    error::{conflict, missing},
    now_ms,
};
use crate::{
    compute::{Sample, Settings, profile},
    operations::Step,
};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use supabricks_core::resource::{BranchId, DesiredState, ProjectId};

pub(super) fn settings(db: &Connection, id: BranchId) -> Result<Settings> {
    Ok(db.query_row(
        "SELECT size,idle_timeout_seconds FROM branch_compute WHERE branch_id=?1",
        [id.to_string()],
        |r| {
            Ok(Settings {
                size: r.get(0)?,
                idle_timeout_seconds: r.get(1)?,
            })
        },
    )?)
}
pub(super) fn write_settings(db: &Connection, id: BranchId, value: &Settings) -> Result<()> {
    db.execute(
        "UPDATE branch_compute SET size=?2,idle_timeout_seconds=?3 WHERE branch_id=?1",
        params![id.to_string(), value.size, value.idle_timeout_seconds],
    )?;
    Ok(())
}
/// Includes replication even between capture polls, when it owns no short work lease.
pub(super) fn idle(db: &Connection, id: BranchId) -> Result<bool> {
    Ok(!db.prepare("SELECT 1 FROM connection_leases WHERE branch_id=?1
      UNION ALL SELECT 1 FROM leases WHERE branch_id=?1 AND expires_at_ms>?2
      UNION ALL SELECT 1 FROM branch_pins WHERE parent_id=?1 AND active=1
      UNION ALL SELECT 1 FROM ingest_jobs WHERE branch_id=?1 AND state IN ('queued','loading','reconciling')
      UNION ALL SELECT 1 FROM project_applies p, json_each(p.record_json,'$.resources') r WHERE p.state IN ('queued','preparing','activating') AND json_extract(r.value,'$.branch')=?1
      UNION ALL SELECT 1 FROM sync_captures WHERE branch_id=?1 AND state!='deleted'")?.exists(params![id.to_string(),now_ms()?])?)
}

pub(super) fn configure(
    db: &Connection,
    project: ProjectId,
    id: BranchId,
    expected: i64,
    value: &Settings,
    restart: bool,
) -> Result<(BranchId, i64, Vec<Step>)> {
    value.validate()?;
    let b = branch(db, id)?;
    let edits: i64 = db.query_row(
        "SELECT edits FROM branch_details WHERE branch_id=?1",
        [id.to_string()],
        |r| r.get(0),
    )?;
    if b.branch.project_id != project {
        return Err(missing("branch in project"));
    }
    if b.revision + edits != expected {
        return Err(conflict("stale resource revision"));
    }
    if b.expired
        || b.endpoint.desired_state == DesiredState::Deleted
        || b.revision != b.observed_revision
    {
        return Err(conflict(
            "wait for a live, converged branch before changing compute settings",
        ));
    }
    let old = settings(db, id)?;
    if old == *value {
        return Ok((id, b.revision, vec![]));
    }
    let resize = old.size != value.size;
    let running = b.endpoint.desired_state == DesiredState::Running;
    if resize {
        crate::compute::validate_host(profile(&value.size)?)?;
        if running && !restart {
            return Err(conflict(
                "resize restarts PostgreSQL; set allow_restart to true",
            ));
        }
        if !idle(db, id)? {
            return Err(conflict(
                "branch has connections, work, pins, imports or capture; drain them before resizing",
            ));
        }
    }
    write_settings(db, id, value)?;
    if resize && running {
        let revision = b
            .revision
            .checked_add(1)
            .ok_or_else(|| conflict("resource revision exhausted"))?;
        db.execute(
            "UPDATE branches SET revision=?2 WHERE id=?1",
            params![id.to_string(), revision],
        )?;
        Ok((
            id,
            revision,
            vec![
                Step::CaptureSuspend,
                Step::StopCompute,
                Step::RetireCompute,
                Step::EnsureTimeline,
                Step::StartCompute,
            ],
        ))
    } else {
        db.execute(
            "UPDATE branch_details SET edits=edits+1 WHERE branch_id=?1",
            [id.to_string()],
        )?;
        Ok((id, b.revision, vec![]))
    }
}
impl Store {
    pub(crate) fn compute_settings(&self, id: BranchId) -> Result<Settings> {
        settings(&self.db, id)
    }
    pub(crate) fn compute_activity(&self, id: BranchId) -> Result<i64> {
        Ok(self.db.query_row(
            "SELECT activity_epoch FROM branch_compute WHERE branch_id=?1",
            [id.to_string()],
            |r| r.get(0),
        )?)
    }
    pub(crate) fn compute_idle(&self, id: BranchId) -> Result<bool> {
        idle(&self.db, id)
    }
    pub(crate) fn connection_limit(&self, id: BranchId) -> Result<usize> {
        Ok(profile(&self.compute_settings(id)?.size)?.max_connections)
    }
    pub(crate) fn compute_view(&self, id: BranchId) -> Result<Value> {
        let settings = self.compute_settings(id)?;
        let (reason,at): (Option<String>,Option<i64>) = self.db.query_row("SELECT last_suspend_reason,last_suspended_at_ms FROM branch_compute WHERE branch_id=?1", [id.to_string()], |r| Ok((r.get(0)?,r.get(1)?)))?;
        let capture: bool = self
            .db
            .prepare("SELECT 1 FROM sync_captures WHERE branch_id=?1 AND state!='deleted'")?
            .exists([id.to_string()])?;
        Ok(
            json!({"size":settings.size,"idle_timeout_seconds":settings.idle_timeout_seconds,
            "last_suspend_reason":reason,"last_suspended_at_ms":at,"capture_holds_awake":capture,
            "resource_model":"shared_host_tuning"}),
        )
    }
    pub(crate) fn save_storage_sample(&self, s: Sample) -> Result<()> {
        // A deleted timeline may finish sampling while its operation is reconciling.
        if self.branch(s.branch)?.endpoint.desired_state == DesiredState::Deleted {
            return Ok(());
        }
        let logical = s.logical.and_then(|n| i64::try_from(n).ok());
        let physical = s.physical.and_then(|n| i64::try_from(n).ok());
        self.db.execute("INSERT INTO branch_storage_samples VALUES (?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(branch_id) DO UPDATE SET generation=excluded.generation,sampled_at_ms=excluded.sampled_at_ms,logical_bytes=excluded.logical_bytes,physical_bytes=excluded.physical_bytes,logical_size_accurate=excluded.logical_size_accurate,available=excluded.available",params![s.branch.to_string(),self.generation,s.sampled_at_ms,logical,physical,s.accurate,s.available])?;
        Ok(())
    }
    pub(crate) fn storage_view(&self, id: BranchId) -> Result<Value> {
        let row = self.db.query_row("SELECT generation,sampled_at_ms,logical_bytes,physical_bytes,logical_size_accurate,available FROM branch_storage_samples WHERE branch_id=?1",[id.to_string()],|r| Ok((r.get::<_,i64>(0)?,r.get::<_,i64>(1)?,r.get::<_,Option<i64>>(2)?,r.get::<_,Option<i64>>(3)?,r.get::<_,bool>(4)?,r.get::<_,bool>(5)?))).optional()?;
        let Some((generation, at, logical, physical, accurate, available)) = row else {
            return Ok(
                json!({"status":"unavailable","sampled_at_ms":null,"logical_bytes":null,"exclusive_physical_bytes":null,"logical_size_accurate":false}),
            );
        };
        let status = if !available || logical.is_none() || physical.is_none() {
            "unavailable"
        } else if generation != self.generation || now_ms()?.saturating_sub(at) > 120_000 {
            "stale"
        } else {
            "fresh"
        };
        Ok(
            json!({"status":status,"sampled_at_ms":at,"logical_bytes":logical,"exclusive_physical_bytes":physical,"logical_size_accurate":accurate}),
        )
    }
    pub(crate) fn database_storage(&self, ids: &[BranchId]) -> Result<Value> {
        let samples = ids
            .iter()
            .map(|id| self.storage_view(*id))
            .collect::<Result<Vec<_>>>()?;
        let status = if samples.is_empty() || samples.iter().any(|s| s["status"] == "unavailable") {
            "unavailable"
        } else if samples.iter().any(|s| s["status"] == "stale") {
            "stale"
        } else {
            "fresh"
        };
        let total = if status == "unavailable" {
            None
        } else {
            samples.iter().try_fold(0u64, |sum, s| {
                sum.checked_add(s["exclusive_physical_bytes"].as_u64()?)
            })
        };
        let at = samples
            .iter()
            .filter_map(|s| s["sampled_at_ms"].as_i64())
            .min();
        Ok(
            json!({"status":status,"sampled_at_ms":at,"total_physical_bytes":total,"scope":"live_database_timeline_layers","branch_count":ids.len()}),
        )
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::{
        operations::{Mutation, Operation, Ports},
        project::ProjectConfig,
    };
    pub(crate) fn fixture() -> (tempfile::TempDir, Store, ProjectId, BranchId) {
        let root = tempfile::tempdir().unwrap();
        let mut store = Store::open(&root.path().join("cell")).unwrap();
        let project = ProjectId::new();
        store
            .register_project(&ProjectConfig {
                format_version: 1,
                id: project,
                name: "compute".into(),
            })
            .unwrap();
        let op = store
            .submit(
                project,
                "root",
                Mutation::CreateDatabase {
                    name: "main".into(),
                    ports: Ports {
                        sql: 5400,
                        external_http: 5401,
                        internal_http: 5402,
                    },
                },
            )
            .unwrap();
        finish(&mut store, &op);
        (root, store, project, op.branch_id)
    }
    pub(crate) fn finish(store: &mut Store, op: &Operation) {
        while let Some(ticket) = store.ticket(op.id).unwrap() {
            if ticket.step == Step::CaptureSuspend {
                store
                    .capture_suspend_lsn(&ticket, "0/2000".parse().unwrap())
                    .unwrap();
            }
            store
                .checkpoint(&ticket, json!({"step":ticket.idempotency_key()}))
                .unwrap();
        }
    }
    pub(crate) fn configure_idle(
        store: &mut Store,
        project: ProjectId,
        id: BranchId,
        timeout: Option<u32>,
    ) -> Operation {
        let revision = store.database_record(project, id).unwrap().revision;
        let mut settings = store.compute_settings(id).unwrap();
        settings.idle_timeout_seconds = timeout;
        store
            .submit(
                project,
                &format!("settings-{revision}"),
                Mutation::ConfigureCompute {
                    branch_id: id,
                    expected_revision: revision,
                    settings,
                    allow_restart: false,
                    api_request: None,
                },
            )
            .unwrap()
    }
    #[test]
    fn compute_resize_requires_review_draining_and_revision_then_replays_after_owner_loss() {
        let (_root, mut store, project, id) = fixture();
        let before = store.branch(id).unwrap();
        let password = store.app_password(before.endpoint.id).unwrap();
        let request = Mutation::ConfigureCompute {
            branch_id: id,
            expected_revision: 1,
            settings: Settings {
                size: "s".into(),
                idle_timeout_seconds: None,
            },
            allow_restart: true,
            api_request: Some(json!({"compute_size":"s","allow_restart":true})),
        };
        let mut no_review = request.clone();
        if let Mutation::ConfigureCompute { allow_restart, .. } = &mut no_review {
            *allow_restart = false;
        }
        assert!(store.submit(project, "no-review", no_review).is_err());
        let lease = store.accept_connection(id).unwrap();
        assert!(store.submit(project, "busy", request.clone()).is_err());
        assert_eq!(store.compute_settings(id).unwrap().size, "m");
        store.release_connection(lease).unwrap();
        let op = store.submit(project, "resize", request.clone()).unwrap();
        assert_eq!(
            op.steps,
            vec![
                Step::CaptureSuspend,
                Step::StopCompute,
                Step::RetireCompute,
                Step::EnsureTimeline,
                Step::StartCompute
            ]
        );
        assert_eq!(op.revision, 2);
        // Recover after recording the WAL boundary but before advancing its step.
        let ticket = store.ticket(op.id).unwrap().unwrap();
        store
            .capture_suspend_lsn(&ticket, "0/2000".parse().unwrap())
            .unwrap();
        let path = store.root().to_owned();
        drop(store);
        let mut store = Store::open(&path).unwrap();
        assert_eq!(
            store.submit(project, "resize", request.clone()).unwrap().id,
            op.id
        );
        assert_eq!(
            store.suspend_lsn(id, 2).unwrap(),
            Some("0/2000".parse().unwrap())
        );
        let mut altered = request.clone();
        if let Mutation::ConfigureCompute { api_request, .. } = &mut altered {
            *api_request =
                Some(json!({"compute_size":"s","allow_restart":true,"idle_timeout_seconds":null}));
        }
        // The resolved settings are identical, but this is a different HTTP patch.
        assert!(store.submit(project, "resize", altered).is_err());
        assert!(store.submit(project, "stale", request).is_err());
        finish(&mut store, &op);
        let after = store.branch(id).unwrap();
        assert_eq!(after.branch.timeline_id, before.branch.timeline_id);
        assert_eq!(after.endpoint.id, before.endpoint.id);
        assert_eq!(after.ports, before.ports);
        assert_eq!(store.app_password(after.endpoint.id).unwrap(), password);
        assert_eq!(store.connection_limit(id).unwrap(), 32);
        assert_eq!(after.endpoint.desired_state, DesiredState::Running);
    }
    #[test]
    fn compute_idle_changes_do_not_restart_and_suspended_resize_does_not_wake() {
        let (_root, mut store, project, id) = fixture();
        let lease = store.accept_connection(id).unwrap();
        let op = configure_idle(&mut store, project, id, Some(60));
        assert!(op.steps.is_empty());
        assert_eq!(store.branch(id).unwrap().revision, 1);
        assert_eq!(store.database_record(project, id).unwrap().revision, 2);
        store.release_connection(lease).unwrap();
        let op = store
            .submit(
                project,
                "suspend",
                Mutation::SetState {
                    branch_id: id,
                    expected_revision: 1,
                    desired: DesiredState::Suspended,
                },
            )
            .unwrap();
        finish(&mut store, &op);
        let op = store
            .submit(
                project,
                "resize",
                Mutation::ConfigureCompute {
                    branch_id: id,
                    expected_revision: 3,
                    settings: Settings {
                        size: "s".into(),
                        idle_timeout_seconds: Some(60),
                    },
                    allow_restart: false,
                    api_request: None,
                },
            )
            .unwrap();
        assert!(op.steps.is_empty());
        assert_eq!(
            store.branch(id).unwrap().endpoint.desired_state,
            DesiredState::Suspended
        );
        assert_eq!(
            store.compute_view(id).unwrap()["last_suspend_reason"],
            "manual"
        );
        assert_eq!(store.database_record(project, id).unwrap().revision, 4);
    }
    #[test]
    fn compute_gateway_limit_matches_selected_profile() {
        let (_root, mut store, project, id) = fixture();
        let op = store
            .submit(
                project,
                "resize",
                Mutation::ConfigureCompute {
                    branch_id: id,
                    expected_revision: 1,
                    settings: Settings {
                        size: "s".into(),
                        idle_timeout_seconds: None,
                    },
                    allow_restart: true,
                    api_request: None,
                },
            )
            .unwrap();
        finish(&mut store, &op);
        let leases = (0..32)
            .map(|_| store.accept_connection(id).unwrap())
            .collect::<Vec<_>>();
        assert!(store.accept_connection(id).is_err());
        store.release_connection(leases[0]).unwrap();
        assert!(store.accept_connection(id).is_ok());
    }
    #[test]
    fn compute_capture_and_work_leases_hold_awake() {
        let (_root, mut store, project, id) = fixture();
        configure_idle(&mut store, project, id, Some(60));
        let lease = store
            .acquire_lease(id, None, "test", std::time::Duration::from_secs(60))
            .unwrap();
        assert!(!store.compute_idle(id).unwrap());
        store.release_lease(&lease).unwrap();
        store
            .db
            .execute(
                "INSERT INTO sync_policies VALUES ('p',?1,?2,'active','{}')",
                params![project.to_string(), id.to_string()],
            )
            .unwrap();
        store
            .db
            .execute(
                "INSERT INTO sync_captures VALUES ('c','p',?1,?2,'paused',NULL,'{}')",
                params![project.to_string(), id.to_string()],
            )
            .unwrap();
        assert!(!store.compute_idle(id).unwrap());
        assert!(
            store
                .submit(
                    project,
                    "idle",
                    Mutation::AutoSuspend {
                        branch_id: id,
                        expected_revision: 1
                    }
                )
                .is_err()
        );
        assert_eq!(store.compute_view(id).unwrap()["capture_holds_awake"], true);
        store
            .db
            .execute("UPDATE sync_captures SET state='deleted'", [])
            .unwrap();
        assert!(store.compute_idle(id).unwrap());
    }
    #[test]
    fn compute_storage_counts_exclusive_layers_once_and_marks_missing_or_old_samples() {
        let (_root, mut store, project, id) = fixture();
        let child = store
            .submit(
                project,
                "child",
                Mutation::BranchFrom {
                    name: "child".into(),
                    parent_id: id,
                    ports: Ports {
                        sql: 5500,
                        external_http: 5501,
                        internal_http: 5502,
                    },
                    point: Default::default(),
                    timeout_ms: 90000,
                },
            )
            .unwrap()
            .branch_id;
        assert_eq!(
            store.database_storage(&[id, child]).unwrap()["total_physical_bytes"],
            Value::Null
        );
        for (branch, physical) in [(id, 100), (child, 20)] {
            store
                .save_storage_sample(Sample {
                    branch,
                    sampled_at_ms: now_ms().unwrap(),
                    logical: Some(1000),
                    physical: Some(physical),
                    accurate: true,
                    available: true,
                })
                .unwrap();
        }
        assert_eq!(
            store.database_storage(&[id, child]).unwrap()["total_physical_bytes"],
            120
        );
        assert_eq!(store.storage_view(child).unwrap()["logical_bytes"], 1000);
        let path = store.root().to_owned();
        drop(store);
        let store = Store::open(&path).unwrap();
        assert_eq!(store.storage_view(id).unwrap()["status"], "stale");
        assert_eq!(
            store.database_storage(&[id, child]).unwrap()["status"],
            "stale"
        );
        store
            .save_storage_sample(Sample {
                branch: child,
                sampled_at_ms: now_ms().unwrap(),
                logical: None,
                physical: None,
                accurate: false,
                available: false,
            })
            .unwrap();
        assert_eq!(
            store.database_storage(&[id, child]).unwrap()["total_physical_bytes"],
            Value::Null
        );
        assert_eq!(
            store.database_storage(&[id, child]).unwrap()["status"],
            "unavailable"
        );
    }
    #[test]
    fn compute_creation_is_atomic_and_rejects_invalid_profiles_without_reserving_ports() {
        let (_root, mut store, project, _) = fixture();
        let create = Mutation::CreateDatabase {
            name: "second".into(),
            ports: Ports {
                sql: 5500,
                external_http: 5501,
                internal_http: 5502,
            },
        };
        let mut request = Mutation::WithCompute {
            mutation: Box::new(create),
            settings: Settings {
                size: "invalid".into(),
                idle_timeout_seconds: Some(60),
            },
        };
        assert!(store.submit(project, "new", request.clone()).is_err());
        assert_eq!(store.list_branches(project, true).unwrap().len(), 1);
        if let Mutation::WithCompute { settings, .. } = &mut request {
            settings.size = "s".into();
        }
        let op = store.submit(project, "new", request.clone()).unwrap();
        assert_eq!(
            store.compute_settings(op.branch_id).unwrap(),
            Settings {
                size: "s".into(),
                idle_timeout_seconds: Some(60)
            }
        );
        assert_eq!(
            store.submit(project, "new", request.clone()).unwrap().id,
            op.id
        );
        if let Mutation::WithCompute { settings, .. } = &mut request {
            settings.idle_timeout_seconds = None;
        }
        assert!(store.submit(project, "new", request).is_err());
    }
}
