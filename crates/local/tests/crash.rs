//! Real SIGKILL boundaries around a deterministic, idempotent fake engine.
//! This qualifies the journal protocol, not Neon storage or power-loss safety.
use serde_json::json;
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Child, Command, Stdio},
    sync::mpsc,
    time::Duration,
};
use supabricks_core::resource::*;
use supabricks_local::{
    operations::{Mutation, Ports, Status, Step, WorkTicket},
    project::ProjectConfig,
    store::Store,
};

struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn effect(root: &Path, ticket: &WorkTicket) -> serde_json::Value {
    let timeline = root.join(format!("{}-timeline", ticket.branch_id));
    let compute = root.join(format!("{}-compute", ticket.branch_id));
    match ticket.step {
        Step::EnsureTimeline | Step::StartCompute => {
            let path = if ticket.step == Step::EnsureTimeline {
                timeline
            } else {
                compute
            };
            match OpenOptions::new().write(true).create_new(true).open(path) {
                Ok(mut f) => {
                    f.write_all(ticket.idempotency_key().as_bytes()).unwrap();
                    f.sync_all().unwrap();
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(e) => panic!("{e}"),
            }
        }
        Step::StopCompute | Step::DeleteTimeline => {
            let path = if ticket.step == Step::StopCompute {
                compute
            } else {
                timeline
            };
            match fs::remove_file(path) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => panic!("{e}"),
            }
        }
    }
    json!({"effect":ticket.idempotency_key()})
}
fn finish(store: &mut Store, effects: &Path, id: OperationId) {
    while let Some(ticket) = store.ticket(id).unwrap() {
        let result = effect(effects, &ticket);
        store.checkpoint(&ticket, result).unwrap();
    }
}
fn boundary() {
    println!("P02_CRASH_BOUNDARY");
    std::io::stdout().flush().unwrap();
    loop {
        std::thread::park();
    }
}
#[test]
fn crash_worker() {
    let Some(root) = std::env::var_os("SUPABRICKS_P02_TEST_ROOT") else {
        return;
    };
    let effects = std::env::var_os("SUPABRICKS_P02_TEST_EFFECTS").unwrap();
    let id: OperationId = std::env::var("SUPABRICKS_P02_TEST_OPERATION")
        .unwrap()
        .parse()
        .unwrap();
    let step: usize = std::env::var("SUPABRICKS_P02_TEST_STEP")
        .unwrap()
        .parse()
        .unwrap();
    let point = std::env::var("SUPABRICKS_P02_TEST_POINT").unwrap();
    let mut store = Store::open(Path::new(&root)).unwrap();
    while let Some(ticket) = store.ticket(id).unwrap() {
        if ticket.step_index == step && point == "before_effect" {
            boundary();
        }
        let result = effect(Path::new(&effects), &ticket);
        if ticket.step_index == step && point == "after_effect" {
            boundary();
        }
        store.checkpoint(&ticket, result).unwrap();
        if ticket.step_index == step && point == "after_checkpoint" {
            boundary();
        }
    }
    panic!("crash boundary was never reached");
}
#[test]
fn kill_at_every_create_and_delete_checkpoint_converges_without_duplicates() {
    for deletion in [false, true] {
        for step in 0..2 {
            for point in ["before_effect", "after_effect", "after_checkpoint"] {
                let root = tempfile::Builder::new()
                    .prefix("sb-crash-")
                    .tempdir_in("/tmp")
                    .unwrap();
                let data = root.path().join("state");
                let effects = root.path().join("effects");
                fs::create_dir(&effects).unwrap();
                let mut store = Store::open(&data).unwrap();
                let project = ProjectConfig {
                    format_version: 1,
                    id: ProjectId::new(),
                    name: "crash".into(),
                };
                store.register_project(&project).unwrap();
                let create = store
                    .submit(
                        project.id,
                        "create",
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
                let op = if deletion {
                    finish(&mut store, &effects, create.id);
                    store
                        .submit(
                            project.id,
                            "delete",
                            Mutation::SetState {
                                branch_id: create.branch_id,
                                expected_revision: 1,
                                desired: DesiredState::Deleted,
                            },
                        )
                        .unwrap()
                } else {
                    create
                };
                let before = store.branch(op.branch_id).unwrap();
                drop(store);
                let mut child = ChildGuard(
                    Command::new(std::env::current_exe().unwrap())
                        .args(["--exact", "crash_worker", "--nocapture"])
                        .env("SUPABRICKS_P02_TEST_ROOT", &data)
                        .env("SUPABRICKS_P02_TEST_EFFECTS", &effects)
                        .env("SUPABRICKS_P02_TEST_OPERATION", op.id.to_string())
                        .env("SUPABRICKS_P02_TEST_STEP", step.to_string())
                        .env("SUPABRICKS_P02_TEST_POINT", point)
                        .stdout(Stdio::piped())
                        .stderr(Stdio::inherit())
                        .spawn()
                        .unwrap(),
                );
                let stdout = child.0.stdout.take().unwrap();
                let (send, recv) = mpsc::channel();
                std::thread::spawn(move || {
                    for line in BufReader::new(stdout).lines() {
                        if line.unwrap().contains("P02_CRASH_BOUNDARY") {
                            let _ = send.send(());
                            break;
                        }
                    }
                });
                recv.recv_timeout(Duration::from_secs(15))
                    .expect("worker must reach crash boundary");
                child.0.kill().unwrap();
                child.0.wait().unwrap();
                let mut store = Store::open(&data).unwrap();
                let saved = store.operation(op.id).unwrap();
                assert_eq!(
                    saved.next_step,
                    if point == "after_checkpoint" {
                        step + 1
                    } else {
                        step
                    },
                    "{deletion} {step} {point}"
                );
                finish(&mut store, &effects, op.id);
                assert_eq!(store.operation(op.id).unwrap().status, Status::Succeeded);
                assert!(store.pending().unwrap().is_empty());
                let after = store.branch(op.branch_id).unwrap();
                assert_eq!(before.branch.timeline_id, after.branch.timeline_id);
                assert_eq!(before.endpoint.id, after.endpoint.id);
                assert_eq!(
                    fs::read_dir(&effects).unwrap().count(),
                    if deletion { 0 } else { 2 }
                );
                assert_eq!(after.ports.is_none(), deletion);
            }
        }
    }
}
