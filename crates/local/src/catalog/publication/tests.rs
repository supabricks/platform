use super::*;
use crate::{
    analytics::Publisher,
    deployments::Source,
    operations::{Mutation, Ports, Step},
    project::ProjectConfig,
    store::ExportLimits,
};
use sha2::{Digest, Sha256};
use std::fs;
fn fixture() -> (tempfile::TempDir, Store, Context, Binding, BranchId) {
    let dir = tempfile::tempdir().unwrap();
    let work = dir.path().join("project");
    std::fs::create_dir(&work).unwrap();
    let project = ProjectConfig::initialize(&work, "same-name").unwrap();
    let mut store = Store::open(&dir.path().join("data")).unwrap();
    let context = store
        .resolve_deployment(&Source {
            definition_id: project.id,
            worktree: work.canonicalize().unwrap(),
        })
        .unwrap();
    let binding = context.binding(&work.canonicalize().unwrap());
    let branch = store
        .submit(
            project.id,
            "create",
            Mutation::CreateDatabase {
                name: "main".into(),
                ports: Ports {
                    sql: 51000,
                    external_http: 51001,
                    internal_http: 51002,
                },
            },
        )
        .unwrap()
        .branch_id;
    for operation in store.pending().unwrap() {
        while let Some(t) = store.ticket(operation.id).unwrap() {
            store.checkpoint(&t, json!({})).unwrap();
        }
    }
    (dir, store, context, binding, branch)
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
            let data = if path.ends_with(".json") {
                json!({"metaData":{"schemaString":json!({"type":"struct","fields":[{"name":"id","type":"integer","nullable":true,"metadata":{}}]}).to_string()}}).to_string().into_bytes()
            } else {
                format!("fixture {n} {name}").into_bytes()
            };
            fs::write(root.join(&path), &data).unwrap();
            files.push(
                json!({"path":path,"bytes":data.len(),"sha256":hex::encode(Sha256::digest(&data))}),
            );
        }
        tables.push(json!({"oid":oid,"schema":"public","name":name,"path":oid.to_string(),"version":0,"rows":n,"columns":[{"name":"id","arrow_type":"int32","nullable":true}]}));
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

fn setup() -> (tempfile::TempDir, Store, Context, Publication) {
    let (dir, mut store, owner, _, branch) = fixture();
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let export = complete_export(&mut store, owner.runtime_project_id, branch, 1);
    let epoch = publish(&mut store, &mut publisher, owner.runtime_project_id, export);
    let n = Namespace {
        deployment_id: owner.deployment_id,
        project_id: owner.runtime_project_id,
        provider_id: ProjectId::new().to_string(),
        metastore_id: ProjectId::new().to_string(),
        catalog: "sb_test".into(),
        schema: "analytics".into(),
        catalog_id: Some(ProjectId::new().to_string()),
        schema_id: Some(ProjectId::new().to_string()),
        state: "ready".into(),
    };
    let v = preview(&store, &owner, n.clone(), epoch).unwrap();
    let p = Publication {
        id: OperationId::new(),
        deployment_id: owner.deployment_id,
        project_id: owner.runtime_project_id,
        branch_id: branch,
        epoch_id: epoch,
        key: "publish".into(),
        request_hash: "a".repeat(64),
        preview_hash: v["preview_hash"].as_str().unwrap().into(),
        source_revision: store.branch(branch).unwrap().revision,
        expected_binding_revision: 0,
        revision: None,
        namespace: n,
        tables: v["tables"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| Table {
                id: OperationId::new(),
                source_schema: t["source_schema"].as_str().unwrap().into(),
                source_name: t["source_name"].as_str().unwrap().into(),
                body: t["body"].clone(),
                state: "planned".into(),
            })
            .collect(),
        state: "registering".into(),
        error: None,
        unpublish_key: None,
        unpublish_binding_revision: None,
        snapshot_at_ms: v["snapshot_at_ms"].as_i64(),
        manifest_hash: v["manifest_hash"].as_str().unwrap().into(),
    };
    (dir, store, owner, p)
}
#[test]
fn retention_survives_restart_and_blocks_gc_before_remote_registration() {
    let (_dir, mut store, owner, p) = setup();
    store.begin_catalog_publication(&p).unwrap();
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let export = complete_export(&mut store, p.project_id, p.branch_id, 2);
    publish(&mut store, &mut publisher, p.project_id, export);
    assert!(
        store
            .collect_snapshots(p.project_id, p.branch_id, 1)
            .unwrap()["deleting"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let root = store.root().to_owned();
    drop(store);
    let mut store = Store::open(&root).unwrap();
    Service::recover(&mut store).unwrap();
    assert_eq!(
        store
            .catalog_publication_key(owner.deployment_id, "publish")
            .unwrap()
            .unwrap()
            .id,
        p.id
    );
    assert!(
        store
            .catalog_publication(DeploymentId::new(), p.id)
            .is_err()
    );
    assert!(
        store
            .collect_snapshots(p.project_id, p.branch_id, 1)
            .unwrap()["deleting"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}
#[test]
fn complete_commit_is_atomic_and_stale_refresh_cannot_replace_head() {
    let (_dir, mut store, _, mut p) = setup();
    store.begin_catalog_publication(&p).unwrap();
    assert!(store.commit_catalog_publication(&mut p).is_err());
    assert_eq!(
        store.catalog_head(p.deployment_id, p.branch_id).unwrap(),
        (0, None)
    );
    for t in &mut p.tables {
        t.state = "verified".into();
    }
    store.commit_catalog_publication(&mut p).unwrap();
    assert_eq!(
        store.catalog_head(p.deployment_id, p.branch_id).unwrap(),
        (1, Some(p.id))
    );
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let export = complete_export(&mut store, p.project_id, p.branch_id, 2);
    let epoch = publish(&mut store, &mut publisher, p.project_id, export);
    let mut stale = p.clone();
    stale.id = OperationId::new();
    stale.key = "stale".into();
    stale.epoch_id = epoch;
    stale.state = "registering".into();
    stale.revision = None;
    store.begin_catalog_publication(&stale).unwrap();
    assert!(store.commit_catalog_publication(&mut stale).is_err());
    assert_eq!(
        store.catalog_head(p.deployment_id, p.branch_id).unwrap(),
        (1, Some(p.id))
    );
}
#[test]
fn unpublish_blocks_new_references_and_releases_gc_pin_only_after_drain() {
    let (_dir, mut store, _, mut p) = setup();
    store.begin_catalog_publication(&p).unwrap();
    for t in &mut p.tables {
        t.state = "verified".into();
    }
    store.commit_catalog_publication(&mut p).unwrap();
    store
        .pin_catalog_publication(p.deployment_id, p.id, "consumer")
        .unwrap();
    let lease = store.pin_snapshot(p.project_id, p.epoch_id, 60000).unwrap();
    let mut p = store
        .retire_catalog_publication(p.deployment_id, p.id, "remove", 1)
        .unwrap();
    assert_eq!(
        store.catalog_head(p.deployment_id, p.branch_id).unwrap(),
        (2, None)
    );
    assert!(
        store
            .pin_catalog_publication(p.deployment_id, p.id, "late")
            .is_err()
    );
    assert!(store.catalog_references(&p).unwrap());
    assert!(store.finish_catalog_retirement(&mut p).is_err());
    store
        .release_catalog_publication(p.deployment_id, p.id, "consumer")
        .unwrap();
    assert!(store.catalog_references(&p).unwrap());
    store
        .release_snapshot_lease(p.project_id, lease.id)
        .unwrap();
    assert!(store.finish_catalog_retirement(&mut p).is_err());
    for t in &mut p.tables {
        t.state = "deleted".into();
    }
    store.finish_catalog_retirement(&mut p).unwrap();
    let mut publisher = Publisher::recover(&mut store).unwrap();
    let export = complete_export(&mut store, p.project_id, p.branch_id, 2);
    publish(&mut store, &mut publisher, p.project_id, export);
    assert_eq!(
        store
            .collect_snapshots(p.project_id, p.branch_id, 1)
            .unwrap()["deleting"],
        json!([p.epoch_id])
    );
    assert_eq!(
        store
            .retire_catalog_publication(p.deployment_id, p.id, "remove", 1)
            .unwrap()
            .state,
        "retired"
    );
}
#[test]
fn restart_preserves_preassigned_identity_and_reverifies_candidate_set() {
    let (_dir, mut store, _, mut p) = setup();
    p.tables[0].state = "creating".into();
    p.tables[1].state = "verified".into();
    store.begin_catalog_publication(&p).unwrap();
    Service::recover(&mut store).unwrap();
    let saved = store.catalog_publication(p.deployment_id, p.id).unwrap();
    assert_eq!(saved.tables[0].id, p.tables[0].id);
    assert_eq!(saved.tables[0].state, "creating");
    assert_eq!(saved.tables[1].state, "registered");
    assert_eq!(
        store.catalog_head(p.deployment_id, p.branch_id).unwrap(),
        (0, None)
    );
}
#[test]
fn preview_rejects_tampered_schema_and_owner_inputs_are_strict() {
    let (_dir, store, owner, p) = setup();
    let snapshot = store.snapshot(p.project_id, p.epoch_id).unwrap();
    let path = store
        .root()
        .join("analytics/generations")
        .join(snapshot.publication.export_id.to_string())
        .join("101/_delta_log/00000000000000000000.json");
    fs::write(path, b"{}").unwrap();
    assert!(preview(&store, &owner, p.namespace, p.epoch_id).is_err());
    assert!(
        serde_json::from_value::<Command>(
            json!({"action":"preview","epoch_id":p.epoch_id,"project_id":p.project_id})
        )
        .is_err()
    );
}

#[test]
fn crash_child() {
    let Ok(root) = std::env::var("SB_UC03_CRASH_ROOT") else {
        return;
    };
    let root = std::path::PathBuf::from(root);
    let mut p: Publication =
        serde_json::from_slice(&fs::read(root.join("candidate.json")).unwrap()).unwrap();
    let mut store = Store::open(&root.join("data")).unwrap();
    let phase = std::env::var("SB_UC03_CRASH_PHASE").unwrap();
    store.begin_catalog_publication(&p).unwrap();
    if phase != "admitted" {
        p.tables[0].state = "creating".into();
        store.save_catalog_publication(&p).unwrap();
        if phase != "intent" {
            p.tables[0].state = "registered".into();
            store.save_catalog_publication(&p).unwrap();
            if phase != "partial" {
                for t in &mut p.tables {
                    t.state = "verified".into();
                }
                store.save_catalog_publication(&p).unwrap();
                if phase != "verified" {
                    store.commit_catalog_publication(&mut p).unwrap();
                    if phase != "committed" {
                        p = store
                            .retire_catalog_publication(p.deployment_id, p.id, "remove", 1)
                            .unwrap();
                        if phase != "retiring" {
                            for t in &mut p.tables {
                                t.state = "deleted".into();
                            }
                            store.save_catalog_publication(&p).unwrap();
                            if phase == "retired" {
                                store.finish_catalog_retirement(&mut p).unwrap();
                            }
                        }
                    }
                }
            }
        }
    }
    fs::write(root.join("kill-ready"), b"ready").unwrap();
    loop {
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
}
#[test]
fn sigkill_at_each_journal_boundary_preserves_visibility_and_retention() {
    for phase in [
        "admitted",
        "intent",
        "partial",
        "verified",
        "committed",
        "retiring",
        "deleting",
        "retired",
    ] {
        let (dir, store, _, p) = setup();
        fs::write(
            dir.path().join("candidate.json"),
            serde_json::to_vec(&p).unwrap(),
        )
        .unwrap();
        drop(store);
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "catalog::publication::tests::crash_child",
                "--nocapture",
            ])
            .env("SB_UC03_CRASH_ROOT", dir.path())
            .env("SB_UC03_CRASH_PHASE", phase)
            .stdout(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while !dir.path().join("kill-ready").exists() {
            assert!(
                child.try_wait().unwrap().is_none(),
                "child failed before {phase}"
            );
            if std::time::Instant::now() > deadline {
                child.kill().unwrap();
                child.wait().unwrap();
                panic!("crash fixture timeout at {phase}");
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        child.kill().unwrap();
        assert!(!child.wait().unwrap().success());
        let mut store = Store::open(&dir.path().join("data")).unwrap();
        Service::recover(&mut store).unwrap();
        let recovered = store.catalog_publication(p.deployment_id, p.id).unwrap();
        assert_eq!(recovered.tables[0].id, p.tables[0].id);
        let head = store.catalog_head(p.deployment_id, p.branch_id).unwrap();
        assert_eq!(
            head.1,
            if phase == "committed" {
                Some(p.id)
            } else {
                None
            },
            "{phase}"
        );
        let db = rusqlite::Connection::open(store.root().join("state.sqlite3")).unwrap();
        let retained: i64 = db
            .query_row(
                "SELECT count(*) FROM catalog_retention WHERE publication_id=?1",
                [p.id.to_string()],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(retained, if phase == "retired" { 0 } else { 1 }, "{phase}");
    }
}
