use super::*;
fn sample(at_ms: i64) -> Sample {
    Sample {
        at_ms,
        generation: 1,
        revision: 1,
        status: "available".into(),
        compute: None,
        raw: Some(Raw {
            database_oid: "5".into(),
            postmaster_start: "boot".into(),
            database_reset: None,
            wal_reset: None,
            commits: 10,
            rollbacks: 2,
            hits: 80,
            reads: 20,
            wal_bytes: 100,
            connections_active: 3,
            connections_idle: 4,
            connections_other: 0,
            database_size_bytes: 4096,
        }),
    }
}
#[test]
fn rates_use_elapsed_time_and_never_bridge_resets_restarts_or_gaps() {
    let a = sample(30_000);
    let mut b = sample(60_000);
    let r = b.raw.as_mut().unwrap();
    r.commits += 60;
    r.rollbacks += 30;
    r.hits += 90;
    r.reads += 10;
    r.wal_bytes += 3000;
    let point = b.point(Some(&a));
    assert_eq!(point["commits_per_second"], 2.);
    assert_eq!(point["rollbacks_per_second"], 1.);
    assert_eq!(point["wal_bytes_per_second"], 100.);
    assert_eq!(point["cache_hit_ratio"], 0.9);
    assert!(a.point(None)["commits_per_second"].is_null());
    assert!(a.point(Some(&a))["commits_per_second"].is_null());
    for change in 0..9 {
        let mut b = b.clone();
        match change {
            0 => b.generation += 1,
            1 => b.revision += 1,
            2 => b.at_ms = 120_001,
            3 => b.raw.as_mut().unwrap().database_reset = Some("reset".into()),
            4 => b.raw.as_mut().unwrap().wal_reset = Some("reset".into()),
            5 => b.raw.as_mut().unwrap().postmaster_start = "new".into(),
            6 => b.raw.as_mut().unwrap().database_oid = "new".into(),
            7 => b.raw.as_mut().unwrap().commits = 0,
            _ => b.raw = None,
        }
        assert!(
            b.point(Some(&a))["commits_per_second"].is_null(),
            "{change}"
        );
    }
    let mut gap = a.clone();
    gap.raw = None;
    assert!(b.point(Some(&gap))["commits_per_second"].is_null());
    let mut quiet = a.clone();
    quiet.at_ms += 30_000;
    let quiet = quiet.point(Some(&a));
    assert_eq!(quiet["commits_per_second"], 0.);
    assert!(quiet["cache_hit_ratio"].is_null());
}
#[test]
fn history_survives_reopen_with_project_isolation_gaps_and_retention() {
    let dir = tempfile::tempdir().unwrap();
    let project = ProjectId::new();
    let branch = BranchId::new();
    let now = crate::identity::now() / INTERVAL_MS * INTERVAL_MS;
    {
        let mut h = history::History::open(dir.path()).unwrap();
        h.append(project, branch, &sample(now - 60_000)).unwrap();
        let mut gap = sample(now - 30_000);
        gap.raw = None;
        gap.status = "suspended".into();
        h.append(project, branch, &gap).unwrap();
        h.append(project, branch, &sample(now)).unwrap();
    }
    let h = history::History::open(dir.path()).unwrap();
    let value = h.read(project, branch, Range::Hour, now).unwrap();
    let points = value["points"].as_array().unwrap();
    assert_eq!(points.len(), 121);
    assert_eq!(points[119]["status"], "suspended");
    assert!(points[119]["connections_active"].is_null());
    assert!(points[120]["commits_per_second"].is_null());
    assert_eq!(points[0]["status"], "missing");
    let other = h.read(ProjectId::new(), branch, Range::Hour, now).unwrap();
    assert!(other["oldest_retained_at_ms"].is_null());
    assert!(
        other["points"]
            .as_array()
            .unwrap()
            .iter()
            .all(|p| p["status"] == "missing")
    );
    let old = h
        .read(project, branch, Range::Week, now + RETENTION_MS + 600_000)
        .unwrap();
    assert!(
        old["points"]
            .as_array()
            .unwrap()
            .iter()
            .all(|p| p["status"] == "missing")
    );
    assert!(old["oldest_retained_at_ms"].is_null());
    assert!(serde_json::to_vec(&old).unwrap().len() < 1024 * 1024);
}
#[test]
fn metrics_authority_and_engine_disabled_sampling() {
    use crate::{
        operations::{Mutation, Ports},
        project::ProjectConfig,
    };
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let mut store = Store::open(dir.path()).unwrap();
    let project = ProjectConfig {
        format_version: 1,
        id: ProjectId::new(),
        name: "metrics".into(),
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
    while let Some(ticket) = store.ticket(op.id).unwrap() {
        store.checkpoint(&ticket, json!({})).unwrap();
    }
    let target = Target {
        branch: op.branch_id,
        revision: store.branch(op.branch_id).unwrap().revision,
    };
    let binding = Binding {
        project_id: project.id,
        worktree: dir.path().into(),
    };
    let mut service = Service::open(&store);
    service.tick(&store, None, false);
    assert_eq!(service.active(), 0);
    let read = |service: &Service, target: Target, binding: &Binding| {
        service.handle(
            &store,
            binding,
            Command::History {
                target,
                range: Range::Hour,
            },
        )
    };
    let value = read(&service, target.clone(), &binding).unwrap();
    assert_eq!(
        value["points"].as_array().unwrap().last().unwrap()["status"],
        "engine_disabled"
    );
    let foreign = Binding {
        project_id: ProjectId::new(),
        worktree: dir.path().into(),
    };
    assert!(read(&service, target.clone(), &foreign).is_err());
    let mut stale = target.clone();
    stale.revision -= 1;
    assert!(read(&service, stale, &binding).is_err());
    rusqlite::Connection::open(dir.path().join("state.sqlite3"))
        .unwrap()
        .execute(
            "INSERT INTO governed_branches VALUES (?1,'ready')",
            [target.branch.to_string()],
        )
        .unwrap();
    assert!(
        service
            .handle(
                &store,
                &binding,
                Command::History {
                    target,
                    range: Range::Hour
                }
            )
            .is_err()
    );
    assert!(eligible(&store).unwrap().is_empty());
}
