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
                key: "continuous".into(),
                config: Config {
                    mode: "continuous".into(),
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
fn captured(s: &Store, p: &Policy) -> crate::capture::Capture {
    let mut c = s.capture(p.project_id, p.capture_id.unwrap()).unwrap();
    c.state = "capturing".into();
    c.worker_generation = s.generation();
    c.bootstrap_lsn = Some("0/C8".into());
    c.captured_lsn = Some("0/12C".into());
    c.observed_at_ms = Some(now());
    c.progress = Some(json!({"last_data_lsn":"0/12C"}));
    s.save_capture(&c).unwrap();
    c
}
#[test]
fn continuous_samples_one_target_and_idle_barriers_do_not_create_epochs() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let mut c = captured(&s, &policy);
    s.schedule_continuous(now()).unwrap();
    s.schedule_continuous(now()).unwrap();
    assert_eq!(s.active_sync_runs().unwrap().len(), 1);
    let id = s.active_sync_runs().unwrap()[0].id;
    for version in 0..2 {
        s.tick_triggered(now()).unwrap();
        let r = s.sync_run(p, id).unwrap();
        assert_eq!(r.target_lsn.as_deref(), Some("0/12C"));
        let mut a = s.incremental_run(p, r.apply_id.unwrap()).unwrap();
        prepare(&mut s, &c, &mut a, version);
        finish(&mut s, &a);
        c.captured_lsn = Some("0/190".into());
        s.save_capture(&c).unwrap();
        s.reconcile_sync(now()).unwrap();
    }
    assert_eq!(s.sync_run(p, id).unwrap().state, "succeeded");
    s.schedule_continuous(now() + 501).unwrap();
    assert!(s.active_sync_runs().unwrap().is_empty());
    assert!(
        s.sync_command(
            p,
            d,
            Command::RunNow {
                id: policy.id,
                expected_revision: 1,
                key: "manual".into()
            },
            now()
        )
        .is_err()
    );
}
#[test]
fn pause_drains_current_batch_and_mode_change_reuses_generation() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let c = captured(&s, &policy);
    s.schedule_continuous(now()).unwrap();
    s.tick_triggered(now()).unwrap();
    let r = s.active_sync_runs().unwrap()[0].clone();
    let paused: Policy = serde_json::from_value(
        s.sync_command(
            p,
            d,
            Command::Pause {
                id: policy.id,
                expected_revision: 1,
                key: "pause".into(),
            },
            now(),
        )
        .unwrap(),
    )
    .unwrap();
    assert!(paused.pause_requested);
    assert_eq!(paused.state, "active");
    let mut a = s.incremental_run(p, r.apply_id.unwrap()).unwrap();
    assert!(s.incremental_live(&a).is_ok());
    prepare(&mut s, &c, &mut a, 0);
    finish(&mut s, &a);
    s.reconcile_sync(now()).unwrap();
    let paused = s.sync_policy(p, policy.id).unwrap();
    assert_eq!(paused.state, "paused");
    assert!(!paused.pause_requested);
    assert_eq!(s.capture(p, c.id).unwrap().desired, "running");
    assert!(s.capture_live(&c).is_ok());
    let converted: Policy = serde_json::from_value(
        s.sync_command(
            p,
            d,
            Command::Update {
                id: policy.id,
                expected_revision: paused.revision,
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
    .unwrap();
    assert_eq!(converted.capture_id, Some(c.id));
    assert!(s.capture_live(&c).is_ok());
}
#[test]
fn freshness_needs_source_barrier_and_stream_evidence_and_reports_pressure() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let mut c = captured(&s, &policy);
    s.schedule_continuous(now()).unwrap();
    let id = s.active_sync_runs().unwrap()[0].id;
    for version in 0..2 {
        s.tick_triggered(now()).unwrap();
        let r = s.sync_run(p, id).unwrap();
        let mut a = s.incremental_run(p, r.apply_id.unwrap()).unwrap();
        prepare(&mut s, &c, &mut a, version);
        finish(&mut s, &a);
        s.reconcile_sync(now()).unwrap();
    }
    let at = now();
    c.observed_at_ms = Some(at);
    c.progress = Some(
        json!({"published_lsn":"0/12C","last_data_lsn":"0/12C","stream_observed_at_ms":at,"barrier_commit_at_ms":at,"backlog_bytes":0}),
    );
    s.save_capture(&c).unwrap();
    let view = |s: &Store, time| {
        s.sync_policy_view(&s.sync_policy(p, policy.id).unwrap(), time)
            .unwrap()["continuous_status"]
            .clone()
    };
    assert_eq!(view(&s, at)["state"], "healthy");
    assert_eq!(view(&s, at)["oldest_unpublished_commit_age_ms"], 0);
    assert_eq!(view(&s, at + 5001)["state"], "unavailable");
    assert!(view(&s, at + 5001)["oldest_unpublished_commit_age_ms"].is_null());
    c.progress.as_mut().unwrap()["barrier_commit_at_ms"] = Value::Null;
    s.save_capture(&c).unwrap();
    assert_eq!(view(&s, at)["state"], "catching_up");
    c.progress.as_mut().unwrap()["last_data_lsn"] = json!("0/190");
    c.progress.as_mut().unwrap()["oldest_commit_at_ms"] = json!(at - 6000);
    s.save_capture(&c).unwrap();
    assert_eq!(view(&s, at)["state"], "lagging");
    c.spool_bytes = Some(c.limits.spool_bytes * 4 / 5);
    s.save_capture(&c).unwrap();
    assert_eq!(view(&s, at)["pressure"], true);
    c.state = "unavailable".into();
    s.save_capture(&c).unwrap();
    assert_eq!(view(&s, at)["state"], "unavailable");
    assert!(view(&s, at)["oldest_unpublished_commit_age_ms"].is_null());
}
#[test]
fn continuous_configuration_rejects_schedule_and_invalid_budgets() {
    for c in [
        crate::sync::Continuous {
            freshness_ms: 0,
            batch_interval_ms: 500,
        },
        crate::sync::Continuous {
            freshness_ms: 1000,
            batch_interval_ms: 2000,
        },
    ] {
        assert!(
            Config {
                mode: "continuous".into(),
                strategy: "incremental".into(),
                continuous: Some(c),
                ..Default::default()
            }
            .validate()
            .is_err()
        );
    }
    let mut c = config();
    c.mode = "continuous".into();
    c.strategy = "incremental".into();
    assert!(c.validate().is_err());
}

#[test]
fn legacy_snapshot_retry_receipt_survives_new_continuous_configuration() {
    let (_dir, mut s, p, d, b) = setup();
    let command = || Command::Create {
        branch: b.to_string(),
        key: "legacy".into(),
        config: Config::default(),
    };
    let first = s.sync_command(p, d, command(), now()).unwrap();
    let legacy = format!(
        r#"{{"kind":"create","branch":"{b}","key":"legacy","config":{{"mode":"snapshot","strategy":"full","schedule":null,"limits":{{"max_bytes":1073741824,"timeout_ms":300000}}}}}}"#
    );
    s.db.execute(
        "UPDATE sync_requests SET request=?1 WHERE project_id=?2 AND request_key='legacy'",
        params![legacy, p.to_string()],
    )
    .unwrap();
    assert_eq!(s.sync_command(p, d, command(), now()).unwrap(), first);
}
