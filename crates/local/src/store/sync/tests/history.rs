use super::continuous::{captured, policy};
use super::incremental::{finish, prepare};
use super::*;
use crate::incremental::{Command as ApplyCommand, Run as Apply};
use supabricks_core::resource::EpochId;

fn now() -> i64 {
    super::super::super::now_ms().unwrap()
}

// Build valid completed history from an actually published managed run. Keep all
// publications and available snapshots: execution retention must not collect data.
pub(super) fn history(s: &mut Store, p: &Policy, count: usize) -> Vec<(Run, Apply)> {
    let c = captured(s, p);
    s.schedule_continuous(now()).unwrap();
    let parent = s.active_sync_runs().unwrap()[0].id;
    for version in 0..2 {
        s.tick_triggered(now()).unwrap();
        let id = s.sync_run(p.project_id, parent).unwrap().apply_id.unwrap();
        let mut a = s.incremental_run(p.project_id, id).unwrap();
        prepare(s, &c, &mut a, version);
        finish(s, &a);
        s.reconcile_sync(now()).unwrap();
    }
    let template = s.sync_run(p.project_id, parent).unwrap();
    let apply = s
        .incremental_run(p.project_id, template.published_artifact_id.unwrap())
        .unwrap();
    let publication = s.publication(apply.id).unwrap();
    append_history(s, p, &template, &apply, &publication, count - 2)
}

fn append_history(
    s: &Store,
    p: &Policy,
    template: &Run,
    apply: &Apply,
    publication: &crate::store::Publication,
    count: usize,
) -> Vec<(Run, Apply)> {
    let mut result = Vec::new();
    s.db.execute_batch("SAVEPOINT seed_history").unwrap();
    for _ in 0..count {
        let mut r = template.clone();
        let mut a = apply.clone();
        r.id = OperationId::new();
        a.id = OperationId::new();
        a.epoch_id = EpochId::new();
        a.sync_run_id = Some(r.id);
        r.published_artifact_id = Some(a.id);
        r.epoch_id = Some(a.epoch_id.to_string());
        s.db.execute("INSERT INTO sync_runs(id,policy_id,project_id,branch_id,state,record) VALUES (?1,?2,?3,?4,'succeeded',?5)", params![r.id.to_string(),p.id.to_string(),p.project_id.to_string(),p.branch_id.to_string(),json!(r).to_string()]).unwrap();
        s.db.execute("INSERT INTO analytical_artifacts(id,project_id,branch_id,kind) VALUES (?1,?2,?3,'incremental')",params![a.id.to_string(),p.project_id.to_string(),p.branch_id.to_string()]).unwrap();
        let mut descriptor = publication.descriptor.clone().unwrap();
        descriptor["export_id"] = json!(a.id);
        descriptor["epoch_id"] = json!(a.epoch_id);
        s.db.execute("INSERT INTO publications(export_id,epoch_id,branch_id,source_revision,export_order,requested_at_ms,published_at_ms,state,descriptor) SELECT ?1,?2,?3,?4,ordinal,?5,?5,'published',?6 FROM analytical_artifacts WHERE id=?1", params![a.id.to_string(),a.epoch_id.to_string(),a.branch_id.to_string(),a.source_revision,now(),descriptor.to_string()]).unwrap();
        s.db.execute(
            "INSERT INTO epochs VALUES (?1,?2,?3)",
            params![
                a.epoch_id.to_string(),
                a.branch_id.to_string(),
                a.target_lsn
            ],
        )
        .unwrap();
        s.db.execute(
            "INSERT INTO snapshots VALUES (?1,?2,'available',NULL)",
            params![a.epoch_id.to_string(), a.id.to_string()],
        )
        .unwrap();
        s.db.execute(
            "INSERT INTO incremental_runs VALUES (?1,?2,'succeeded',?3)",
            params![
                a.id.to_string(),
                a.capture_id.to_string(),
                json!(a).to_string()
            ],
        )
        .unwrap();
        let key = format!("internal:triggered:{}:0", r.id);
        let request = json!({"command":ApplyCommand::Apply{capture_id:a.capture_id,key:key.clone()},"target":a.target_lsn,"owner":r.id});
        let mut response = json!(a);
        response["state"] = json!("requested");
        s.db.execute(
            "INSERT INTO incremental_requests VALUES (?1,?2,?3,?4)",
            params![
                a.project_id.to_string(),
                key,
                request.to_string(),
                response.to_string()
            ],
        )
        .unwrap();
        result.push((r, a));
    }
    s.db.execute_batch("RELEASE seed_history").unwrap();
    result
}

#[test]
fn continuous_admits_after_1024_successful_batches() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    history(&mut s, &policy, 1024);
    let mut c = captured(&s, &policy);
    c.captured_lsn = Some("0/200".into());
    c.progress = Some(json!({"last_data_lsn":"0/200"}));
    s.save_capture(&c).unwrap();
    s.schedule_continuous(now() + 501).unwrap();
    let id = s.active_sync_runs().unwrap()[0].id;
    s.tick_triggered(now()).unwrap();
    let r = s.sync_run(p, id).unwrap();
    assert_eq!(r.state, "running", "{:?}", r.error);
    let mut a = s.incremental_run(p, r.apply_id.unwrap()).unwrap();
    let (request,response):(String,String)=s.db.query_row("SELECT request,response FROM incremental_requests WHERE json_extract(response,'$.id')=?1",[a.id.to_string()],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();
    s.retain_incremental_history().unwrap();
    let request: Value = serde_json::from_str(&request).unwrap();
    let replay = s
        .incremental_command_at(
            p,
            serde_json::from_value(request["command"].clone()).unwrap(),
            request["target"].as_str(),
            Some(id),
        )
        .unwrap();
    assert_eq!(replay, serde_json::from_str::<Value>(&response).unwrap());
    prepare(&mut s, &c, &mut a, 2);
    // The real capture updates this heartbeat independently while the publisher
    // recovers. Simulate that after scanning the deliberately large history.
    let mut publisher = crate::analytics::Publisher::recover(&mut s).unwrap();
    c.observed_at_ms = Some(now());
    s.save_capture(&c).unwrap();
    for _ in 0..10 {
        publisher.tick(&mut s).unwrap();
        if s.incremental_run(p, a.id).unwrap().state == "succeeded" {
            break;
        }
    }
    assert_eq!(s.incremental_run(p, a.id).unwrap().state, "succeeded");
    s.reconcile_sync(now()).unwrap();
    assert_eq!(s.sync_run(p, id).unwrap().state, "succeeded");
}

fn count(s: &Store, table: &str) -> i64 {
    s.db.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
        .unwrap()
}

#[test]
fn history_preserves_readers_provenance_requests_and_restart() {
    let (dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let rows = history(&mut s, &policy, 1024);
    let (parent, retired) = &rows[0];
    let lease = s.pin_snapshot(p, retired.epoch_id, 60000).unwrap();
    let (_, public) = &rows[1];
    let command = ApplyCommand::Cancel {
        id: public.id,
        key: "public-retry".into(),
    };
    let receipt = s.incremental_command(p, command.clone()).unwrap();
    let publications = count(&s, "publications");
    let snapshots = count(&s, "snapshots");
    let audit = count(&s, "security_audit");
    s.retain_incremental_history().unwrap();
    assert!(s.incremental_run(p, retired.id).is_err());
    assert!(s.sync_run(p, parent.id).is_err());
    assert_eq!(s.snapshot(p, retired.epoch_id).unwrap().state, "available");
    assert_eq!(
        s.sync_export_policy(retired.id).unwrap().unwrap().id,
        policy.id
    );
    assert!(s.renew_snapshot_lease(p, lease.id, 60000).is_ok());
    assert_eq!(count(&s, "publications"), publications);
    assert_eq!(count(&s, "snapshots"), snapshots);
    assert_eq!(count(&s, "security_audit"), audit);
    assert!(count(&s, "incremental_runs") < 1024);
    assert!(s.incremental_run(p, public.id).is_ok());
    // A stale private retry cannot manufacture another application, even after
    // its receipt and parent expire. Public requests retain exact responses.
    let stale = ApplyCommand::Apply {
        capture_id: retired.capture_id,
        key: format!("internal:triggered:{}:0", parent.id),
    };
    assert!(
        s.incremental_command_at(p, stale, Some(&retired.target_lsn), Some(parent.id))
            .is_err()
    );
    drop(s);
    let mut s = Store::open(dir.path()).unwrap();
    assert_eq!(s.incremental_command(p, command).unwrap(), receipt);
    assert!(
        s.incremental_command(
            p,
            ApplyCommand::Cancel {
                id: retired.id,
                key: "public-retry".into()
            }
        )
        .is_err()
    );
    assert_eq!(
        s.sync_export_policy(retired.id).unwrap().unwrap().id,
        policy.id
    );
    assert!(s.snapshot(p, retired.epoch_id).is_ok());
    assert!(
        !s.db
            .prepare("PRAGMA foreign_key_check")
            .unwrap()
            .exists([])
            .unwrap()
    );
}

#[test]
fn retention_protects_unfinished_failed_unmanaged_and_pending_cleanup() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let rows = history(&mut s, &policy, 1024);
    let update = |s: &Store, index: usize, state: &str| {
        s.db.execute(
            "UPDATE incremental_runs SET state=?2,record=json_set(record,'$.state',?2) WHERE id=?1",
            params![rows[index].1.id.to_string(), state],
        )
        .unwrap();
    };
    update(&s, 0, "running");
    update(&s, 1, "failed");
    update(&s, 2, "cancelled");
    s.db.execute(
        "UPDATE incremental_runs SET record=json_set(record,'$.sync_run_id',NULL) WHERE id=?1",
        [rows[3].1.id.to_string()],
    )
    .unwrap();
    s.db.execute("UPDATE sync_runs SET state='running',record=json_set(record,'$.state','running') WHERE id=?1",[rows[4].0.id.to_string()]).unwrap();
    let work = s
        .root()
        .join("analytics/apply-work")
        .join(rows[5].1.id.to_string());
    std::fs::create_dir_all(&work).unwrap();
    s.db.execute(
        "INSERT INTO native_processes VALUES (?1,'{}')",
        [format!("incremental-{}", rows[6].1.id)],
    )
    .unwrap();
    s.db.execute(
        "INSERT INTO analytics_gc VALUES (?1,NULL,'pending')",
        [rows[7].1.id.to_string()],
    )
    .unwrap();
    // Even a prefix that looks internal is a user request unless its structured
    // owner and command binding match the managed run.
    s.db.execute("UPDATE incremental_requests SET request=json_remove(request,'$.owner') WHERE json_extract(response,'$.id')=?1",[rows[8].1.id.to_string()]).unwrap();
    let link = s
        .root()
        .join("analytics/apply-work")
        .join(rows[9].1.id.to_string());
    std::os::unix::fs::symlink("absent", &link).unwrap();
    s.retain_incremental_history().unwrap();
    for (_, a) in &rows[..10] {
        assert!(s.incremental_run(p, a.id).is_ok(), "{}", a.id);
    }
    assert!(s.incremental_run(p, rows[10].1.id).is_err());
    assert!(work.exists());
    assert!(link.is_symlink());
    assert_eq!(count(&s, "native_processes"), 1);
    // The current publication, active writer's source epoch and every retained
    // reader stay backed by exactly the same publications and snapshots.
    assert_eq!(count(&s, "publications"), 1024);
    assert_eq!(count(&s, "snapshots"), 1024);
}

#[test]
fn history_cleanup_rolls_back_receipts_and_rows_together() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    history(&mut s, &policy, 1024);
    let receipts = count(&s, "incremental_requests");
    s.db.execute_batch("CREATE TEMP TRIGGER fail_retention BEFORE DELETE ON incremental_runs BEGIN SELECT RAISE(ABORT,'injected retention failure'); END;").unwrap();
    assert!(s.retain_incremental_history().is_err());
    assert_eq!(count(&s, "incremental_runs"), 1024);
    assert_eq!(count(&s, "incremental_requests"), receipts);
    assert_eq!(count(&s, "sync_runs"), 1023);
    s.db.execute_batch("DROP TRIGGER fail_retention").unwrap();
    s.retain_incremental_history().unwrap();
    assert_eq!(count(&s, "incremental_runs"), 896);
    assert_eq!(count(&s, "incremental_requests"), receipts - 128);
}

#[test]
fn exhausted_protected_history_reports_the_specific_budget() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    history(&mut s, &policy, 1024);
    s.db.execute(
        "UPDATE incremental_runs SET state='failed',record=json_set(record,'$.state','failed')",
        [],
    )
    .unwrap();
    let mut c = captured(&s, &policy);
    c.captured_lsn = Some("0/200".into());
    c.progress = Some(json!({"last_data_lsn":"0/200"}));
    s.save_capture(&c).unwrap();
    s.schedule_continuous(now() + 501).unwrap();
    let parent = s.active_sync_runs().unwrap()[0].id;
    s.tick_triggered(now()).unwrap();
    let r = s.sync_run(p, parent).unwrap();
    assert_eq!(r.error.as_deref(), Some("incremental_run_budget_exhausted"));
    assert_eq!(count(&s, "incremental_runs"), 1024);
}

#[test]
fn continuous_history_rotates_past_receipt_and_parent_budgets() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let rows = history(&mut s, &policy, 768);
    let (parent, a) = rows.last().unwrap();
    let publication = s.publication(a.id).unwrap();
    let start = std::time::Instant::now();
    for _ in 0..80 {
        s.retain_incremental_history().unwrap();
        append_history(&s, &policy, parent, a, &publication, 128);
        assert!(count(&s, "incremental_runs") <= 768);
        assert!(count(&s, "incremental_requests") <= 768);
        assert!(count(&s, "sync_runs") <= 896);
    }
    println!(
        "history rotation: {} durable publications, {:?}",
        count(&s, "publications"),
        start.elapsed()
    );
    assert_eq!(count(&s, "publications"), 11008);
    assert_eq!(count(&s, "snapshots"), 11008);
    let mut c = captured(&s, &policy);
    c.captured_lsn = Some("0/200".into());
    c.progress = Some(json!({"last_data_lsn":"0/200"}));
    s.save_capture(&c).unwrap();
    s.schedule_continuous(now() + 501).unwrap();
    let id = s.active_sync_runs().unwrap()[0].id;
    s.tick_triggered(now()).unwrap();
    assert_eq!(s.sync_run(p, id).unwrap().state, "running");
}

#[test]
fn parent_history_keeps_user_receipts_and_recent_status() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let rows = history(&mut s, &policy, 1024);
    let (r, _) = &rows[0];
    // A user request naming an automatic run keeps its status addressable.
    s.db.execute(
        "INSERT INTO sync_requests VALUES (?1,'retained','{}',?2)",
        params![p.to_string(), json!(r).to_string()],
    )
    .unwrap();
    let (manual, _) = &rows[1];
    s.db.execute(
        "UPDATE sync_runs SET record=json_set(record,'$.trigger','manual') WHERE id=?1",
        [manual.id.to_string()],
    )
    .unwrap();
    s.retain_incremental_history().unwrap();
    assert!(s.sync_run(p, r.id).is_ok());
    assert!(s.sync_run(p, manual.id).is_ok());
    assert!(s.sync_run(p, rows[2].0.id).is_err());
    assert!(s.sync_run(p, rows.last().unwrap().0.id).is_ok());
}

#[test]
fn catalog_29_upgrade_preserves_receipts_and_adds_lookup_indexes() {
    let (dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let rows = history(&mut s, &policy, 4);
    let original: Vec<(String, String)> =
        s.db.prepare("SELECT request,response FROM incremental_requests ORDER BY rowid")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
    s.db.execute_batch("DROP INDEX publication_storage_generation; DROP INDEX incremental_receipt_run; DROP INDEX incremental_receipt_owner; DROP INDEX sync_receipt_run; DROP INDEX sync_request_run; PRAGMA user_version=29;").unwrap();
    drop(s);
    assert!(Store::open(dir.path()).is_err(), "upgrade must be explicit");
    let mut db = rusqlite::Connection::open(dir.path().join("state.sqlite3")).unwrap();
    crate::store::migrations::catalog_upgrade(&mut db, 29, "verified-backup", "candidate-release")
        .unwrap();
    drop(db);
    let s = Store::open(dir.path()).unwrap();
    assert_eq!(
        s.db.query_row("PRAGMA user_version", [], |r| r.get::<_, u32>(0))
            .unwrap(),
        crate::store::SCHEMA_VERSION
    );
    assert!(s.incremental_run(p, rows[0].1.id).is_ok());
    let restored: Vec<(String, String)> =
        s.db.prepare("SELECT request,response FROM incremental_requests ORDER BY rowid")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
    assert_eq!(restored, original);
    assert_eq!(s.db.query_row("SELECT count(*) FROM sqlite_master WHERE type='index' AND name IN ('incremental_receipt_run','incremental_receipt_owner','sync_receipt_run','sync_request_run')",[],|r|r.get::<_,i64>(0)).unwrap(),4);
}

#[test]
fn retained_root_lookup_does_not_scan_unrelated_publication_history() {
    let (_dir, mut s, p, d, b) = setup();
    let policy = policy(&mut s, p, d, b);
    let rows = history(&mut s, &policy, 2048);
    let root = captured(&s, &policy).id;
    let absent = OperationId::new();
    let steps = |s: &Store| {
        let mut q =
            s.db.prepare(crate::store::incremental::ROOT_REFERENCED)
                .unwrap();
        assert!(!q.exists([absent.to_string()]).unwrap());
        q.get_status(rusqlite::StatementStatus::VmStep)
    };
    let indexed = steps(&s);
    assert!(s.incremental_root_referenced(root).unwrap());
    // A descriptor update must move its index entry atomically. Unavailable and
    // deleting snapshots remain roots; only completed deletion releases them.
    let moved = OperationId::new();
    let a = &rows.last().unwrap().1;
    s.db.execute("UPDATE publications SET descriptor=json_set(descriptor,'$.generation',?2) WHERE export_id=?1",
        params![a.id.to_string(), format!("analytics/incremental/{moved}")]).unwrap();
    for state in ["available", "unavailable", "deleting", "deleted"] {
        s.db.execute(
            "UPDATE snapshots SET state=?2 WHERE epoch_id=?1",
            params![a.epoch_id.to_string(), state],
        )
        .unwrap();
        assert_eq!(
            s.incremental_root_referenced(moved).unwrap(),
            state != "deleted"
        );
    }
    s.db.execute_batch("DROP INDEX publication_storage_generation")
        .unwrap();
    let scanned = steps(&s);
    println!(
        "root lookup on 2048 retained publications: scan={scanned} VM steps, indexed={indexed}"
    );
    assert!(
        indexed < 100,
        "indexed absent-root lookup must stay bounded: {indexed}"
    );
    assert!(
        scanned > indexed * 100,
        "regression fixture must expose the historical scan"
    );
}
