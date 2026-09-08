use std::{fs, io::Write};
use supabricks_core::resource::*;
use supabricks_local::{
    ingest::*,
    operations::{Mutation, Ports},
    project::ProjectConfig,
    recovery,
    store::Store,
    supervisor::OwnedProcess,
};
fn fixture() -> (tempfile::TempDir, Store, Load) {
    let tmp = tempfile::tempdir().unwrap();
    let mut s = Store::open(&tmp.path().join("data")).unwrap();
    let p = ProjectConfig {
        format_version: 1,
        id: ProjectId::new(),
        name: "example".into(),
    };
    s.register_project(&p).unwrap();
    let b = s
        .submit(
            p.id,
            "main",
            Mutation::CreateDatabase {
                name: "main".into(),
                ports: Ports {
                    sql: 5432,
                    external_http: 5433,
                    internal_http: 5434,
                },
            },
        )
        .unwrap();
    let source = s.acquire_source(p.id, "../../display-only.csv").unwrap();
    let mut file = s.source_writer(p.id, source.id).unwrap();
    file.write_all(b"id\n9007199254740993\n").unwrap();
    drop(file);
    let source = s.seal_source(p.id, source.id).unwrap();
    let load = Load {
        version: 1,
        project_id: p.id,
        branch_id: b.branch_id,
        branch_revision: 1,
        source_id: source.id,
        source_sha256: source.sha256.unwrap(),
        schema: "public".into(),
        table: "orders".into(),
        mapping: Mapping {
            version: 1,
            format: Format::Csv,
            delimiter: ",".into(),
            header: true,
            null_strings: vec![],
            columns: vec![Column {
                input: "id".into(),
                name: "id".into(),
                data_type: DataType::Bigint,
                nullable: false,
            }],
        },
    };
    (tmp, s, load)
}
fn worker(s: &Store, j: &Job) -> OwnedProcess {
    OwnedProcess {
        root: s.root().to_owned(),
        generation: s.generation(),
        role: format!("ingest-{}", j.id),
        pid: 999999,
        start_identity: "synthetic-contract-no-process".into(),
        token: "ticket".into(),
        branch: Some((j.load.branch_id, j.load.branch_revision)),
    }
}
#[test]
fn immutable_sources_idempotency_ownership_limits_and_lifecycle() {
    let (_tmp, mut s, load) = fixture();
    let p = load.project_id;
    assert!(s.ingest_source(ProjectId::new(), load.source_id).is_err());
    assert!(s.source_writer(p, load.source_id).is_err());
    let j = s.create_ingest("first", load.clone()).unwrap();
    assert_eq!(s.create_ingest("first", load.clone()).unwrap().id, j.id);
    let mut changed = load.clone();
    changed.table = "different".into();
    assert!(s.create_ingest("first", changed).is_err());
    assert!(s.create_ingest("second", load.clone()).is_err());
    assert!(s.dispose_source(p, load.source_id, false).is_err());
    assert!(
        s.submit(
            p,
            "suspend",
            Mutation::SetState {
                branch_id: load.branch_id,
                expected_revision: 1,
                desired: DesiredState::Suspended
            }
        )
        .is_err()
    );
    assert!(
        s.submit(
            p,
            "force",
            Mutation::ForceDelete {
                branch_id: load.branch_id,
                expected_revision: 1
            }
        )
        .is_err()
    );
    assert_eq!(s.cancel_ingest(p, j.id).unwrap().state, State::Cancelled);
    s.dispose_source(p, load.source_id, false).unwrap();
    s.dispose_source(p, load.source_id, false).unwrap();
    assert_eq!(s.create_ingest("first", load.clone()).unwrap().id, j.id);
    let mut invalid = load.mapping.clone();
    invalid.columns.push(invalid.columns[0].clone());
    assert!(invalid.fingerprint().is_err());
    let mut invalid = load.clone();
    invalid.schema = INTERNAL_SCHEMA.into();
    assert!(invalid.validate().is_err());
    let mut view = Inspection {
        version: 1,
        source_id: load.source_id,
        source_sha256: load.source_sha256.clone(),
        mapping: load.mapping.clone(),
        sample_only: true,
        rows: vec![vec![Some("x".into())]; 100],
    };
    view.validate().unwrap();
    view.rows.push(vec![None]);
    assert!(view.validate().is_err());
    for _ in 0..5 {
        s.acquire_source(p, "reserved").unwrap();
    }
    assert!(s.acquire_source(p, "over budget").is_err());
}
#[test]
fn commit_ambiguity_fencing_inherited_receipts_and_retry() {
    let (tmp, mut s, load) = fixture();
    let p = load.project_id;
    let j = s.create_ingest("commit", load.clone()).unwrap();
    let w = worker(&s, &j);
    s.start_ingest(p, j.id, &w).unwrap();
    s.ingest_progress(p, j.id, &w, 12, 10).unwrap();
    assert!(s.ingest_progress(p, j.id, &w, 11, 10).is_err());
    let j = s.cancel_ingest(p, j.id).unwrap();
    assert_eq!(j.state, State::Reconciling);
    assert_eq!(j.committed_rows, None);
    assert!(s.fence_ingest(p, j.id).is_err());
    assert!(
        s.reconcile_ingest(
            p,
            j.id,
            Reconciliation::Absent {
                target_exists: false
            }
        )
        .is_err()
    );
    // No OS process exists in this contract fixture. Native process death is
    // separately qualified by process_ownership and the installed harness.
    s.forget_native_process(&w).unwrap();
    s.fence_ingest(p, j.id).unwrap();
    assert_eq!(
        s.reconcile_ingest(p, j.id, Reconciliation::Unknown)
            .unwrap()
            .state,
        State::Reconciling
    );
    assert!(
        s.reconcile_ingest(
            p,
            j.id,
            Reconciliation::Absent {
                target_exists: true
            }
        )
        .is_err()
    );
    drop(s);
    assert!(
        recovery::create(
            &tmp.path().join("data"),
            &tmp.path().join("uncertain-backup")
        )
        .is_err()
    );
    let mut s = Store::open(&tmp.path().join("data")).unwrap();
    let mut r = Receipt {
        version: 1,
        origin: s.ingest_origin().unwrap(),
        project_id: p,
        branch_id: BranchId::new(),
        job_id: j.id,
        source_sha256: load.source_sha256.clone(),
        mapping_fingerprint: load.mapping.fingerprint().unwrap(),
        schema: load.schema.clone(),
        table: load.table.clone(),
        table_oid: 123,
        committed_rows: 12,
    };
    assert!(
        s.reconcile_ingest(
            p,
            j.id,
            Reconciliation::Committed {
                receipt: r.clone(),
                target_oid: 123
            }
        )
        .is_err()
    );
    r.branch_id = load.branch_id;
    assert!(
        s.reconcile_ingest(
            p,
            j.id,
            Reconciliation::Committed {
                receipt: r.clone(),
                target_oid: 124
            }
        )
        .is_err()
    );
    let j = s
        .reconcile_ingest(
            p,
            j.id,
            Reconciliation::Committed {
                receipt: r,
                target_oid: 123,
            },
        )
        .unwrap();
    assert_eq!(j.state, State::Succeeded);
    assert_eq!(j.committed_rows, Some(12)); // commit wins cancellation
    s.dispose_source(p, load.source_id, false).unwrap();
    assert!(s.retry_ingest(p, j.id).is_err());
}
#[test]
fn rollback_retry_and_backup_restore_preserve_origin_sources_and_jobs() {
    let (tmp, mut s, load) = fixture();
    let p = load.project_id;
    let j = s.create_ingest("rollback", load.clone()).unwrap();
    let w = worker(&s, &j);
    s.start_ingest(p, j.id, &w).unwrap();
    s.forget_native_process(&w).unwrap();
    s.fence_ingest(p, j.id).unwrap();
    assert!(
        s.reconcile_ingest(
            p,
            j.id,
            Reconciliation::Absent {
                target_exists: false
            }
        )
        .unwrap()
        .retryable
    );
    assert_eq!(s.retry_ingest(p, j.id).unwrap().state, State::Queued);
    let w = worker(&s, &j);
    let j = s.start_ingest(p, j.id, &w).unwrap();
    assert_eq!(j.attempt, 2);
    s.forget_native_process(&w).unwrap();
    s.fence_ingest(p, j.id).unwrap();
    s.reconcile_ingest(
        p,
        j.id,
        Reconciliation::Absent {
            target_exists: false,
        },
    )
    .unwrap();
    let origin = s.ingest_origin().unwrap();
    drop(s);
    let root = tmp.path().join("data");
    let backup = tmp.path().join("backup");
    recovery::create(&root, &backup).unwrap();
    assert!(
        recovery::verify(&backup)
            .unwrap()
            .files
            .contains_key(&format!("ingest/sources/{}.source", load.source_id))
    );
    let restored = tmp.path().join("restored");
    recovery::restore(&backup, &restored).unwrap();
    let mut s = Store::open(&restored).unwrap();
    assert_eq!(s.ingest_origin().unwrap(), origin);
    assert_eq!(s.ingest_job(p, j.id).unwrap().state, State::Failed);
    assert_eq!(
        s.ingest_source(p, load.source_id).unwrap().sha256,
        Some(load.source_sha256)
    );
    assert!(s.ingest_progress(p, j.id, &w, 99, 99).is_err());
    s.dispose_source(p, load.source_id, false).unwrap();
    assert!(!s.ingest_job(p, j.id).unwrap().retryable);
    assert!(
        !fs::metadata(restored.join(format!("ingest/sources/{}.source", load.source_id))).is_ok()
    );
}
#[test]
fn source_symlinks_and_missing_payload_cannot_be_sealed() {
    let (_tmp, mut s, load) = fixture();
    let p = load.project_id;
    let source = s.acquire_source(p, "missing").unwrap();
    assert!(s.seal_source(p, source.id).is_err());
    let path = s.root().join(format!("ingest/sources/{}.part", source.id));
    std::os::unix::fs::symlink("/etc/passwd", path).unwrap();
    assert!(s.seal_source(p, source.id).is_err());
}

#[test]
fn owner_replacement_fences_real_import_worker_and_preserves_uncertainty() {
    use std::{
        collections::BTreeMap,
        os::unix::process::CommandExt,
        process::{Command, Stdio},
        time::{Duration, Instant},
    };
    use supabricks_local::{
        daemon::Daemon,
        supervisor::{self, Launch},
    };
    let (_tmp, mut s, load) = fixture();
    let p = load.project_id;
    let source = s.acquire_source(p, "interrupted-upload").unwrap();
    let j = s.create_ingest("real-worker", load.clone()).unwrap();
    let launch = Launch {
        root: s.root().to_owned(),
        generation: s.generation(),
        role: format!("ingest-{}", j.id),
        token: "ingest-process-contract".into(),
        branch: Some((load.branch_id, 1)),
        argv: vec![
            "/bin/sh".into(),
            "-c".into(),
            "touch started; exec sleep 30".into(),
        ],
        env: BTreeMap::from([("PATH".into(), "/usr/bin:/bin".into())]),
        cwd: s.root().to_owned(),
    };
    let config = s.root().join("worker.json");
    supervisor::write_json(&config, &launch).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["child", "--launch"])
        .arg(&config)
        .arg("--stdin-gate")
        .env_clear()
        .env("SUPABRICKS_PROCESS_TOKEN", &launch.token)
        .process_group(0)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let w = supervisor::evidence(&launch, child.id()).unwrap();
    s.start_ingest(p, j.id, &w).unwrap();
    child.stdin.take().unwrap().write_all(&[1]).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while !s.root().join("started").exists() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let root = s.root().to_owned();
    drop(s);
    let daemon = Daemon::bind(&root).unwrap();
    assert!(supervisor::members(&w).unwrap().is_empty());
    child.wait().unwrap();
    drop(daemon);
    let mut s = Store::open(&root).unwrap();
    let j = s.ingest_job(p, j.id).unwrap();
    assert_eq!(j.state, State::Reconciling);
    assert!(j.worker.is_none());
    assert_eq!(s.ingest_source(p, source.id).unwrap().state, "interrupted");
    assert!(s.ingest_progress(p, j.id, &w, 10, 10).is_err());
    s.dispose_source(p, source.id, false).unwrap();
}
