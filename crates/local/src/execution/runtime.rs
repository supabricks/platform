use super::*;
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command as Process, Stdio},
    sync::{Arc, Mutex},
    thread::JoinHandle,
    time::{Duration, Instant},
};

pub(crate) const IMAGE: &str =
    "ubuntu@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3";
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Config {
    pub release: PathBuf,
    pub inventory_sha256: String,
    pub tools: PathBuf,
    pub tools_sha256: String,
    pub rootfs: PathBuf,
    pub rootfs_sha256: String,
}
impl Config {
    pub(crate) fn load(root: &Path) -> Result<Self> {
        if !cfg!(all(target_os = "linux", target_arch = "x86_64")) {
            return Err(denied());
        }
        let bytes =
            crate::catalog::config::private_bytes(&root.join("execution-runtime.json"), 16384)?;
        let c: Self = serde_json::from_slice(&bytes)?;
        c.verify()?;
        Ok(c)
    }
    pub(crate) fn verify(&self) -> Result<()> {
        let pins: Value = serde_json::from_str(include_str!(
            "../../../../components/execution-runtime.lock.json"
        ))?;
        if pins["platform"]["inventory_sha256"] != self.inventory_sha256
            || pins["gvisor"]["inventory_sha256"] != self.tools_sha256
            || pins["rootfs"] != IMAGE
        {
            return Err(denied());
        }
        for path in [&self.release, &self.tools, &self.rootfs] {
            if !path.is_absolute()
                || path.canonicalize()? != *path
                || path.to_str().is_none_or(|s| s.contains([',', '\n', '\r']))
            {
                return Err(denied());
            }
        }
        files::verify_file(&self.release, "release.json", None, &self.inventory_sha256)?;
        let inventory: Value =
            serde_json::from_slice(&fs::read(self.release.join("release.json"))?)?;
        for (path, entry) in inventory["files"].as_object().ok_or_else(denied)? {
            files::verify_file(
                &self.release,
                path,
                None,
                entry["sha256"].as_str().ok_or_else(denied)?,
            )?;
        }
        files::verify_file(&self.tools, "verified.json", None, &self.tools_sha256)?;
        let tools: BTreeMap<String, String> =
            serde_json::from_slice(&fs::read(self.tools.join("verified.json"))?)?;
        for (path, hash) in tools {
            files::verify_file(&self.tools, &path, None, &hash)?;
        }
        files::verify_file(
            self.rootfs.parent().ok_or_else(denied)?,
            self.rootfs
                .file_name()
                .and_then(|v| v.to_str())
                .ok_or_else(denied)?,
            None,
            &self.rootfs_sha256,
        )?;
        Ok(())
    }
}
struct Lease {
    child: Child,
    input: Option<ChildStdin>,
    output: Option<JoinHandle<Result<Vec<u8>>>>,
    deadline: Instant,
    revoked: bool,
    _files: Prepared,
}
#[derive(Clone, Default)]
pub(crate) struct Manager(Arc<Mutex<BTreeMap<String, Lease>>>);
fn docker() -> Process {
    let mut c = Process::new("/usr/bin/docker");
    c.env_clear()
        .env("PATH", "/usr/bin:/bin")
        .arg("--host=unix:///var/run/docker.sock");
    c
}
fn monotonic_ms() -> i64 {
    let mut time = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut time) } != 0 {
        return 0;
    }
    time.tv_sec as i64 * 1000 + time.tv_nsec as i64 / 1_000_000
}
impl Manager {
    pub(crate) fn idle(&self) -> bool {
        self.0.lock().is_ok_and(|leases| leases.is_empty())
    }
    pub(crate) fn start(&self, id: &str, files: Prepared, config: Config) -> Result<()> {
        uuid(id)?;
        let mut leases = self.0.lock().map_err(|_| denied())?;
        if leases.len() >= 2 || leases.contains_key(id) {
            return Err(denied());
        }
        let name = format!("sb-exec-{id}");
        let mut c = docker();
        c.args([
            "run",
            "--rm",
            "--pull=never",
            "--name",
            &name,
            "--label",
            "supabricks.execution=true",
            "--init",
            "--interactive",
            "--privileged",
            "--read-only",
            "--network=none",
            "--memory=2g",
            "--memory-swap=2g",
            "--cpus=2",
            "--pids-limit=512",
            "--log-driver=none",
            "--tmpfs",
            "/work:rw,exec,nosuid,nodev,size=768m,mode=0700",
        ]);
        for (source, target) in [
            (config.release.as_path(), "/product"),
            (config.tools.as_path(), "/tools"),
            (config.rootfs.as_path(), "/rootfs.tar"),
            (files.dir.path(), "/admission"),
        ] {
            let source = source
                .to_str()
                .filter(|s| !s.contains([',', '\n', '\r']))
                .ok_or_else(denied)?;
            c.arg("--mount").arg(format!(
                "type=bind,source={source},target={target},readonly"
            ));
        }
        c.args([
            IMAGE,
            "/product/python/runtime/bin/python3.12",
            "-I",
            "-B",
            "/admission/supervisor.py",
        ]);
        let mut child = c
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        let mut stdout = child.stdout.take().ok_or_else(denied)?;
        let output = std::thread::spawn(move || {
            let mut out = Vec::new();
            stdout.by_ref().take(262145).read_to_end(&mut out)?;
            if out.len() > 262144 {
                return Err(denied());
            }
            Ok(out)
        });
        let mut lease = Lease {
            input: child.stdin.take(),
            child,
            output: Some(output),
            deadline: Instant::now() + Duration::from_millis(LEASE_MS as u64),
            revoked: false,
            _files: files,
        };
        use std::os::fd::AsRawFd;
        if let Some(input) = lease.input.as_ref() {
            let fd = input.as_raw_fd();
            if unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) } < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
        }
        if let Err(error) = renew(&mut lease) {
            let _ = lease.child.kill();
            return Err(error);
        }
        leases.insert(id.into(), lease);
        Ok(())
    }
    pub(crate) fn renew(&self, id: &str) -> Result<()> {
        let mut leases = self.0.lock().map_err(|_| denied())?;
        let lease = leases.get_mut(id).ok_or_else(denied)?;
        if lease.revoked || Instant::now() >= lease.deadline {
            return Err(denied());
        }
        match renew(lease) {
            // The supervisor may finish between the writer's tick and this
            // renewal. Keep its old deadline; tick must reap and validate the
            // exit/result. A closed pipe never creates new lease authority.
            Err(crate::store::Error::Io(error))
                if error.kind() == std::io::ErrorKind::BrokenPipe =>
            {
                Ok(())
            }
            result => result,
        }
    }
    pub(crate) fn tick(&self, store: &mut crate::store::Store, stopping: bool) -> Result<()> {
        let mut leases = self.0.lock().map_err(|_| denied())?;
        let ids: Vec<_> = leases.keys().cloned().collect();
        for id in ids {
            let lease = leases.get_mut(&id).unwrap();
            let denied = lease.revoked
                || stopping
                || Instant::now() >= lease.deadline
                || store.execution_live(&id).is_err();
            if denied {
                lease.revoked = true;
                lease.input.take();
            }
            let ended = lease.child.try_wait()?;
            if ended.is_some() {
                let mut lease = leases.remove(&id).unwrap();
                let result = if !denied && ended.is_some_and(|s| s.success()) {
                    lease
                        .output
                        .take()
                        .unwrap()
                        .join()
                        .map_err(|_| super::denied())
                        .and_then(|v| v)
                        .and_then(|v| Ok(serde_json::from_slice::<Value>(&v)?))
                        .ok()
                } else {
                    None
                };
                // Dropping the renewal channel independently expires the sandbox.
                // Finished launchers are reaped before releasing their resource slot.
                drop(lease);
                store.execution_finish(&id, result)?;
            }
        }
        Ok(())
    }
}
fn renew(lease: &mut Lease) -> Result<()> {
    let deadline = monotonic_ms() + LEASE_MS;
    writeln!(lease.input.as_mut().ok_or_else(denied)?, "{deadline}")?;
    lease.deadline = Instant::now() + Duration::from_millis(LEASE_MS as u64);
    Ok(())
}
impl Drop for Lease {
    fn drop(&mut self) {
        self.input.take();
        let _ = self.child.kill();
        let _ = self.child.wait();
        // The independent supervisor retains a hard deadline after client death.
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn completion_racing_renewal_keeps_the_original_deadline() {
        let manager = Manager::default();
        let mut child = std::process::Command::new("/bin/sh")
            .args(["-c", "exit 0"])
            .stdin(Stdio::piped())
            .spawn()
            .unwrap();
        let input = child.stdin.take();
        child.wait().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        manager.0.lock().unwrap().insert(
            "finished".into(),
            Lease {
                child,
                input,
                output: None,
                deadline,
                revoked: false,
                _files: Prepared {
                    dir: tempfile::tempdir().unwrap(),
                },
            },
        );
        manager.renew("finished").unwrap();
        assert_eq!(manager.0.lock().unwrap()["finished"].deadline, deadline);
        manager
            .0
            .lock()
            .unwrap()
            .get_mut("finished")
            .unwrap()
            .deadline = Instant::now() - Duration::from_secs(1);
        assert!(manager.renew("finished").is_err());
    }
}
