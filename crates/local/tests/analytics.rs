//! Filesystem/SQLite protocol tests; real Delta/Postgres interoperability is native CI.
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Command, Stdio},
    sync::mpsc,
    time::Duration,
};
use supabricks_core::resource::*;
use supabricks_local::{
    analytics::Publisher,
    operations::{Mutation, Ports, Step},
    project::ProjectConfig,
    store::{ExportLimits, Store},
};

fn parent(store: &mut Store) -> (ProjectId, BranchId) {
    let project = ProjectConfig {
        format_version: 1,
        id: ProjectId::new(),
        name: "snapshots".into(),
    };
    store.register_project(&project).unwrap();
    let op = store
        .submit(
            project.id,
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
    while let Some(t) = store.ticket(op.id).unwrap() {
        store.checkpoint(&t, json!({})).unwrap();
    }
    (project.id, op.branch_id)
}
fn complete_export(store: &mut Store, project: ProjectId, branch: BranchId, n: u16) -> OperationId {
    let op = store
        .submit(
            project,
            &format!("export-{n}"),
            Mutation::Export {
                parent_id: branch,
                ports: Ports {
                    sql: 5500 + n * 3,
                    external_http: 5501 + n * 3,
                    internal_http: 5502 + n * 3,
                },
                limits: ExportLimits::default(),
            },
        )
        .unwrap();
    while let Some(t) = store.ticket(op.id).unwrap() {
        if t.step == Step::CaptureBranchPoint {
            store
                .pin_lsn(
                    &t,
                    format!("0/{:X}", 8192 + u32::from(n) * 8).parse().unwrap(),
                )
                .unwrap();
        }
        store.checkpoint(&t, json!({})).unwrap();
    }
    let child = store.branch(op.branch_id).unwrap();
    let source = store.branch(branch).unwrap();
    let root = store
        .root()
        .join("analytics/staging")
        .join(op.id.to_string());
    fs::create_dir_all(&root).unwrap();
    let mut files = Vec::new();
    let mut tables = Vec::new();
    for (oid, name) in [(101, "orders"), (102, "payments")] {
        fs::create_dir_all(root.join(format!("{oid}/_delta_log"))).unwrap();
        for path in [
            format!("{oid}/data.parquet"),
            format!("{oid}/_delta_log/{:020}.json", 0),
        ] {
            let data = format!("fixture {n} {name}").into_bytes();
            fs::write(root.join(&path), &data).unwrap();
            files.push(
                json!({"path":path,"bytes":data.len(),"sha256":hex::encode(Sha256::digest(&data))}),
            );
        }
        tables.push(json!({"oid":oid,"schema":"public","name":name,"path":oid.to_string(),"version":0,"rows":n,"columns":[{"name":"id","arrow_type":"int32"}]}));
    }
    let manifest = json!({"format_version":1,"status":"files_complete","published":false,"id":op.id,
        "source":{"project_id":project,"branch_id":branch,"timeline_id":source.branch.timeline_id,"tenant_id":child.branch.tenant_id,"export_branch_id":child.branch.id,"export_timeline_id":child.branch.timeline_id,"lsn":child.branch.ancestor_lsn},
        "database":"postgres","database_oid":5,"versions":{"fixture":"1"},"transaction":"REPEATABLE READ READ ONLY","tables":tables,"files":files});
    fs::write(
        root.join("manifest.json"),
        serde_json::to_vec(&manifest).unwrap(),
    )
    .unwrap();
    // Model A01's completed/fenced worker and retired child, without a fake PG server.
    let db = rusqlite::Connection::open(store.root().join("state.sqlite3")).unwrap();
    db.execute(
        "UPDATE exports SET state='complete',outcome=?2 WHERE id=?1",
        rusqlite::params![
            op.id.to_string(),
            json!({"status":"files_complete"}).to_string()
        ],
    )
    .unwrap();
    db.execute(
        "UPDATE branches SET desired='deleted',observed_revision=revision WHERE id=?1",
        [op.branch_id.to_string()],
    )
    .unwrap();
    db.execute(
        "UPDATE branch_pins SET active=0 WHERE child_id=?1",
        [op.branch_id.to_string()],
    )
    .unwrap();
    op.id
}
fn publish(
    store: &mut Store,
    publisher: &mut Publisher,
    project: ProjectId,
    id: OperationId,
) -> EpochId {
    let p = store.publish_export(project, id).unwrap();
    for _ in 0..10 {
        publisher.tick(store).unwrap();
        if store.publication(id).unwrap().state == "published" {
            return p.epoch_id;
        }
    }
    panic!("publication never finished")
}
fn root() -> tempfile::TempDir {
    tempfile::Builder::new()
        .prefix("sb-a02-")
        .tempdir_in("/tmp")
        .unwrap()
}
#[test]
fn publication_leases_retention_and_restart_preserve_references() {
    let root = root();
    let path = root.path().join("state");
    let mut store = Store::open(&path).unwrap();
    let (project, branch) = parent(&mut store);
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let a = complete_export(&mut store, project, branch, 1);
    let b = complete_export(&mut store, project, branch, 2);
    let pa = store.publish_export(project, a).unwrap();
    assert_eq!(
        store.publish_export(project, a).unwrap().epoch_id,
        pa.epoch_id
    );
    assert!(store.publish_export(project, b).is_err());
    assert!(store.current_snapshot(project, branch).is_err());
    let first = publish(&mut store, &mut publisher, project, a);
    let lease = store.pin_snapshot(project, first, 60000).unwrap();
    let legacy = store
        .acquire_lease(
            branch,
            Some(first),
            "legacy-reader",
            Duration::from_secs(60),
        )
        .unwrap();
    assert!(store.pin_snapshot(ProjectId::new(), first, 60000).is_err());
    let second = publish(&mut store, &mut publisher, project, b);
    assert_eq!(
        store
            .current_snapshot(project, branch)
            .unwrap()
            .publication
            .epoch_id,
        second
    );
    assert_eq!(
        store
            .snapshot(project, first)
            .unwrap()
            .publication
            .descriptor
            .unwrap()["manifest"]["tables"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(
        store.collect_snapshots(project, branch, 1).unwrap()["deleting"],
        json!([])
    );
    let page = store.snapshot_history(project, branch, None, 1).unwrap();
    assert_eq!(page[0].publication.epoch_id, second);
    assert!(page[0].publication.descriptor.is_none());
    assert_eq!(
        store
            .snapshot_history(project, branch, Some(page[0].publication.ordinal), 1)
            .unwrap()[0]
            .publication
            .epoch_id,
        first
    );
    let installation = store.installation_id().unwrap();
    drop(store);
    let mut store = Store::open(&path).unwrap();
    let mut publisher = Publisher::recover(&mut store).unwrap();
    assert_eq!(store.installation_id().unwrap(), installation);
    store
        .renew_snapshot_lease(project, lease.id, 60000)
        .unwrap();
    // A Postgres force-delete must not revoke analytical readers or their files.
    let rev = store.branch(branch).unwrap().revision;
    store
        .submit(
            project,
            "delete",
            Mutation::ForceDelete {
                branch_id: branch,
                expected_revision: rev,
            },
        )
        .unwrap();
    assert_eq!(
        store.collect_snapshots(project, branch, 1).unwrap()["deleting"],
        json!([])
    );
    store.release_snapshot_lease(project, lease.id).unwrap();
    assert_eq!(
        store.collect_snapshots(project, branch, 1).unwrap()["deleting"],
        json!([])
    );
    store.release_lease(&legacy).unwrap();
    let expired = store.pin_snapshot(project, first, 1000).unwrap();
    let db = rusqlite::Connection::open(store.root().join("state.sqlite3")).unwrap();
    db.execute(
        "UPDATE snapshot_leases SET expires_at_ms=0 WHERE id=?1",
        [expired.id.to_string()],
    )
    .unwrap();
    assert!(
        store
            .renew_snapshot_lease(project, expired.id, 60000)
            .is_err()
    );
    assert_eq!(
        store.collect_snapshots(project, branch, 1).unwrap()["deleting"],
        json!([first])
    );
    assert!(store.pin_snapshot(project, first, 60000).is_err());
    publisher.tick(&mut store).unwrap();
    assert_eq!(store.snapshot(project, first).unwrap().state, "deleted");
    assert_eq!(
        store
            .current_snapshot(project, branch)
            .unwrap()
            .publication
            .epoch_id,
        second
    );
    assert!(!path.join(format!("analytics/generations/{a}")).exists());
}
#[test]
fn stale_source_cancellation_and_bad_files_never_replace_the_head() {
    let root = root();
    let mut store = Store::open(&root.path().join("state")).unwrap();
    let (project, branch) = parent(&mut store);
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let old = complete_export(&mut store, project, branch, 1);
    let new = complete_export(&mut store, project, branch, 2);
    let current = publish(&mut store, &mut publisher, project, new);
    assert!(store.publish_export(project, old).is_err());
    let bad = complete_export(&mut store, project, branch, 3);
    fs::write(
        store
            .root()
            .join(format!("analytics/staging/{bad}/101/data.parquet")),
        b"corrupt",
    )
    .unwrap();
    store.publish_export(project, bad).unwrap();
    assert!(publisher.tick(&mut store).is_err());
    publisher.tick(&mut store).unwrap();
    assert_eq!(store.publication(bad).unwrap().state, "failed");
    let cancelled = complete_export(&mut store, project, branch, 4);
    store.publish_export(project, cancelled).unwrap();
    publisher.tick(&mut store).unwrap();
    assert_eq!(
        store.publication(cancelled).unwrap().state,
        "files_complete"
    );
    store.discard_export(project, cancelled).unwrap();
    publisher.tick(&mut store).unwrap();
    assert_eq!(store.publication(cancelled).unwrap().state, "cancelled");
    let stale = complete_export(&mut store, project, branch, 5);
    store.publish_export(project, stale).unwrap();
    publisher.tick(&mut store).unwrap();
    let rev = store.branch(branch).unwrap().revision;
    store
        .submit(
            project,
            "suspend",
            Mutation::SetState {
                branch_id: branch,
                expected_revision: rev,
                desired: DesiredState::Suspended,
            },
        )
        .unwrap();
    assert!(publisher.tick(&mut store).is_err());
    publisher.tick(&mut store).unwrap();
    assert_eq!(store.publication(stale).unwrap().state, "failed");
    assert_eq!(
        store
            .current_snapshot(project, branch)
            .unwrap()
            .publication
            .epoch_id,
        current
    );
}
#[test]
fn disk_error_and_sql_rollback_preserve_the_previous_complete_snapshot() {
    for point in ["descriptor_written", "before_commit"] {
        let root = root();
        let mut store = Store::open(&root.path().join("state")).unwrap();
        let (project, branch) = parent(&mut store);
        let mut publisher = Publisher::recover(&mut store).unwrap();
        let a = complete_export(&mut store, project, branch, 1);
        let first = publish(&mut store, &mut publisher, project, a);
        let b = complete_export(&mut store, project, branch, 2);
        store.publish_export(project, b).unwrap();
        let mut hook = |name: &str| -> supabricks_local::store::Result<()> {
            if name == point {
                Err(std::io::Error::from_raw_os_error(libc::ENOSPC).into())
            } else {
                Ok(())
            }
        };
        while store.publication(b).unwrap().state != "failed" {
            let _ = publisher.tick_with_hook(&mut store, &mut hook);
        }
        assert_eq!(
            store
                .current_snapshot(project, branch)
                .unwrap()
                .publication
                .epoch_id,
            first
        );
        publisher.tick(&mut store).unwrap();
        assert!(
            !store
                .root()
                .join(format!("analytics/generations/{b}"))
                .exists()
        );
    }
    let root = root();
    let mut store = Store::open(&root.path().join("state")).unwrap();
    let (project, branch) = parent(&mut store);
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let a = complete_export(&mut store, project, branch, 1);
    let first = publish(&mut store, &mut publisher, project, a);
    let b = complete_export(&mut store, project, branch, 2);
    let next = store.publish_export(project, b).unwrap();
    publisher.tick(&mut store).unwrap();
    let db = rusqlite::Connection::open(store.root().join("state.sqlite3")).unwrap();
    db.execute_batch("CREATE TRIGGER fail_pointer BEFORE UPDATE ON snapshot_heads BEGIN SELECT RAISE(ABORT,'injected SQLite write failure'); END;").unwrap();
    assert!(publisher.tick(&mut store).is_err());
    assert!(store.epoch(next.epoch_id).is_err());
    assert_eq!(
        store
            .current_snapshot(project, branch)
            .unwrap()
            .publication
            .epoch_id,
        first
    );
}
#[test]
fn recovery_fails_closed_on_missing_files_and_accepts_a_restored_bundle() {
    let root = root();
    let path = root.path().join("state");
    let mut store = Store::open(&path).unwrap();
    let (project, branch) = parent(&mut store);
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let a = complete_export(&mut store, project, branch, 1);
    let first = publish(&mut store, &mut publisher, project, a);
    let file = path.join(format!("analytics/generations/{a}/101/data.parquet"));
    let bytes = fs::read(&file).unwrap();
    fs::remove_file(&file).unwrap();
    drop(store);
    let mut store = Store::open(&path).unwrap();
    Publisher::recover(&mut store).unwrap();
    assert!(store.current_snapshot(project, branch).is_err());
    assert_eq!(store.snapshot(project, first).unwrap().state, "unavailable");
    fs::write(&file, bytes).unwrap();
    drop(store);
    let mut store = Store::open(&path).unwrap();
    Publisher::recover(&mut store).unwrap();
    assert_eq!(
        store
            .current_snapshot(project, branch)
            .unwrap()
            .publication
            .epoch_id,
        first
    );
}
#[test]
fn publication_crash_worker() {
    let Some(path) = std::env::var_os("SUPABRICKS_A02_TEST_ROOT") else {
        return;
    };
    let point = std::env::var("SUPABRICKS_A02_TEST_POINT").unwrap();
    let mut store = Store::open(Path::new(&path)).unwrap();
    let mut publisher = Publisher::recover(&mut store).unwrap();
    for _ in 0..20 {
        publisher
            .tick_with_hook(&mut store, &mut |name| {
                if name == point {
                    println!("A02_BOUNDARY");
                    std::io::stdout().flush().unwrap();
                    loop {
                        std::thread::park();
                    }
                }
                Ok(())
            })
            .unwrap();
    }
    panic!("crash point not reached: {point}");
}
#[test]
fn sigkill_at_each_publication_and_deletion_boundary_is_recoverable() {
    for point in [
        "verified_file:1",
        "verified_file:2",
        "verified_file:3",
        "verified_file:4",
        "descriptor_written",
        "descriptor_durable",
        "files_complete",
        "before_rename",
        "after_rename",
        "before_commit",
        "after_commit",
        "before_gc",
        "after_gc_files",
        "after_gc_commit",
    ] {
        let root = root();
        let path = root.path().join("state");
        let mut store = Store::open(&path).unwrap();
        let (project, branch) = parent(&mut store);
        let mut publisher = Publisher::recover(&mut store).unwrap();
        let a = complete_export(&mut store, project, branch, 1);
        let first = publish(&mut store, &mut publisher, project, a);
        let b = complete_export(&mut store, project, branch, 2);
        let second = store.publish_export(project, b).unwrap().epoch_id;
        let gc = point.contains("gc");
        if gc {
            publish(&mut store, &mut publisher, project, b);
            store.collect_snapshots(project, branch, 1).unwrap();
        }
        drop(store);
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "publication_crash_worker", "--nocapture"])
            .env("SUPABRICKS_A02_TEST_ROOT", &path)
            .env("SUPABRICKS_A02_TEST_POINT", point)
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (send, receive) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                if line.unwrap() == "A02_BOUNDARY" {
                    let _ = send.send(());
                    break;
                }
            }
        });
        let reached = receive.recv_timeout(Duration::from_secs(20));
        let _ = child.kill();
        child.wait().unwrap();
        reached.expect(point);
        let mut store = Store::open(&path).unwrap();
        let mut publisher = Publisher::recover(&mut store).unwrap();
        let expected = if point == "after_commit" || gc {
            second
        } else {
            first
        };
        assert_eq!(
            store
                .current_snapshot(project, branch)
                .unwrap()
                .publication
                .epoch_id,
            expected,
            "{point}"
        );
        for _ in 0..10 {
            publisher.tick(&mut store).unwrap();
        }
        assert_eq!(
            store
                .current_snapshot(project, branch)
                .unwrap()
                .publication
                .epoch_id,
            second,
            "{point}"
        );
        assert_eq!(store.epoch(second).unwrap().tables.len(), 2);
        if gc {
            assert_eq!(store.snapshot(project, first).unwrap().state, "deleted");
        }
    }
}

#[test]
fn malformed_generations_are_rejected_before_they_can_become_current() {
    for fault in [
        "checksum",
        "extra_file",
        "symlink",
        "identity",
        "missing_log",
        "traversal",
        "duplicate_oid",
    ] {
        let root = root();
        let mut store = Store::open(&root.path().join("state")).unwrap();
        let (project, branch) = parent(&mut store);
        let mut publisher = Publisher::recover(&mut store).unwrap();
        let a = complete_export(&mut store, project, branch, 1);
        let first = publish(&mut store, &mut publisher, project, a);
        let b = complete_export(&mut store, project, branch, 2);
        let generation = store.root().join(format!("analytics/staging/{b}"));
        let path = generation.join("manifest.json");
        let mut manifest: serde_json::Value =
            serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        let outside = root.path().join("do-not-touch");
        fs::write(&outside, b"keep").unwrap();
        match fault {
            "checksum" => {
                let file = generation.join("101/data.parquet");
                let size = fs::metadata(&file).unwrap().len();
                fs::write(file, vec![b'x'; size as usize]).unwrap();
            }
            "extra_file" => fs::write(generation.join("extra"), b"extra").unwrap(),
            "symlink" => {
                let file = generation.join("101/data.parquet");
                fs::remove_file(&file).unwrap();
                std::os::unix::fs::symlink(&outside, file).unwrap();
            }
            "identity" => manifest["source"]["project_id"] = json!(ProjectId::new()),
            "missing_log" => manifest["tables"][0]["version"] = json!(1),
            "traversal" => manifest["files"][0]["path"] = json!("../../do-not-touch"),
            "duplicate_oid" => manifest["tables"][1]["oid"] = json!(101),
            _ => unreachable!(),
        }
        fs::write(path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        store.publish_export(project, b).unwrap();
        assert!(publisher.tick(&mut store).is_err(), "{fault}");
        publisher.tick(&mut store).unwrap();
        assert_eq!(
            store
                .current_snapshot(project, branch)
                .unwrap()
                .publication
                .epoch_id,
            first,
            "{fault}"
        );
        assert_eq!(fs::read(outside).unwrap(), b"keep");
    }
}
#[test]
fn stopped_analytical_bundle_keeps_identity_pointers_and_leases_when_copied() {
    fn copy(from: &Path, to: &Path) {
        fs::create_dir(to).unwrap();
        fs::set_permissions(to, fs::metadata(from).unwrap().permissions()).unwrap();
        for entry in fs::read_dir(from).unwrap() {
            let path = entry.unwrap().path();
            let target = to.join(path.file_name().unwrap());
            if path.is_dir() {
                copy(&path, &target);
            } else {
                fs::copy(path, target).unwrap();
            }
        }
    }
    let root = root();
    let path = root.path().join("original");
    let mut store = Store::open(&path).unwrap();
    let (project, branch) = parent(&mut store);
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let a = complete_export(&mut store, project, branch, 1);
    let epoch = publish(&mut store, &mut publisher, project, a);
    let lease = store.pin_snapshot(project, epoch, 60000).unwrap();
    let installation = store.installation_id().unwrap();
    drop(store);
    drop(publisher);
    let restored = root.path().join("restored");
    copy(&path, &restored);
    let mut store = Store::open(&restored).unwrap();
    Publisher::recover(&mut store).unwrap();
    assert_eq!(store.installation_id().unwrap(), installation);
    assert_eq!(
        store
            .current_snapshot(project, branch)
            .unwrap()
            .publication
            .epoch_id,
        epoch
    );
    store
        .renew_snapshot_lease(project, lease.id, 60000)
        .unwrap();
    assert!(
        restored
            .join(format!("analytics/generations/{a}/101/data.parquet"))
            .exists()
    );
    drop(store);
    fs::remove_file(restored.join("state.sqlite3")).unwrap();
    assert!(Store::open(&restored).is_err());
}

#[test]
fn analytical_session_references_survive_expiry_and_closing_until_verified_recovery() {
    use supabricks_local::sessions::Sessions;
    let root = root();
    let path = root.path().join("state");
    let mut store = Store::open(&path).unwrap();
    let (project, branch) = parent(&mut store);
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let a = complete_export(&mut store, project, branch, 31);
    let first = publish(&mut store, &mut publisher, project, a);
    let request = json!({"branch":branch,"ttl_ms":10000});
    let session = store
        .admit_analytical_session(
            project,
            branch,
            "reader",
            request.clone(),
            Some(first),
            None,
            10000,
        )
        .unwrap();
    assert_eq!(
        session.id,
        store
            .admit_analytical_session(
                project,
                branch,
                "reader",
                request.clone(),
                Some(first),
                None,
                10000
            )
            .unwrap()
            .id
    );
    assert!(
        store
            .admit_analytical_session(
                project,
                branch,
                "reader",
                json!({}),
                Some(first),
                None,
                10000
            )
            .is_err()
    );
    assert!(
        store
            .analytical_session(ProjectId::new(), session.id)
            .is_err()
    );
    let b = complete_export(&mut store, project, branch, 32);
    publish(&mut store, &mut publisher, project, b);
    // Advance the durable session deadline without actually sleeping. The GC
    // reference must not depend on clocks or an orphan worker renewing a lease.
    let db = rusqlite::Connection::open(path.join("state.sqlite3")).unwrap();
    db.execute("UPDATE analytical_sessions SET expires_at_ms=0", [])
        .unwrap();
    assert_eq!(
        store.collect_snapshots(project, branch, 1).unwrap()["deleting"],
        json!([])
    );
    store
        .close_analytical_session(project, session.id, "cancelled")
        .unwrap();
    assert_eq!(
        store.collect_snapshots(project, branch, 1).unwrap()["deleting"],
        json!([])
    );
    drop(db);
    drop(store);
    let mut store = Store::open(&path).unwrap();
    assert_eq!(
        store.collect_snapshots(project, branch, 1).unwrap()["deleting"],
        json!([])
    );
    // No native process was launched in this fixture. Recovery proves there is
    // no surviving worker before making its reference collectible.
    Sessions::recover(&mut store).unwrap();
    assert_eq!(
        store.analytical_session(project, session.id).unwrap().state,
        "failed"
    );
    assert_eq!(
        store.collect_snapshots(project, branch, 1).unwrap()["deleting"],
        json!([first])
    );
    assert!(
        store
            .admit_analytical_session(
                project,
                branch,
                "too-late",
                json!({}),
                Some(first),
                None,
                10000
            )
            .is_err()
    );
}

#[test]
fn analytical_session_admission_is_bounded_and_epoch_selection_is_project_scoped() {
    let root = root();
    let mut store = Store::open(&root.path().join("state")).unwrap();
    let (project, branch) = parent(&mut store);
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let a = complete_export(&mut store, project, branch, 41);
    let epoch = publish(&mut store, &mut publisher, project, a);
    assert!(
        store
            .admit_analytical_session(project, branch, "short", json!({}), Some(epoch), None, 1)
            .is_err()
    );
    assert!(
        store
            .admit_analytical_session(
                project,
                branch,
                "long",
                json!({}),
                Some(epoch),
                None,
                3600001
            )
            .is_err()
    );
    assert!(
        store
            .admit_analytical_session(
                ProjectId::new(),
                branch,
                "other",
                json!({}),
                Some(epoch),
                None,
                60000
            )
            .is_err()
    );
    for key in ["one", "two"] {
        store
            .admit_analytical_session(project, branch, key, json!({}), Some(epoch), None, 60000)
            .unwrap();
    }
    assert!(
        store
            .admit_analytical_session(
                project,
                branch,
                "three",
                json!({}),
                Some(epoch),
                None,
                60000
            )
            .is_err()
    );
    let old = store
        .session_for_key(project, "one", &json!({}))
        .unwrap()
        .unwrap();
    assert_eq!(
        store
            .admit_analytical_session(project, branch, "one", json!({}), Some(epoch), None, 60000)
            .unwrap()
            .id,
        old.id
    );
    assert!(
        supabricks_local::sessions::Sessions::query(
            &mut store,
            project,
            old.id,
            "SELECT 1".into(),
            1001,
            262144,
            10000
        )
        .is_err()
    );
    assert!(
        supabricks_local::sessions::Sessions::query(
            &mut store,
            project,
            old.id,
            "SELECT 1".into(),
            1,
            262145,
            10000
        )
        .is_err()
    );
    assert!(
        supabricks_local::sessions::Sessions::query(
            &mut store,
            project,
            old.id,
            "SELECT 1".into(),
            1,
            262144,
            30001
        )
        .is_err()
    );
    assert!(
        supabricks_local::sessions::Sessions::query(
            &mut store,
            project,
            old.id,
            "SELECT 1".into(),
            1,
            262144,
            10000
        )
        .is_err()
    ); // not ready
}
