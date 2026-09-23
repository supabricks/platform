use super::*;
use crate::{
    analytics::Publisher,
    incremental::{Command as I, Run as Apply},
};
use sha2::{Digest, Sha256};
use std::{fs, process::Command as Process};

fn ready(s: &mut Store, p: ProjectId, d: DeploymentId, b: BranchId) -> crate::capture::Capture {
    let policy = capture_policy(s, p, d, b);
    let mut c = capture_start(s, &policy, "capture");
    c.state = "capturing".into();
    c.bootstrap_lsn = Some("0/C8".into());
    c.captured_lsn = Some("0/12C".into());
    c.observed_at_ms = Some(super::super::super::now_ms().unwrap());
    s.save_capture(&c).unwrap();
    c
}
fn apply(s: &mut Store, c: &crate::capture::Capture, key: &str) -> Apply {
    serde_json::from_value(
        s.incremental_command(
            c.project_id,
            I::Apply {
                capture_id: c.id,
                key: key.into(),
            },
        )
        .unwrap(),
    )
    .unwrap()
}
pub(super) fn prepare(
    s: &mut Store,
    c: &crate::capture::Capture,
    r: &mut Apply,
    version: u64,
) -> serde_json::Value {
    let generation = format!(
        "analytics/incremental/{}",
        r.storage_generation.unwrap_or(c.id)
    );
    let root = s.root().join(&generation);
    let mut files = Vec::new();
    let mut tables = Vec::new();
    for oid in [101, 102] {
        fs::create_dir_all(root.join(format!("tables/{oid}/_delta_log"))).unwrap();
        for v in 0..=version {
            let path = format!("tables/{oid}/_delta_log/{v:020}.json");
            let data = format!("immutable fixture {oid} {v}");
            fs::write(root.join(&path), &data).unwrap();
            files.push(json!({"path":path,"bytes":data.len(),"sha256":hex::encode(Sha256::digest(data.as_bytes()))}));
        }
        tables.push(json!({"oid":oid,"schema":"public","name":format!("t{oid}"),"path":format!("tables/{oid}"),"version":version}));
    }
    let manifest = json!({"format_version":2,"capture_identity":c.identity,"storage_generation":r.storage_generation,"source":{"lsn":r.target_lsn},"tables":tables,"files":files});
    let bytes = serde_json::to_vec(&manifest).unwrap();
    let descriptor = json!({"format_version":2,"installation_id":s.installation_id().unwrap(),"epoch_id":r.epoch_id,"export_id":r.id,"ordinal":s.publication(r.id).unwrap().ordinal,"source_revision":r.source_revision,"generation":generation,"manifest_sha256":hex::encode(Sha256::digest(&bytes)),"manifest":manifest});
    let stage = s.root().join("analytics/staging").join(r.id.to_string());
    fs::create_dir_all(&stage).unwrap();
    fs::write(stage.join("manifest.json"), bytes).unwrap();
    s.incremental_ready(r, &descriptor).unwrap();
    descriptor
}
pub(super) fn finish(s: &mut Store, r: &Apply) {
    let mut publisher = Publisher::recover(s).unwrap();
    for _ in 0..10 {
        publisher.tick(s).unwrap();
        if s.incremental_run(r.project_id, r.id).unwrap().state == "succeeded" {
            return;
        }
    }
    panic!("publication did not finish")
}
fn head(s: &Store, c: &crate::capture::Capture) -> (String, String, String) {
    s.db.query_row("SELECT h.epoch_id,i.epoch_id,i.published_lsn FROM snapshot_heads h JOIN incremental_heads i ON i.capture_id=?1 WHERE h.branch_id=?2",params![c.id.to_string(),c.branch_id.to_string()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap()
}
#[test]
fn verified_incremental_publishes_in_one_turn_without_exceeding_hash_budget() {
    for extra_bytes in [0, 5 * 1024 * 1024] {
        let (_dir, mut s, p, d, b) = setup();
        let c = ready(&mut s, p, d, b);
        let mut run = apply(&mut s, &c, "first");
        let mut descriptor = prepare(&mut s, &c, &mut run, 0);
        if extra_bytes > 0 {
            let path = "tables/101/data.parquet";
            let data = vec![7u8; extra_bytes];
            fs::write(
                s.root()
                    .join(descriptor["generation"].as_str().unwrap())
                    .join(path),
                &data,
            )
            .unwrap();
            descriptor["manifest"]["files"]
                .as_array_mut()
                .unwrap()
                .push(json!({
                    "path": path, "bytes": data.len(), "sha256": hex::encode(Sha256::digest(&data))
                }));
            let bytes = serde_json::to_vec(&descriptor["manifest"]).unwrap();
            descriptor["manifest_sha256"] = json!(hex::encode(Sha256::digest(&bytes)));
            fs::write(
                s.root()
                    .join("analytics/staging")
                    .join(run.id.to_string())
                    .join("manifest.json"),
                bytes,
            )
            .unwrap();
            s.incremental_ready(&mut run, &descriptor).unwrap();
        }
        let mut publisher = Publisher::recover(&mut s).unwrap();
        publisher.tick(&mut s).unwrap();
        if extra_bytes > 0 {
            assert_eq!(s.publication(run.id).unwrap().state, "requested");
            assert!(s.snapshot(p, run.epoch_id).is_err());
            publisher.tick(&mut s).unwrap();
        }
        assert_eq!(s.publication(run.id).unwrap().state, "published");
        assert_eq!(
            head(&s, &c),
            (
                run.epoch_id.to_string(),
                run.epoch_id.to_string(),
                "0/C8".into()
            )
        );
    }
}
#[test]
fn published_prefix_is_still_hashed_but_only_new_files_need_sync() {
    let (_dir, mut s, p, d, b) = setup();
    let c = ready(&mut s, p, d, b);
    let mut first = apply(&mut s, &c, "first");
    prepare(&mut s, &c, &mut first, 0);
    finish(&mut s, &first);
    let mut next = apply(&mut s, &c, "next");
    prepare(&mut s, &c, &mut next, 1);
    let mut publisher = Publisher::recover(&mut s).unwrap();
    let mut verified = 0;
    let mut synced = 0;
    publisher
        .tick_with_hook(&mut s, &mut |at| {
            verified += usize::from(at.starts_with("verified_file:"));
            synced += usize::from(at.starts_with("synced_file:"));
            Ok(())
        })
        .unwrap();
    assert_eq!((verified, synced), (4, 2));
    assert_eq!(s.publication(next.id).unwrap().state, "published");
    let old = head(&s, &c);
    let mut corrupt = apply(&mut s, &c, "corrupt");
    let descriptor = prepare(&mut s, &c, &mut corrupt, 2);
    let path = s
        .root()
        .join(descriptor["generation"].as_str().unwrap())
        .join("tables/101/_delta_log/00000000000000000000.json");
    let mut bytes = fs::read(&path).unwrap();
    bytes[0] ^= 1;
    fs::write(path, bytes).unwrap();
    assert!(publisher.tick(&mut s).is_err());
    assert_eq!(head(&s, &c), old);
}

#[test]
fn admission_cancel_scope_and_restore_fence_writers() {
    let (_dir, mut s, p, d, b) = setup();
    let mut c = ready(&mut s, p, d, b);
    let a = apply(&mut s, &c, "first");
    assert_eq!(a.id, apply(&mut s, &c, "first").id);
    assert_eq!(a.target_lsn, "0/C8");
    assert!(
        s.incremental_command(
            p,
            I::Apply {
                capture_id: c.id,
                key: "overlap".into()
            }
        )
        .is_err()
    );
    assert!(
        s.incremental_command(ProjectId::new(), I::Status { id: a.id })
            .is_err()
    );
    assert!(
        s.incremental_command(
            p,
            I::Cancel {
                id: a.id,
                key: "first".into()
            }
        )
        .is_err()
    );
    s.incremental_command(
        p,
        I::Cancel {
            id: a.id,
            key: "cancel".into(),
        },
    )
    .unwrap();
    assert_eq!(s.capture(p, c.id).unwrap().desired, "fenced");
    assert!(
        s.incremental_command(
            p,
            I::Apply {
                capture_id: c.id,
                key: "fenced".into()
            }
        )
        .is_err()
    );
    c.observed_at_ms = Some(0);
    s.save_capture(&c).unwrap();
    assert!(
        s.incremental_command(
            p,
            I::Apply {
                capture_id: c.id,
                key: "stale".into()
            }
        )
        .is_err()
    );
    c.observed_at_ms = Some(super::super::super::now_ms().unwrap());
    s.save_capture(&c).unwrap();
    let next = apply(&mut s, &c, "restore");
    super::super::super::incremental::restore(&s.db).unwrap();
    assert_eq!(s.incremental_run(p, next.id).unwrap().state, "cancelled");
    assert_eq!(s.publication(next.id).unwrap().state, "cancelled");
}
#[test]
fn group_cursor_commit_rolls_back_on_mapping_failure_and_competing_head() {
    let (_dir, mut s, p, d, b) = setup();
    let c = ready(&mut s, p, d, b);
    let mut first = apply(&mut s, &c, "first");
    prepare(&mut s, &c, &mut first, 0);
    finish(&mut s, &first);
    let old = head(&s, &c);
    let mut regressed = c.clone();
    regressed.captured_lsn = Some("0/64".into());
    s.save_capture(&regressed).unwrap();
    assert!(
        s.incremental_command(
            p,
            I::Apply {
                capture_id: c.id,
                key: "regressed".into()
            }
        )
        .is_err()
    );
    assert_eq!(head(&s, &c), old);
    s.save_capture(&c).unwrap();
    let mut next = apply(&mut s, &c, "next");
    let descriptor = prepare(&mut s, &c, &mut next, 1);
    let mut broken = descriptor.clone();
    broken["manifest"]["tables"][1] = broken["manifest"]["tables"][0].clone();
    assert!(s.commit_incremental(&mut next, &broken).is_err());
    assert_eq!(head(&s, &c), old);
    let count: i64 =
        s.db.query_row(
            "SELECT count(*) FROM epochs WHERE id=?1",
            [next.epoch_id.to_string()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
    let expected = next.expected_head;
    next.expected_head = None;
    assert!(s.commit_incremental(&mut next, &descriptor).is_err());
    assert_eq!(head(&s, &c), old);
    next.expected_head = expected;
    finish(&mut s, &next);
    let published = head(&s, &c);
    assert_eq!(
        published,
        (
            next.epoch_id.to_string(),
            next.epoch_id.to_string(),
            "0/12C".into()
        )
    );
    assert!(s.incremental_root_referenced(c.id).unwrap());
}
#[test]
fn publication_crash_child() {
    let Ok(root) = std::env::var("SB_SY03_TEST_ROOT") else {
        return;
    };
    let point = std::env::var("SB_SY03_TEST_POINT").unwrap();
    let mut s = Store::open(std::path::Path::new(&root)).unwrap();
    let mut publisher = Publisher::recover(&mut s).unwrap();
    for _ in 0..10 {
        publisher
            .tick_with_hook(&mut s, &mut |at| {
                if at == point {
                    unsafe {
                        libc::kill(libc::getpid(), libc::SIGKILL);
                    }
                }
                Ok(())
            })
            .unwrap();
    }
    panic!("crash hook not reached")
}
#[test]
fn sigkill_publication_keeps_group_map_and_cursor_atomic() {
    use std::os::unix::process::ExitStatusExt;
    for point in [
        "files_complete",
        "after_rename",
        "before_commit",
        "after_commit",
    ] {
        let (dir, mut s, p, d, b) = setup();
        let c = ready(&mut s, p, d, b);
        let mut first = apply(&mut s, &c, "first");
        prepare(&mut s, &c, &mut first, 0);
        finish(&mut s, &first);
        let old = head(&s, &c);
        let mut next = apply(&mut s, &c, "next");
        prepare(&mut s, &c, &mut next, 1);
        drop(s);
        let status = Process::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "store::sync::tests::incremental::publication_crash_child",
                "--nocapture",
            ])
            .env("SB_SY03_TEST_ROOT", dir.path())
            .env("SB_SY03_TEST_POINT", point)
            .status()
            .unwrap();
        assert_eq!(status.signal(), Some(libc::SIGKILL), "{point}");
        let mut s = Store::open(dir.path()).unwrap();
        let new = (
            next.epoch_id.to_string(),
            next.epoch_id.to_string(),
            "0/12C".into(),
        );
        assert_eq!(
            head(&s, &c),
            if point == "after_commit" {
                new.clone()
            } else {
                old
            },
            "{point}"
        );
        let mut refreshed = s.capture(p, c.id).unwrap();
        refreshed.observed_at_ms = Some(super::super::super::now_ms().unwrap());
        s.save_capture(&refreshed).unwrap();
        finish(&mut s, &next);
        assert_eq!(head(&s, &c), new);
        assert_eq!(s.snapshot(p, first.epoch_id).unwrap().state, "available");
        let mappings: i64 =
            s.db.query_row(
                "SELECT count(*) FROM table_mappings WHERE epoch_id=?1",
                [next.epoch_id.to_string()],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(mappings, 2);
    }
}

#[test]
fn v2_rejects_unselected_logs_symlinks_and_corrupt_content_without_advancing_head() {
    let (_dir, mut s, p, d, b) = setup();
    let c = ready(&mut s, p, d, b);
    let mut first = apply(&mut s, &c, "first");
    prepare(&mut s, &c, &mut first, 0);
    finish(&mut s, &first);
    let old = head(&s, &c);
    let mut next = apply(&mut s, &c, "next");
    let descriptor = prepare(&mut s, &c, &mut next, 1);
    let mut wrong = descriptor.clone();
    wrong["manifest"]["tables"][0]["version"] = json!(0);
    assert!(crate::analytics_v2::layout(s.root(), &wrong).is_err());
    let root = s.root().join(descriptor["generation"].as_str().unwrap());
    let path = root.join("tables/101/_delta_log/00000000000000000001.json");
    let original = fs::read(&path).unwrap();
    fs::remove_file(&path).unwrap();
    std::os::unix::fs::symlink(
        root.join("tables/101/_delta_log/00000000000000000000.json"),
        &path,
    )
    .unwrap();
    assert!(crate::analytics_v2::layout(s.root(), &descriptor).is_err());
    fs::remove_file(&path).unwrap();
    let mut damaged = original;
    damaged[0] ^= 1;
    fs::write(path, damaged).unwrap();
    let mut publisher = Publisher::recover(&mut s).unwrap();
    assert!(publisher.tick(&mut s).is_err());
    assert_eq!(head(&s, &c), old);
    assert_eq!(s.incremental_run(p, next.id).unwrap().state, "failed");
    assert_eq!(s.capture(p, c.id).unwrap().state, "resync_required");
    assert_eq!(s.snapshot(p, first.epoch_id).unwrap().state, "available");
}

#[test]
fn compaction_admission_is_durable_and_old_roots_wait_for_explicit_unpinned_gc() {
    let (_dir, mut s, p, d, b) = setup();
    let c = ready(&mut s, p, d, b);
    let mut first = apply(&mut s, &c, "first");
    prepare(&mut s, &c, &mut first, 64);
    finish(&mut s, &first);
    let pin = s.pin_snapshot(p, first.epoch_id, 60000).unwrap();
    let mut next = apply(&mut s, &c, "compact");
    assert_eq!(next.storage_generation, Some(next.id));
    assert_eq!(
        apply(&mut s, &c, "compact").storage_generation,
        Some(next.id)
    );
    assert!(s.incremental_root_referenced(c.id).unwrap());
    assert!(s.incremental_root_referenced(next.id).unwrap());
    assert_eq!(
        s.incremental_root_identity(next.id).unwrap(),
        Some(c.identity.clone())
    );
    let descriptor = prepare(&mut s, &c, &mut next, 1);
    let mut wrong = descriptor.clone();
    wrong["manifest"]["storage_generation"] = json!(c.id);
    assert!(crate::analytics_v2::data_root(s.root(), &wrong).is_err());
    assert!(s.commit_incremental(&mut next, &wrong).is_err());
    let mut publisher = Publisher::recover(&mut s).unwrap();
    let mut synced = 0;
    publisher
        .tick_with_hook(&mut s, &mut |at| {
            synced += usize::from(at.starts_with("synced_file:"));
            Ok(())
        })
        .unwrap();
    // Identical relative names in a compacted generation are new files.
    assert_eq!(synced, 4);
    assert_eq!(s.publication(next.id).unwrap().state, "published");
    s.collect_snapshots(p, b, 1).unwrap();
    assert_eq!(s.snapshot(p, first.epoch_id).unwrap().state, "available");
    assert!(s.incremental_root_referenced(c.id).unwrap());
    s.release_snapshot_lease(p, pin.id).unwrap();
    s.collect_snapshots(p, b, 1).unwrap();
    assert!(s.incremental_root_referenced(c.id).unwrap()); // Deletion is not complete yet.
    s.finish_analytics_gc(first.id).unwrap();
    assert!(!s.incremental_root_referenced(c.id).unwrap());
    assert!(s.incremental_root_referenced(next.id).unwrap());
    let third = apply(&mut s, &c, "reuse");
    assert_eq!(third.storage_generation, next.storage_generation);
}
