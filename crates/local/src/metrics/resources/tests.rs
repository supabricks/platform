use super::*;
use std::{
    io::{BufRead, BufReader, Write},
    os::unix::process::CommandExt,
    process::{Command, Stdio},
};
struct Child(std::process::Child);
impl Drop for Child {
    fn drop(&mut self) {
        unsafe {
            libc::kill(-(self.0.id() as i32), libc::SIGKILL);
        }
        let _ = self.0.wait();
    }
}
fn spawn(script: &str) -> (Child, OwnedProcess) {
    let token = supabricks_core::resource::OperationId::new().to_string();
    let child = Child(
        Command::new("/bin/sh")
            .args(["-c", script])
            .env("SUPABRICKS_PROCESS_TOKEN", &token)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .process_group(0)
            .spawn()
            .unwrap(),
    );
    let id = identity::identity(child.0.id()).unwrap().unwrap();
    let record = OwnedProcess {
        root: "/unused".into(),
        generation: 1,
        role: "compute-test".into(),
        pid: child.0.id(),
        start_identity: id.start,
        token,
        branch: None,
    };
    (child, record)
}
fn stable(root: &OwnedProcess, started: Instant) -> Observation {
    let until = Instant::now() + Duration::from_secs(3);
    loop {
        let result = collect(Some(root.clone()), started);
        if result.raw.is_some() {
            return result;
        }
        assert!(Instant::now() < until, "{}", result.status);
        thread::sleep(Duration::from_millis(10));
    }
}
#[test]
fn owned_tree_includes_children_and_refuses_stale_or_unowned_roots() {
    let (mut child, root) = spawn("sleep 20 & echo ready; wait");
    let started = Instant::now();
    let mut reader = BufReader::new(child.0.stdout.take().unwrap());
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    let observation = stable(&root, started);
    let raw = observation.raw.unwrap();
    assert_eq!(raw.process_count, 2);
    assert!(raw.rss_sum_bytes > 0);
    let mut stale = root.clone();
    stale.start_identity.push_str("-stale");
    assert_eq!(
        collect(Some(stale), started).status,
        "compute_identity_changed"
    );
    let mut unowned = root;
    unowned.token.push_str("-wrong");
    assert_eq!(
        collect(Some(unowned), started).status,
        "compute_identity_changed"
    );
    assert_eq!(
        collect(None, started).status,
        "compute_identity_unavailable"
    );
}
#[test]
fn reaped_child_cpu_is_preserved_between_samples() {
    let (mut child, root) = spawn(
        "echo ready; read go; /bin/sh -c 'i=0; while [ $i -lt 150000 ]; do i=$((i+1)); done'; echo done; read quit",
    );
    let started = Instant::now();
    let mut reader = BufReader::new(child.0.stdout.take().unwrap());
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    let before = stable(&root, started).raw.unwrap();
    child.0.stdin.as_mut().unwrap().write_all(b"go\n").unwrap();
    line.clear();
    reader.read_line(&mut line).unwrap();
    assert_eq!(line.trim(), "done");
    let after = stable(&root, started).raw.unwrap();
    assert_eq!(after.process_count, 1);
    assert!(after.cpu_ns > before.cpu_ns + 10_000_000);
    assert!(os::read(root.pid).unwrap().reaped_cpu_ns > 0);
}
fn sample(elapsed_ns: u64, cpu_ns: u64) -> Sample {
    Sample {
        at_ms: 0,
        generation: 1,
        revision: 1,
        status: "available".into(),
        raw: None,
        compute: Some(Observation {
            status: "available".into(),
            at_ms: 0,
            raw: Some(Counters {
                root_identity: "root".into(),
                elapsed_ns,
                cpu_ns,
                rss_sum_bytes: 4096,
                process_count: 2,
                collection_ns: 5000,
            }),
        }),
    }
}
#[test]
fn cpu_uses_monotonic_time_and_fences_restarts_gaps_and_regressions() {
    let a = sample(1_000_000_000, 100_000_000);
    let b = sample(31_000_000_000, 15_100_000_000);
    assert_eq!(point(&b, Some(&a))["cpu_cores"], 0.5);
    assert_eq!(point(&b, Some(&a))["cpu_interval_ms"], 30_000.);
    assert_eq!(point(&b, None)["rss_sum_bytes"], 4096);
    assert!(point(&b, None)["cpu_cores"].is_null());
    for change in 0..6 {
        let mut b = b.clone();
        match change {
            0 => b.generation += 1,
            1 => b.revision += 1,
            2 => {
                b.compute
                    .as_mut()
                    .unwrap()
                    .raw
                    .as_mut()
                    .unwrap()
                    .root_identity = "reused".into()
            }
            3 => b.compute.as_mut().unwrap().raw.as_mut().unwrap().cpu_ns = 0,
            4 => b.compute.as_mut().unwrap().raw.as_mut().unwrap().elapsed_ns = 99_000_000_000,
            _ => b.compute = None,
        };
        assert!(point(&b, Some(&a))["cpu_cores"].is_null(), "{change}");
    }
    let mut old = serde_json::to_value(&a).unwrap();
    old.as_object_mut().unwrap().remove("compute");
    let legacy: Sample = serde_json::from_value(old).unwrap();
    assert_eq!(point(&legacy, None)["status"], "not_collected");
    assert!(serde_json::to_string(&b).unwrap().len() < 2048);
}
#[test]
fn traversal_budget_is_enforced_before_any_probe() {
    let result = walk(
        std::process::id(),
        None,
        unsafe { libc::geteuid() },
        Instant::now(),
        &mut HashSet::new(),
    );
    assert_eq!(result.unwrap_err(), "probe_budget_exceeded");
}
