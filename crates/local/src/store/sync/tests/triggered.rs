use super::incremental::{finish, prepare};
use super::*;
fn now() -> i64 {
    super::super::super::now_ms().unwrap()
}
fn policy(s: &mut Store, p: ProjectId, d: DeploymentId, b: BranchId) -> Policy {
    serde_json::from_value(
        s.sync_command(
            p,
            d,
            Command::Create {
                branch: b.to_string(),
                key: "triggered".into(),
                config: Config {
                    mode: "triggered".into(),
                    strategy: "incremental".into(),
                    ..Default::default()
                },
            },
            now(),
        )
        .unwrap(),
    )
    .unwrap()
}
fn trigger(s: &mut Store, p: &Policy, key: &str) -> Run {
    serde_json::from_value(
        s.sync_command(
            p.project_id,
            p.deployment_id,
            Command::RunNow {
                id: p.id,
                expected_revision: p.revision,
                key: key.into(),
            },
            now(),
        )
        .unwrap(),
    )
    .unwrap()
}
fn captured(s: &Store, p: &Policy, r: &Run) -> crate::capture::Capture {
    let mut c = s.capture(p.project_id, r.capture_id.unwrap()).unwrap();
    c.bootstrap_lsn = Some("0/C8".into());
    c.captured_lsn = Some("0/200".into());
    c.observed_at_ms = Some(now());
    c.state = "capturing".into();
    c.barrier = Some(json!({"run_id":r.id,"end_lsn":"0/12C"}));
    s.save_capture(&c).unwrap();
    c
}
#[test]
fn target_survives_recovery_and_partial_batches_without_chasing_source() {
    let (dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let run = trigger(&mut s, &policy, "run");
    let mut c = captured(&s, &policy, &run);
    assert_eq!(trigger(&mut s, &policy, "run").id, run.id);
    assert!(
        s.sync_command(
            p,
            d,
            Command::RunNow {
                id: policy.id,
                expected_revision: 1,
                key: "overlap".into()
            },
            now()
        )
        .is_err()
    );
    assert!(
        s.incremental_command(
            p,
            crate::incremental::Command::Apply {
                capture_id: c.id,
                key: "bypass".into()
            }
        )
        .is_err()
    );
    for (version, end) in [(0, "0/C8"), (1, "0/FA"), (2, "0/12C")] {
        s.tick_triggered(now()).unwrap();
        let r = s.sync_run(p, run.id).unwrap();
        assert_eq!(r.target_lsn.as_deref(), Some("0/12C"));
        let mut a = s.incremental_run(p, r.apply_id.unwrap()).unwrap();
        assert_eq!(a.target_lsn, if version == 0 { "0/C8" } else { "0/12C" });
        // Model a complete input-budget prefix shorter than the admitted target.
        a.target_lsn = end.into();
        prepare(&mut s, &c, &mut a, version);
        finish(&mut s, &a);
        if version == 1 {
            drop(s);
            s = Store::open(dir.path()).unwrap();
            c.captured_lsn = Some("0/300".into());
            c.observed_at_ms = Some(now());
            s.save_capture(&c).unwrap();
        }
        s.reconcile_sync(now()).unwrap();
    }
    let r = s.sync_run(p, run.id).unwrap();
    assert_eq!(r.state, "succeeded");
    assert_eq!(r.source_lsn, r.target_lsn);
    assert_eq!(r.batches, 3);
    assert!(
        s.sync_command(
            p,
            d,
            Command::Cancel {
                id: r.id,
                key: "late-cancel".into()
            },
            now()
        )
        .is_err()
    );
    assert!(s.active_incremental().unwrap().is_empty());
    assert_eq!(
        s.sync_policy(p, policy.id).unwrap().last_epoch_id,
        r.epoch_id
    );
}
#[test]
fn policy_pause_resume_schedule_and_cancel_fence_only_owned_work() {
    let (_dir, mut s, p, d, b) = setup();
    let mut policy = policy(&mut s, p, d, b);
    let run = trigger(&mut s, &policy, "run");
    let c = captured(&s, &policy, &run);
    s.tick_triggered(now()).unwrap();
    let r = s.sync_run(p, run.id).unwrap();
    let a = s.incremental_run(p, r.apply_id.unwrap()).unwrap();
    s.sync_command(
        p,
        d,
        Command::Cancel {
            id: r.id,
            key: "cancel".into(),
        },
        now(),
    )
    .unwrap();
    assert!(s.incremental_live(&a).is_err());
    assert_eq!(s.capture(p, c.id).unwrap().desired, "fenced");
    // A fresh fixture checks benign policy revisions without a partial writer.
    let (_dir, mut s, p, d, b) = setup();
    policy = self::policy(&mut s, p, d, b);
    let original = s.captures().unwrap()[0].clone();
    for (key, command) in [("pause", true), ("resume", false)] {
        let cmd = if command {
            Command::Pause {
                id: policy.id,
                expected_revision: policy.revision,
                key: key.into(),
            }
        } else {
            Command::Resume {
                id: policy.id,
                expected_revision: policy.revision,
                key: key.into(),
            }
        };
        policy = serde_json::from_value(s.sync_command(p, d, cmd, now()).unwrap()).unwrap();
        assert!(s.capture_live(&original).is_ok());
    }
    let mut config = policy.config.clone();
    config.schedule = Some(Schedule {
        interval_seconds: 60,
        timezone: "UTC".into(),
        missed_run: "coalesce".into(),
    });
    policy = serde_json::from_value(
        s.sync_command(
            p,
            d,
            Command::Update {
                id: policy.id,
                expected_revision: policy.revision,
                key: "schedule".into(),
                config,
            },
            now(),
        )
        .unwrap(),
    )
    .unwrap();
    s.schedule_sync(now() + 300001).unwrap();
    s.schedule_sync(now() + 360001).unwrap();
    assert_eq!(s.active_sync_runs().unwrap().len(), 1);
    assert!(s.capture_live(&original).is_ok());
    s.sync_command(
        p,
        d,
        Command::Delete {
            id: policy.id,
            expected_revision: policy.revision,
            key: "delete".into(),
        },
        now(),
    )
    .unwrap();
    assert_eq!(s.capture(p, original.id).unwrap().desired, "deleted");
}
#[test]
fn restored_final_publication_wins_over_copied_unfinished_intent() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let run = trigger(&mut s, &policy, "run");
    let c = captured(&s, &policy, &run);
    for version in 0..2 {
        s.tick_triggered(now()).unwrap();
        let r = s.sync_run(p, run.id).unwrap();
        let mut a = s.incremental_run(p, r.apply_id.unwrap()).unwrap();
        prepare(&mut s, &c, &mut a, version);
        finish(&mut s, &a);
        if version == 0 {
            s.reconcile_sync(now()).unwrap();
        }
    }
    assert_eq!(s.sync_run(p, run.id).unwrap().state, "running");
    super::super::restore(&s.db, now()).unwrap();
    assert_eq!(s.sync_run(p, run.id).unwrap().state, "succeeded");
    assert_eq!(s.sync_policy(p, policy.id).unwrap().state, "paused");
}

#[test]
fn queued_deadline_and_foreign_project_requests_cannot_start_apply() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    assert!(policy.capture_id.is_some());
    let r = trigger(&mut s, &policy, "queued");
    assert_eq!(r.capture_id, policy.capture_id);
    assert!(s.sync_run(ProjectId::new(), r.id).is_err());
    s.reconcile_sync(r.deadline_ms.unwrap() + 1).unwrap();
    assert_eq!(s.sync_run(p, r.id).unwrap().state, "failed");
    assert!(s.active_incremental().unwrap().is_empty());
    assert!(
        s.triggered_barrier_request(r.capture_id.unwrap())
            .unwrap()
            .is_none()
    );
}

#[test]
fn queued_run_cannot_adopt_a_replacement_capture_generation() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let r = trigger(&mut s, &policy, "queued");
    let mut old = s.capture(p, r.capture_id.unwrap()).unwrap();
    old.state = "deleted".into();
    old.desired = "deleted".into();
    s.save_capture(&old).unwrap();
    let replacement: crate::capture::Capture = serde_json::from_value(
        s.capture_command(
            p,
            crate::capture::Command::Start {
                policy_id: policy.id,
                expected_revision: policy.revision,
                key: "replacement".into(),
                limits: Default::default(),
            },
        )
        .unwrap(),
    )
    .unwrap();
    assert_ne!(replacement.id, old.id);
    assert!(s.tick_triggered(now()).is_err());
    s.reconcile_sync(now()).unwrap();
    assert_eq!(s.sync_run(p, r.id).unwrap().state, "failed");
    assert!(s.active_incremental().unwrap().is_empty());
    assert_eq!(s.capture(p, replacement.id).unwrap().desired, "running");
}

#[test]
fn deferred_read_pause_stops_at_safe_boundary_and_exhaustion_is_explicit() {
    for pause in [true, false] {
        let (_dir, mut s, p, d, b) = setup();
        let policy = super::continuous::policy(&mut s, p, d, b);
        let c = super::continuous::captured(&s, &policy);
        s.schedule_continuous(now()).unwrap();
        let parent = s.active_sync_runs().unwrap().remove(0);
        s.tick_triggered(now()).unwrap();
        let mut r = s
            .incremental_run(p, s.sync_run(p, parent.id).unwrap().apply_id.unwrap())
            .unwrap();
        prepare(&mut s, &c, &mut r, 0);
        finish(&mut s, &r);
        s.reconcile_sync(now()).unwrap();
        s.tick_triggered(now()).unwrap();
        let mut r = s
            .incremental_run(p, s.sync_run(p, parent.id).unwrap().apply_id.unwrap())
            .unwrap();
        let receipt =
            super::incremental::deferred_receipt(&s, &c, &mut r, if pause { 1 } else { 3 });
        s.defer_incremental(&mut r, &receipt, now()).unwrap();
        if pause {
            s.sync_command(
                p,
                d,
                Command::Pause {
                    id: policy.id,
                    expected_revision: policy.revision,
                    key: "pause".into(),
                },
                now(),
            )
            .unwrap();
            assert!(s.pause_deferred_incremental(&mut r).unwrap());
        }
        s.reconcile_sync(now()).unwrap();
        let parent = s.sync_run(p, parent.id).unwrap();
        assert_eq!(parent.state, if pause { "cancelled" } else { "failed" });
        assert_eq!(s.capture(p, c.id).unwrap().state, "capturing");
        assert!(s.active_incremental().unwrap().is_empty());
        if pause {
            assert_eq!(s.sync_policy(p, policy.id).unwrap().state, "paused");
        } else {
            assert_eq!(parent.error.as_deref(), Some("journal_read_busy_exhausted"));
            s.schedule_continuous(now() + 1000).unwrap();
            assert!(s.active_sync_runs().unwrap().is_empty());
            let current = s.sync_policy(p, policy.id).unwrap();
            s.sync_command(
                p,
                d,
                Command::Resume {
                    id: policy.id,
                    expected_revision: current.revision,
                    key: "resume-busy".into(),
                },
                now(),
            )
            .unwrap();
            s.schedule_continuous(now() + 1000).unwrap();
            let resumed = s.active_sync_runs().unwrap();
            assert_eq!(resumed.len(), 1);
            assert_eq!(resumed[0].capture_id, Some(c.id));
            assert_eq!(s.capture(p, c.id).unwrap().bootstrap_id, c.bootstrap_id);
        }
    }
}

#[test]
fn deferred_read_cannot_resume_after_cancel_or_authority_revocation() {
    for cancel in [true, false] {
        let (_dir, mut s, p, d, b) = setup();
        let policy = policy(&mut s, p, d, b);
        let parent = trigger(&mut s, &policy, "run");
        let c = captured(&s, &policy, &parent);
        s.tick_triggered(now()).unwrap();
        let mut r = s
            .incremental_run(p, s.sync_run(p, parent.id).unwrap().apply_id.unwrap())
            .unwrap();
        let receipt = super::incremental::deferred_receipt(&s, &c, &mut r, 1);
        s.defer_incremental(&mut r, &receipt, now()).unwrap();
        if cancel {
            s.sync_command(
                p,
                d,
                Command::Cancel {
                    id: parent.id,
                    key: "cancel".into(),
                },
                now(),
            )
            .unwrap();
        } else {
            // The managed policy's authority is revoked before the next dispatch.
            s.db.execute(
                "INSERT INTO governed_branches VALUES (?1,'ready')",
                [b.to_string()],
            )
            .unwrap();
        }
        assert!(s.incremental_live(&r).is_err());
        assert!(s.defer_incremental(&mut r, &receipt, now()).is_err());
        assert!(s.snapshot(p, r.epoch_id).is_err());
    }
}
