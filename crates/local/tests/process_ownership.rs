//! Real process tests: gated execution and orphan/reused-identity recovery.
use std::{
    collections::BTreeMap,
    fs,
    io::Write,
    os::unix::{fs::PermissionsExt, process::CommandExt},
    path::Path,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use supabricks_local::{
    store::Store,
    supervisor::{self, Launch},
};
fn launch(store: &Store, argv: Vec<String>) -> Launch {
    Launch {
        root: store.root().to_owned(),
        generation: store.generation(),
        role: "fixture".into(),
        token: "owned-process-test-unique-token".into(),
        branch: None,
        argv,
        env: BTreeMap::from([("PATH".into(), "/usr/bin:/bin".into())]),
        cwd: store.root().to_owned(),
    }
}
fn gated(launch: &Launch) -> Child {
    let file = launch.root.join("launch.json");
    supervisor::write_json(&file, launch).unwrap();
    Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["child", "--launch"])
        .arg(file)
        .arg("--stdin-gate")
        .env_clear()
        .env("SUPABRICKS_PROCESS_TOKEN", &launch.token)
        .process_group(0)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap()
}
fn wait_file(path: &Path) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !path.exists() {
        assert!(Instant::now() < deadline, "fixture did not execute");
        std::thread::sleep(Duration::from_millis(10));
    }
}
#[test]
fn no_engine_effect_can_happen_before_durable_launch_authorization() {
    let temp = tempfile::tempdir().unwrap();
    fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let mut store = Store::open(temp.path()).unwrap();
    let l = launch(
        &store,
        vec![
            "/bin/sh".into(),
            "-c".into(),
            "touch effect; exec sleep 30".into(),
        ],
    );
    let mut child = gated(&l);
    std::thread::sleep(Duration::from_millis(50));
    assert!(!temp.path().join("effect").exists());
    // Simulate owner death before the commit: EOF must abort the launch.
    drop(child.stdin.take());
    assert!(!child.wait().unwrap().success());
    assert!(!temp.path().join("effect").exists());
    let mut child = gated(&l);
    let record = supervisor::evidence(&l, child.id()).unwrap();
    store.record_native_process(&record).unwrap();
    child.stdin.take().unwrap().write_all(&[1]).unwrap();
    wait_file(&temp.path().join("effect"));
    supervisor::stop(&record).unwrap();
    child.wait().unwrap();
    store.forget_native_process(&record).unwrap();
    assert!(store.native_processes().unwrap().is_empty());
}
#[test]
fn restart_fences_orphans_and_refuses_a_reused_pid_or_unmarked_group() {
    let temp = tempfile::tempdir().unwrap();
    fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let mut store = Store::open(temp.path()).unwrap();
    let l = launch(
        &store,
        vec![
            "/bin/sh".into(),
            "-c".into(),
            "sleep 30 & echo ready > ready; wait".into(),
        ],
    );
    let mut child = gated(&l);
    let record = supervisor::evidence(&l, child.id()).unwrap();
    store.record_native_process(&record).unwrap();
    child.stdin.take().unwrap().write_all(&[1]).unwrap();
    wait_file(&temp.path().join("ready"));
    let mut forged = record.clone();
    forged.start_identity.push_str("-reused");
    assert!(supervisor::stop(&forged).is_err());
    assert!(child.try_wait().unwrap().is_none());
    forged = record.clone();
    forged.token.push_str("-unowned");
    assert!(supervisor::stop(&forged).is_err());
    assert!(child.try_wait().unwrap().is_none());
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(
        !supervisor::members(&record).unwrap().is_empty(),
        "fixture must leave a surviving child"
    );
    drop(store);
    let mut store = Store::open(temp.path()).unwrap();
    assert!(
        store.record_native_process(&record).is_err(),
        "old generation cannot authorize new work"
    );
    assert_eq!(store.native_processes().unwrap(), vec![record.clone()]);
    supervisor::stop(&record).unwrap();
    assert!(supervisor::members(&record).unwrap().is_empty());
    store.forget_native_process(&record).unwrap();
    assert!(store.native_processes().unwrap().is_empty());
    fs::remove_file(temp.path().join("ready")).unwrap();
}
