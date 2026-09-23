use super::*;
use crate::{
    operations::{Mutation, Ports},
    project::ProjectConfig,
    sync::{Config, Schedule},
};
use supabricks_core::resource::BranchId;
fn setup() -> (tempfile::TempDir, Store, ProjectId, DeploymentId, BranchId) {
    let dir = tempfile::tempdir().unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let mut s = Store::open(dir.path()).unwrap();
    let p = ProjectConfig {
        format_version: 1,
        id: ProjectId::new(),
        name: "sync".into(),
    };
    s.register_project(&p).unwrap();
    let b = s
        .submit(
            p.id,
            "main",
            Mutation::CreateBranch {
                name: "main".into(),
                parent_id: None,
                ports: Ports {
                    sql: 5400,
                    external_http: 5401,
                    internal_http: 5402,
                },
            },
        )
        .unwrap();
    while let Some(t) = s.ticket(b.id).unwrap() {
        s.checkpoint(&t, json!({})).unwrap();
    }
    let d: String =
        s.db.query_row(
            "SELECT id FROM deployments WHERE runtime_project_id=?1",
            [p.id.to_string()],
            |r| r.get(0),
        )
        .unwrap();
    (dir, s, p.id, super::super::parse(&d).unwrap(), b.branch_id)
}
fn config() -> Config {
    Config {
        schedule: Some(Schedule {
            interval_seconds: 60,
            timezone: "UTC".into(),
            missed_run: "coalesce".into(),
        }),
        ..Default::default()
    }
}
fn create(s: &mut Store, p: ProjectId, d: DeploymentId, b: BranchId) -> Policy {
    serde_json::from_value(
        s.sync_command(
            p,
            d,
            Command::Create {
                branch: b.to_string(),
                key: "create".into(),
                config: config(),
            },
            0,
        )
        .unwrap(),
    )
    .unwrap()
}
fn run(s: &mut Store, p: &Policy, key: &str) -> Run {
    serde_json::from_value(
        s.sync_command(
            p.project_id,
            p.deployment_id,
            Command::RunNow {
                id: p.id,
                expected_revision: p.revision,
                key: key.into(),
            },
            1,
        )
        .unwrap(),
    )
    .unwrap()
}
fn export(s: &mut Store, r: &Run) -> OperationId {
    s.start_sync_run(r.id).unwrap();
    s.submit(
        r.project_id,
        &format!("internal:sync:{}", r.id),
        Mutation::Export {
            parent_id: r.branch_id,
            ports: Ports {
                sql: 5500,
                external_http: 5501,
                internal_http: 5502,
            },
            limits: Default::default(),
        },
    )
    .unwrap()
    .id
}
#[test]
fn retries_are_durable_and_payload_conflicts_do_not_mutate() {
    let (dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    let r = run(&mut s, &policy, "run");
    assert_eq!(r.id, run(&mut s, &policy, "run").id);
    assert!(
        s.sync_command(
            p,
            d,
            Command::RunNow {
                id: policy.id,
                expected_revision: 2,
                key: "run".into()
            },
            2
        )
        .is_err()
    );
    assert!(
        s.sync_command(
            p,
            d,
            Command::RunNow {
                id: policy.id,
                expected_revision: 1,
                key: "overlap".into()
            },
            2
        )
        .is_err()
    );
    assert!(
        s.sync_command(
            p,
            d,
            Command::Pause {
                id: policy.id,
                expected_revision: 9,
                key: "pause".into()
            },
            2
        )
        .is_err()
    );
    drop(s);
    let mut s = Store::open(dir.path()).unwrap();
    assert_eq!(run(&mut s, &policy, "run").id, r.id);
    assert_eq!(create(&mut s, p, d, b).id, policy.id);
}
#[test]
fn missed_intervals_coalesce_and_preserve_phase_without_backlog() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    s.schedule_sync(59_999).unwrap();
    assert!(s.active_sync_runs().unwrap().is_empty());
    s.schedule_sync(86_400_123).unwrap();
    let rs = s.active_sync_runs().unwrap();
    assert_eq!(rs.len(), 1);
    assert_eq!(rs[0].scheduled_for_ms, Some(60_000));
    assert_eq!(
        s.sync_policy(p, policy.id).unwrap().next_due_at_ms,
        Some(86_460_000)
    );
    s.schedule_sync(86_520_050).unwrap();
    assert_eq!(s.active_sync_runs().unwrap().len(), 1);
    assert_eq!(
        s.sync_policy(p, policy.id).unwrap().next_due_at_ms,
        Some(86_580_000)
    );
    // Clock rollback does not produce early or duplicate work.
    s.schedule_sync(60_000).unwrap();
    assert_eq!(s.active_sync_runs().unwrap().len(), 1);
}
#[test]
fn pause_resume_revision_and_delete_fence_queued_work() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    let r = run(&mut s, &policy, "run");
    let paused = s
        .sync_command(
            p,
            d,
            Command::Pause {
                id: policy.id,
                expected_revision: 1,
                key: "pause".into(),
            },
            5,
        )
        .unwrap();
    assert_eq!(paused["revision"], 2);
    assert!(paused["next_due_at_ms"].is_null());
    assert_eq!(s.sync_run(p, r.id).unwrap().state, "cancelled");
    s.schedule_sync(999_999).unwrap();
    assert!(s.active_sync_runs().unwrap().is_empty());
    let resumed = s
        .sync_command(
            p,
            d,
            Command::Resume {
                id: policy.id,
                expected_revision: 2,
                key: "resume".into(),
            },
            100_000,
        )
        .unwrap();
    assert_eq!(resumed["next_due_at_ms"], 160_000);
    s.sync_command(
        p,
        d,
        Command::Delete {
            id: policy.id,
            expected_revision: 3,
            key: "delete".into(),
        },
        100_001,
    )
    .unwrap();
    assert!(
        s.sync_command(
            p,
            d,
            Command::Resume {
                id: policy.id,
                expected_revision: 4,
                key: "again".into()
            },
            100_002
        )
        .is_err()
    );
}
#[test]
fn configuration_rejects_incrementality_events_and_ambiguous_calendars() {
    let (_dir, mut s, p, d, b) = setup();
    for bad in [
        Config {
            mode: "triggered".into(),
            ..config()
        },
        Config {
            mode: "continuous".into(),
            ..config()
        },
        Config {
            strategy: "incremental".into(),
            ..config()
        },
        Config {
            schedule: Some(Schedule {
                timezone: "America/Chicago".into(),
                ..config().schedule.unwrap()
            }),
            ..config()
        },
        Config {
            schedule: Some(Schedule {
                interval_seconds: 0,
                ..config().schedule.unwrap()
            }),
            ..config()
        },
    ] {
        assert!(
            s.sync_command(
                p,
                d,
                Command::Create {
                    branch: b.to_string(),
                    key: "bad".into(),
                    config: bad
                },
                0
            )
            .is_err()
        );
    }
    assert!(s.sync_policies().unwrap().is_empty());
    assert!(
        serde_json::from_value::<Config>(
            json!({"mode":"snapshot","strategy":"full","schedule":null,"events":true})
        )
        .is_err()
    );
}
#[test]
fn installation_timeline_and_governed_authority_changes_block_whole_group() {
    for change in [
        "UPDATE analytics_installation SET id='different'",
        "UPDATE branches SET timeline_id='11111111111111111111111111111111'",
        "INSERT INTO governed_branches SELECT id,'ready' FROM branches",
    ] {
        let (_dir, mut s, p, d, b) = setup();
        let policy = create(&mut s, p, d, b);
        let r = run(&mut s, &policy, "run");
        s.db.execute_batch(change).unwrap();
        s.reconcile_sync(20).unwrap();
        assert_eq!(s.sync_policy(p, policy.id).unwrap().state, "blocked");
        assert_eq!(s.sync_run(p, r.id).unwrap().state, "cancelled");
        assert!(
            s.sync_command(
                p,
                d,
                Command::Resume {
                    id: policy.id,
                    expected_revision: 2,
                    key: "resume".into()
                },
                21
            )
            .is_err()
        );
    }
}
#[test]
fn governed_sources_and_foreign_projects_cannot_enroll_or_inspect() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    let other = ProjectId::new();
    assert!(s.sync_policy(other, policy.id).is_err());
    assert!(s.sync_command(other, d, Command::List, 0).is_err());
    s.db.execute(
        "INSERT INTO governed_branches VALUES (?1,'ready')",
        [b.to_string()],
    )
    .unwrap();
    assert!(
        s.sync_command(
            p,
            d,
            Command::Create {
                branch: b.to_string(),
                key: "governed".into(),
                config: config()
            },
            0
        )
        .is_err()
    );
}
#[test]
fn crash_after_export_admission_repairs_link_before_cancel_without_duplicate() {
    let (dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    let r = run(&mut s, &policy, "run");
    let id = export(&mut s, &r);
    assert!(s.sync_run(p, r.id).unwrap().refresh_id.is_none());
    drop(s);
    let mut s = Store::open(dir.path()).unwrap();
    s.sync_command(
        p,
        d,
        Command::Cancel {
            id: r.id,
            key: "cancel".into(),
        },
        10,
    )
    .unwrap();
    let recovered = s.sync_run(p, r.id).unwrap();
    assert_eq!(recovered.refresh_id, Some(id));
    assert_eq!(recovered.state, "cancelled");
    assert!(s.export(id).unwrap().cancel_requested);
    assert_eq!(s.refresh_status(p, id).unwrap()["state"], "cancelled");
    assert!(s.sync_publication_live(id).is_err());
    assert_eq!(
        s.db.query_row("SELECT count(*) FROM exports", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
}
#[test]
fn revised_policy_and_schema_fences_prevent_stale_publication() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    let r = run(&mut s, &policy, "run");
    let id = export(&mut s, &r);
    s.link_sync_refresh(r.id, id).unwrap();
    assert!(s.sync_publication_live(id).is_ok());
    s.sync_command(
        p,
        d,
        Command::Update {
            id: policy.id,
            expected_revision: 1,
            key: "update".into(),
            config: Config::default(),
        },
        2,
    )
    .unwrap();
    assert!(s.sync_publication_live(id).is_err());
    assert!(
        s.sync_policy(p, policy.id)
            .unwrap()
            .config
            .schedule
            .is_none()
    );
}
#[test]
fn cancellation_fences_staged_publication_and_retains_existing_head() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    let r = run(&mut s, &policy, "run");
    let id = export(&mut s, &r);
    s.link_sync_refresh(r.id, id).unwrap();
    s.db.execute(
        "UPDATE exports SET state='complete' WHERE id=?1",
        [id.to_string()],
    )
    .unwrap();
    s.db.execute("INSERT INTO publications(export_id,epoch_id,branch_id,source_revision,export_order,requested_at_ms,state) VALUES (?1,?2,?3,1,1,0,'files_complete')",params![id.to_string(),supabricks_core::resource::EpochId::new().to_string(),b.to_string()]).unwrap();
    // A cancellation changes no epochs or head; the native test supplies real prior snapshots.
    s.sync_command(
        p,
        d,
        Command::Cancel {
            id: r.id,
            key: "cancel".into(),
        },
        10,
    )
    .unwrap();
    assert_eq!(s.publication(id).unwrap().state, "cancelled");
    assert!(s.commit_publication(&s.publication(id).unwrap()).is_err());
    assert!(
        s.db.prepare("SELECT 1 FROM analytics_gc WHERE export_id=?1")
            .unwrap()
            .exists([id.to_string()])
            .unwrap()
    );
}
#[test]
fn success_is_reconciled_before_cancel_and_is_durable() {
    let (dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    let r = run(&mut s, &policy, "run");
    let id = export(&mut s, &r);
    s.link_sync_refresh(r.id, id).unwrap();
    s.db.execute("INSERT INTO publications(export_id,epoch_id,branch_id,source_revision,export_order,requested_at_ms,published_at_ms,state,descriptor) VALUES (?1,?2,?3,1,1,0,42,'published',?4)",params![id.to_string(),supabricks_core::resource::EpochId::new().to_string(),b.to_string(),json!({"manifest":{"source":{"lsn":"0/1234"}}}).to_string()]).unwrap();
    assert!(
        s.sync_command(
            p,
            d,
            Command::Cancel {
                id: r.id,
                key: "too-late".into()
            },
            45
        )
        .is_err()
    );
    let done = s.sync_run(p, r.id).unwrap();
    assert_eq!(done.state, "succeeded");
    assert_eq!(s.publish_export(p, id).unwrap().state, "published");
    assert_eq!(done.source_lsn.as_deref(), Some("0/1234"));
    assert_eq!(
        s.sync_policy(p, policy.id).unwrap().last_success_at_ms,
        Some(42)
    );
    // Restore also retains an epoch committed just before run bookkeeping.
    let mut gap = done.clone();
    gap.state = "running".into();
    gap.finished_at_ms = None;
    s.save_sync_run(&gap).unwrap();
    super::restore(&s.db, 99).unwrap();
    assert_eq!(s.sync_run(p, r.id).unwrap().state, "succeeded");
    assert_eq!(
        s.sync_policy(p, policy.id).unwrap().last_success_at_ms,
        Some(42)
    );
    drop(s);
    let mut s = Store::open(dir.path()).unwrap();
    s.reconcile_sync(100).unwrap();
    assert_eq!(s.sync_run(p, r.id).unwrap().epoch_id, done.epoch_id);
}

#[test]
fn restore_pauses_policy_and_fences_unlinked_export_before_worker_start() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    let r = run(&mut s, &policy, "run");
    let id = export(&mut s, &r);
    super::restore(&s.db, 100).unwrap();
    assert_eq!(s.sync_policy(p, policy.id).unwrap().state, "paused");
    assert_eq!(s.sync_policy(p, policy.id).unwrap().revision, 2);
    let r = s.sync_run(p, r.id).unwrap();
    assert_eq!(r.state, "cancelled");
    assert_eq!(r.refresh_id, Some(id));
    assert_eq!(r.finished_at_ms, Some(100));
    assert!(s.export(id).unwrap().cancel_requested);
    assert!(s.sync_publication_live(id).is_err());
    assert_eq!(s.refresh_status(p, id).unwrap()["state"], "cancelled");
    s.schedule_sync(99999999).unwrap();
    assert!(s.active_sync_runs().unwrap().is_empty());
}

#[test]
fn failed_receipt_write_rolls_back_policy_revision_and_cancellation_together() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    let r = run(&mut s, &policy, "run");
    let id = export(&mut s, &r);
    s.link_sync_refresh(r.id, id).unwrap();
    s.db.execute_batch("CREATE TEMP TRIGGER reject_sync_receipt BEFORE INSERT ON sync_requests BEGIN SELECT RAISE(FAIL,'simulated receipt write failure'); END;").unwrap();
    assert!(
        s.sync_command(
            p,
            d,
            Command::Pause {
                id: policy.id,
                expected_revision: 1,
                key: "pause".into()
            },
            10
        )
        .is_err()
    );
    assert_eq!(s.sync_policy(p, policy.id).unwrap().revision, 1);
    assert_eq!(s.sync_policy(p, policy.id).unwrap().state, "active");
    assert_eq!(s.sync_run(p, r.id).unwrap().state, "running");
    assert!(!s.export(id).unwrap().cancel_requested);
    assert!(s.sync_publication_live(id).is_ok());
    s.db.execute_batch("DROP TRIGGER reject_sync_receipt;")
        .unwrap();
    s.sync_command(
        p,
        d,
        Command::Pause {
            id: policy.id,
            expected_revision: 1,
            key: "pause".into(),
        },
        11,
    )
    .unwrap();
    assert_eq!(s.sync_run(p, r.id).unwrap().state, "cancelled");
}

fn capture_policy(s: &mut Store, p: ProjectId, d: DeploymentId, b: BranchId) -> Policy {
    serde_json::from_value(
        s.sync_command(
            p,
            d,
            Command::Create {
                branch: b.to_string(),
                key: "capture-policy".into(),
                config: Config::default(),
            },
            0,
        )
        .unwrap(),
    )
    .unwrap()
}
fn capture_start(s: &mut Store, p: &Policy, key: &str) -> crate::capture::Capture {
    serde_json::from_value(
        s.capture_command(
            p.project_id,
            crate::capture::Command::Start {
                policy_id: p.id,
                expected_revision: p.revision,
                key: key.into(),
                limits: Default::default(),
            },
        )
        .unwrap(),
    )
    .unwrap()
}
#[test]
fn capture_admission_is_idempotent_scoped_and_globally_bounded() {
    use crate::capture::Command as C;
    let (_dir, mut s, p, d, b) = setup();
    let policy = capture_policy(&mut s, p, d, b);
    let c = capture_start(&mut s, &policy, "start");
    assert_eq!(c.id, capture_start(&mut s, &policy, "start").id);
    assert!(
        s.capture_command(
            p,
            C::Start {
                policy_id: policy.id,
                expected_revision: 1,
                key: "another".into(),
                limits: Default::default()
            }
        )
        .is_err()
    );
    assert!(
        s.capture_command(ProjectId::new(), C::Status { id: c.id })
            .is_err()
    );
    assert!(
        s.capture_command(
            p,
            C::Pause {
                id: c.id,
                key: "start".into()
            }
        )
        .is_err()
    );
    s.capture_command(
        p,
        C::Pause {
            id: c.id,
            key: "pause".into(),
        },
    )
    .unwrap();
    s.recover_captures().unwrap();
    assert_eq!(s.capture(p, c.id).unwrap().desired, "paused");
    let mut c = s.capture(p, c.id).unwrap();
    c.state = "resync_required".into();
    c.desired = "fenced".into();
    s.save_capture(&c).unwrap();
    assert!(
        s.capture_command(
            p,
            C::Resume {
                id: c.id,
                key: "resume".into()
            }
        )
        .is_err()
    );
    s.capture_command(
        p,
        C::Delete {
            id: c.id,
            key: "delete".into(),
        },
    )
    .unwrap();
    assert_eq!(s.capture(p, c.id).unwrap().state, "deleting");
    assert!(
        s.capture_command(
            p,
            C::Start {
                policy_id: policy.id,
                expected_revision: 1,
                key: "blocked".into(),
                limits: Default::default()
            }
        )
        .is_err()
    );
}
#[test]
fn capture_policy_revision_fences_and_stale_status_does_not_claim_health() {
    use crate::capture::Command as C;
    let (_dir, mut s, p, d, b) = setup();
    let policy = capture_policy(&mut s, p, d, b);
    let mut c = capture_start(&mut s, &policy, "start");
    c.state = "capturing".into();
    c.observed_at_ms = Some(0);
    s.save_capture(&c).unwrap();
    assert_eq!(
        s.capture_command(p, C::Status { id: c.id }).unwrap()["state"],
        "unavailable"
    );
    s.sync_command(
        p,
        d,
        Command::Pause {
            id: policy.id,
            expected_revision: 1,
            key: "pause-policy".into(),
        },
        1,
    )
    .unwrap();
    assert!(s.capture_live(&c).is_err());
    assert!(
        s.capture_command(
            p,
            C::Resume {
                id: c.id,
                key: "resume".into()
            }
        )
        .is_err()
    );
}
#[test]
fn scheduled_policy_cannot_accidentally_enable_capture() {
    use crate::capture::Command as C;
    let (_dir, mut s, p, d, b) = setup();
    let policy = create(&mut s, p, d, b);
    assert!(
        s.capture_command(
            p,
            C::Start {
                policy_id: policy.id,
                expected_revision: 1,
                key: "no".into(),
                limits: Default::default()
            }
        )
        .is_err()
    );
    assert!(s.captures().unwrap().is_empty());
}

#[test]
fn capture_restore_never_executes_copied_capture_intent() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = capture_policy(&mut s, p, d, b);
    let c = capture_start(&mut s, &policy, "start");
    super::super::capture::restore(&s.db).unwrap();
    s.recover_captures().unwrap();
    let restored = s.capture(p, c.id).unwrap();
    assert_eq!(restored.state, "resync_required");
    assert_eq!(restored.desired, "fenced");
    assert!(restored.cleanup_complete);
    assert_eq!(restored.error.as_deref(), Some("restored_requires_resync"));
}

#[test]
fn capture_lineage_installation_and_governance_changes_fence_the_generation() {
    for change in [
        "UPDATE analytics_installation SET id='different'",
        "UPDATE branches SET timeline_id='11111111111111111111111111111111'",
        "INSERT INTO governed_branches SELECT id,'ready' FROM branches",
    ] {
        let (_dir, mut s, p, d, b) = setup();
        let policy = capture_policy(&mut s, p, d, b);
        let c = capture_start(&mut s, &policy, "start");
        s.db.execute_batch(change).unwrap();
        assert!(s.capture_live(&c).is_err());
    }
}

mod incremental;

mod triggered;

mod continuous;
