use serde_json::json;
use std::{fs, time::Duration};
use supabricks_core::resource::*;
use supabricks_local::{
    operations::{Mutation, Ports, Status},
    project::ProjectConfig,
    store::{Epoch, ProcessRecord, Store, TableMapping},
};

fn temp_root() -> tempfile::TempDir {
    use std::os::unix::fs::PermissionsExt;
    let root = tempfile::Builder::new()
        .prefix("sb-p02-")
        .tempdir_in("/tmp")
        .unwrap();
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
    root
}

fn config() -> ProjectConfig {
    ProjectConfig {
        format_version: 1,
        id: ProjectId::new(),
        name: "project".into(),
    }
}
fn create(name: &str, base: u16) -> Mutation {
    Mutation::CreateBranch {
        name: name.into(),
        parent_id: None,
        ports: Ports {
            sql: base,
            external_http: base + 1,
            internal_http: base + 2,
        },
    }
}
fn finish(store: &mut Store, id: OperationId) {
    while let Some(ticket) = store.ticket(id).unwrap() {
        // A worker can obtain a SQLite write lock here: dispatch holds no SQL
        // transaction. Production workers never open this connection.
        let db = rusqlite::Connection::open(store.root().join("state.sqlite3")).unwrap();
        db.execute_batch("BEGIN IMMEDIATE; ROLLBACK;").unwrap();
        let result = json!({"key":ticket.idempotency_key()});
        store.checkpoint(&ticket, result.clone()).unwrap();
        store.checkpoint(&ticket, result).unwrap();
    }
}
#[test]
fn identities_credentials_worktrees_and_reservations_survive_restart() {
    let root = temp_root();
    let a = temp_root();
    let b = temp_root();
    let project = ProjectConfig::initialize(a.path(), "Project").unwrap();
    assert_eq!(
        ProjectConfig::initialize(a.path(), "project").unwrap(),
        project
    );
    fs::copy(
        a.path().join("supabricks.toml"),
        b.path().join("supabricks.toml"),
    )
    .unwrap();
    let mut store = Store::open(root.path()).unwrap();
    store.register_project(&project).unwrap();
    let one = store
        .submit(project.id, "one", create("Main", 5400))
        .unwrap();
    assert_eq!(
        store
            .submit(project.id, "one", create("main", 5400))
            .unwrap()
            .id,
        one.id
    );
    assert!(
        store
            .submit(project.id, "one", create("other", 5400))
            .is_err()
    );
    assert!(
        store
            .submit(project.id, "collision", create("main", 5500))
            .is_err()
    );
    assert!(
        store
            .submit(project.id, "collision", create("other", 5400))
            .is_err()
    );
    let two = store
        .submit(project.id, "collision", create("other", 5500))
        .unwrap();
    assert_eq!(store.pending().unwrap().len(), 2);
    store
        .select_worktree(a.path(), project.id, one.branch_id)
        .unwrap();
    store
        .select_worktree(b.path(), project.id, two.branch_id)
        .unwrap();
    let before = store.branch(one.branch_id).unwrap();
    let password = store.endpoint_password(before.endpoint.id).unwrap();
    assert!(store.connection(project.id, one.branch_id).is_err());
    finish(&mut store, one.id);
    finish(&mut store, two.id);
    store
        .rename_branch(project.id, one.branch_id, "renamed")
        .unwrap();
    let generation = store.generation();
    drop(store);
    let store = Store::open(root.path()).unwrap();
    assert!(store.generation() > generation);
    assert_eq!(
        store.selected_branch(a.path(), project.id).unwrap(),
        one.branch_id
    );
    assert_eq!(
        store.selected_branch(b.path(), project.id).unwrap(),
        two.branch_id
    );
    assert_eq!(
        store.branch(one.branch_id).unwrap().branch.timeline_id,
        before.branch.timeline_id
    );
    let target = store.connection(project.id, one.branch_id).unwrap();
    assert_eq!(target.password, password);
    assert_eq!(target.port, 5400);
    assert!(store.connection(ProjectId::new(), one.branch_id).is_err());
    let public = fs::read_to_string(a.path().join("supabricks.toml")).unwrap();
    assert!(!public.contains(&password));
    assert!(!public.contains("/tmp"));
    assert!(!public.contains("branch"));
    assert_eq!(store.operation(one.id).unwrap().status, Status::Succeeded);
}
#[test]
fn stale_workers_cannot_revive_a_suspended_or_deleted_branch() {
    let root = temp_root();
    let project = config();
    let mut store = Store::open(root.path()).unwrap();
    store.register_project(&project).unwrap();
    let create_op = store
        .submit(project.id, "create", create("main", 5400))
        .unwrap();
    let stale = store.ticket(create_op.id).unwrap().unwrap();
    let suspended = store
        .submit(
            project.id,
            "suspend",
            Mutation::SetState {
                branch_id: create_op.branch_id,
                expected_revision: 1,
                desired: DesiredState::Suspended,
            },
        )
        .unwrap();
    assert!(store.checkpoint(&stale, json!({})).is_err());
    assert_eq!(
        store.operation(create_op.id).unwrap().status,
        Status::Superseded
    );
    assert!(
        store
            .submit(
                project.id,
                "stale",
                Mutation::SetState {
                    branch_id: create_op.branch_id,
                    expected_revision: 1,
                    desired: DesiredState::Running
                }
            )
            .is_err()
    );
    let old_generation = store.ticket(suspended.id).unwrap().unwrap();
    drop(store);
    let mut store = Store::open(root.path()).unwrap();
    assert!(store.checkpoint(&old_generation, json!({})).is_err());
    finish(&mut store, suspended.id);
    let deletion = store
        .submit(
            project.id,
            "delete",
            Mutation::SetState {
                branch_id: create_op.branch_id,
                expected_revision: 2,
                desired: DesiredState::Deleted,
            },
        )
        .unwrap();
    let ticket = store.ticket(deletion.id).unwrap().unwrap();
    let mut wrong = ticket.clone();
    wrong.step_index += 1;
    assert!(store.checkpoint(&wrong, json!({})).is_err());
    store.checkpoint(&ticket, json!({"stopped":true})).unwrap();
    assert!(store.checkpoint(&ticket, json!({"stopped":false})).is_err());
    assert!(
        store
            .submit(project.id, "reuse", create("main", 5400))
            .is_err()
    );
    drop(store);
    let mut store = Store::open(root.path()).unwrap();
    assert_eq!(store.operation(deletion.id).unwrap().next_step, 1);
    finish(&mut store, deletion.id);
    assert!(store.branch(create_op.branch_id).unwrap().ports.is_none());
    let replacement = store
        .submit(project.id, "reuse", create("main", 5400))
        .unwrap();
    assert_ne!(replacement.branch_id, create_op.branch_id);
    assert_eq!(
        store
            .submit(project.id, "create", create("main", 5400))
            .unwrap()
            .id,
        create_op.id
    );
}
#[test]
fn epochs_leases_and_process_evidence_are_retained_and_fenced() {
    let root = temp_root();
    let project = config();
    let mut store = Store::open(root.path()).unwrap();
    store.register_project(&project).unwrap();
    let op = store
        .submit(project.id, "one", create("main", 5400))
        .unwrap();
    finish(&mut store, op.id);
    let epoch = Epoch {
        id: EpochId::new(),
        branch_id: op.branch_id,
        source_lsn: "1/AB".parse().unwrap(),
        tables: vec![TableMapping {
            source_oid: 42,
            table_name: "public.orders".into(),
            object_path: "snapshots/orders".into(),
        }],
    };
    store.put_epoch(&epoch).unwrap();
    store.put_epoch(&epoch).unwrap();
    let mut changed = epoch.clone();
    changed.tables[0].object_path = "different".into();
    assert!(store.put_epoch(&changed).is_err());
    let lease = store
        .acquire_lease(
            op.branch_id,
            Some(epoch.id),
            "export",
            Duration::from_secs(60),
        )
        .unwrap();
    let endpoint = store.branch(op.branch_id).unwrap().endpoint.id;
    let process = ProcessRecord {
        endpoint_id: endpoint,
        role: "compute".into(),
        generation: store.generation(),
        revision: 1,
        pid: 123,
        process_group: 123,
        start_identity: "boot-id:start-time".into(),
    };
    store.record_process(&process).unwrap();
    store.record_process(&process).unwrap();
    let suspend = Mutation::SetState {
        branch_id: op.branch_id,
        expected_revision: 1,
        desired: DesiredState::Suspended,
    };
    assert!(
        store
            .submit(project.id, "suspend", suspend.clone())
            .is_err()
    );
    drop(store);
    let mut store = Store::open(root.path()).unwrap();
    assert_eq!(store.epoch(epoch.id).unwrap(), epoch);
    assert_eq!(store.leases(op.branch_id).unwrap(), vec![lease.clone()]);
    assert_eq!(store.processes().unwrap(), vec![process.clone()]);
    assert!(store.renew_lease(&lease, Duration::from_secs(60)).is_err());
    let mut replacement = process.clone();
    replacement.generation = store.generation();
    assert!(store.record_process(&replacement).is_err());
    store.release_lease(&lease).unwrap();
    let suspend = store.submit(project.id, "suspend", suspend).unwrap();
    let ticket = store.ticket(suspend.id).unwrap().unwrap();
    assert!(store.checkpoint(&ticket, json!({})).is_err());
    let mut wrong = process.clone();
    wrong.start_identity = "reused pid".into();
    assert!(store.forget_process(&wrong).is_err());
    store.forget_process(&process).unwrap();
    finish(&mut store, suspend.id);
    assert!(
        store
            .acquire_lease(op.branch_id, None, "late", Duration::from_secs(60))
            .is_err()
    );
}
#[test]
fn root_lock_rejects_aliases_and_unsafe_state_files() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let root = temp_root();
    let alias = root.path().join("alias");
    symlink(root.path(), &alias).unwrap();
    let store = Store::open(root.path()).unwrap();
    assert!(Store::open(&alias).is_err());
    drop(store);
    let store = Store::open(&alias).unwrap();
    drop(store);
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o755)).unwrap();
    assert!(Store::open(root.path()).is_err());
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let other = temp_root();
    let target = other.path().join("outside");
    fs::write(&target, "unchanged").unwrap();
    fs::remove_file(root.path().join("state.sqlite3")).unwrap();
    symlink(&target, root.path().join("state.sqlite3")).unwrap();
    assert!(Store::open(root.path()).is_err());
    assert_eq!(fs::read_to_string(target).unwrap(), "unchanged");
}
#[test]
fn project_file_rejects_private_fields_without_overwriting() {
    let directory = temp_root();
    let config = ProjectConfig::initialize(directory.path(), "demo").unwrap();
    assert!(ProjectConfig::initialize(directory.path(), "other").is_err());
    let file = directory.path().join("supabricks.toml");
    let before = fs::read_to_string(&file).unwrap();
    fs::write(&file, format!("{before}\npassword = 'private'\n")).unwrap();
    assert!(ProjectConfig::read(directory.path()).is_err());
    fs::write(&file, toml::to_string(&config).unwrap()).unwrap();
    assert_eq!(ProjectConfig::read(directory.path()).unwrap(), config);
}

#[test]
fn project_boundaries_parent_references_and_epoch_mappings_are_enforced() {
    let root = temp_root();
    let a = config();
    let b = config();
    let mut store = Store::open(root.path()).unwrap();
    store.register_project(&a).unwrap();
    store.register_project(&b).unwrap();
    let first = store
        .submit(a.id, "same-key", create("main", 5400))
        .unwrap();
    let second = store
        .submit(b.id, "same-key", create("main", 5500))
        .unwrap();
    assert_ne!(first.id, second.id);
    let mut child = create("child", 5600);
    if let Mutation::CreateBranch { parent_id, .. } = &mut child {
        *parent_id = Some(first.branch_id);
    }
    assert!(store.submit(b.id, "child", child.clone()).is_err());
    store.submit(a.id, "child", child).unwrap();
    assert!(
        store
            .submit(
                a.id,
                "delete-parent",
                Mutation::SetState {
                    branch_id: first.branch_id,
                    expected_revision: 1,
                    desired: DesiredState::Deleted
                }
            )
            .is_err()
    );
    assert!(
        store
            .submit(
                b.id,
                "cross-project",
                Mutation::SetState {
                    branch_id: first.branch_id,
                    expected_revision: 1,
                    desired: DesiredState::Suspended
                }
            )
            .is_err()
    );
    let mut epoch = Epoch {
        id: EpochId::new(),
        branch_id: first.branch_id,
        source_lsn: "0/FF".parse().unwrap(),
        tables: vec![
            TableMapping {
                source_oid: 2,
                table_name: "b".into(),
                object_path: "b".into(),
            },
            TableMapping {
                source_oid: 1,
                table_name: "a".into(),
                object_path: "a".into(),
            },
        ],
    };
    store.put_epoch(&epoch).unwrap();
    store.put_epoch(&epoch).unwrap();
    epoch.tables.reverse();
    store.put_epoch(&epoch).unwrap();
    assert_eq!(store.epoch(epoch.id).unwrap(), epoch);
    assert!(
        store
            .acquire_lease(
                second.branch_id,
                Some(epoch.id),
                "wrong-branch",
                Duration::from_secs(60)
            )
            .is_err()
    );
    let mut duplicate = epoch.clone();
    duplicate.id = EpochId::new();
    duplicate.tables[1].source_oid = 1;
    assert!(store.put_epoch(&duplicate).is_err());
    assert!(store.epoch(duplicate.id).is_err());
    duplicate.tables[1].source_oid = 2;
    store.put_epoch(&duplicate).unwrap();
}
