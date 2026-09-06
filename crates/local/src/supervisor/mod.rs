//! Process Compose executes gated children; SQLite owns launch authorization.
pub(crate) mod os;
use crate::{
    daemon::{Envelope, Request},
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    os::unix::{fs::OpenOptionsExt, net::UnixStream, process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use supabricks_core::resource::BranchId;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Launch {
    pub root: PathBuf,
    pub generation: i64,
    pub role: String,
    pub token: String,
    pub branch: Option<(BranchId, i64)>,
    pub argv: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub cwd: PathBuf,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct OwnedProcess {
    pub root: PathBuf,
    pub generation: i64,
    pub role: String,
    pub pid: u32,
    pub start_identity: String,
    pub token: String,
    pub branch: Option<(BranchId, i64)>,
}

/// Atomic and durable private runtime files; directory durability matters after rename.
pub fn write_private(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| invalid("missing parent directory"))?;
    let temp = parent.join(format!(
        ".write-{}",
        supabricks_core::resource::OperationId::new()
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temp)?;
    let result = (|| {
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temp, path)?;
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(temp);
    result
}

pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    write_private(path, &serde_json::to_vec_pretty(value)?)
}

/// This subprocess performs no engine IO until its PID + birth identity commits.
pub fn child(path: &Path, stdin_gate: bool) -> Result<()> {
    let launch: Launch = serde_json::from_slice(&fs::read(path)?)?;
    if launch.argv.is_empty() || !Path::new(&launch.argv[0]).is_absolute() {
        return Err(invalid("invalid launch executable"));
    }
    if stdin_gate {
        let mut byte = [0];
        std::io::stdin().read_exact(&mut byte)?;
        if byte != [1] {
            return Err(conflict("launch was not authorized"));
        }
    } else {
        let mut stream = UnixStream::connect(launch.root.join("control.sock"))?;
        stream.set_read_timeout(Some(Duration::from_secs(30)))?;
        writeln!(
            stream,
            "{}",
            serde_json::to_string(&Envelope {
                version: 1,
                request: Request::AuthorizeProcess {
                    role: launch.role.clone(),
                    generation: launch.generation,
                    token: launch.token.clone(),
                    pid: std::process::id()
                }
            })?
        )?;
        let mut line = String::new();
        BufReader::new(stream).take(65536).read_line(&mut line)?;
        let response: serde_json::Value = serde_json::from_str(&line)?;
        if response["result"]["authorized"] != true {
            return Err(conflict("launch was not authorized"));
        }
    }
    let mut cmd = Command::new(&launch.argv[0]);
    cmd.args(&launch.argv[1..])
        .env_clear()
        .envs(&launch.env)
        .env("SUPABRICKS_PROCESS_TOKEN", &launch.token)
        .current_dir(&launch.cwd)
        .stdin(Stdio::null());
    Err(cmd.exec().into())
}

pub fn evidence(launch: &Launch, pid: u32) -> Result<OwnedProcess> {
    let deadline = Instant::now() + Duration::from_secs(2);
    let id = loop {
        let id = os::identity(pid)?.ok_or_else(|| conflict("launch process disappeared"))?;
        if id.zombie || id.group != pid || id.uid != unsafe { libc::geteuid() } {
            return Err(conflict(
                "launch identity does not match its owned process group",
            ));
        }
        if os::has_token(&id, &launch.token)? {
            break id;
        }
        if Instant::now() >= deadline {
            return Err(conflict(
                "launch environment does not match its authorization",
            ));
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    Ok(OwnedProcess {
        root: launch.root.clone(),
        generation: launch.generation,
        role: launch.role.clone(),
        pid,
        start_identity: id.start,
        token: launch.token.clone(),
        branch: launch.branch,
    })
}

/// Used only for Process Compose itself. Its stdin pipe closes on daemon death;
/// before authorization it cannot execute, afterwards its identity is durable.
pub fn start_supervisor(store: &mut Store, launch: &Launch, file: &Path) -> Result<Child> {
    write_json(file, launch)?;
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(store.root().join("logs/supervisor.log"))?;
    let mut child = Command::new(std::env::current_exe()?)
        .args(["child", "--launch"])
        .arg(file)
        .arg("--stdin-gate")
        .env_clear()
        .env("SUPABRICKS_PROCESS_TOKEN", &launch.token)
        .process_group(0)
        .stdin(Stdio::piped())
        .stdout(log.try_clone()?)
        .stderr(log)
        .spawn()?;
    let result = (|| {
        let record = evidence(launch, child.id())?;
        store.record_native_process(&record)?;
        child.stdin.take().unwrap().write_all(&[1])?;
        Ok(())
    })();
    if let Err(e) = result {
        let _ = child.kill();
        let _ = child.wait();
        return Err(e);
    }
    Ok(child)
}

/// Verify every surviving group member, including orphaned Postgres descendants.
/// A reused leader PID or an unmarked member stops recovery without any signal.
pub fn members(record: &OwnedProcess) -> Result<Vec<u32>> {
    if let Some(id) = os::identity(record.pid)?
        && id.start != record.start_identity
    {
        return Err(conflict(format!(
            "{} PID has been reused; ownership is ambiguous",
            record.role
        )));
    }
    let mut members = Vec::new();
    for pid in os::pids()? {
        // Filter by the kernel group before asking for identity/environment.
        // macOS protects metadata for some unrelated system processes.
        let group = unsafe { libc::getpgid(pid as i32) };
        if group < 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ESRCH) {
                continue;
            }
            return Err(error.into());
        }
        if group as u32 != record.pid {
            continue;
        }
        let Some(id) = os::identity(pid)? else {
            continue;
        };
        if id.group != record.pid || id.zombie {
            continue;
        }
        if id.uid != unsafe { libc::geteuid() } || !os::has_token(&id, &record.token)? {
            if os::identity(pid)?.is_none_or(|i| i.zombie) {
                continue;
            }
            return Err(conflict(format!(
                "{} contains an unverified process; recovery stopped",
                record.role
            )));
        }
        members.push(pid);
    }
    Ok(members)
}

pub fn stop(record: &OwnedProcess) -> Result<()> {
    // Stop the verified leader first. Neon's sandboxed WAL redo helpers clear
    // their environment; they exit when the pageserver's pipes close. Never
    // signal such an unmarked child merely because it shares a numeric PGID.
    if let Some(id) = os::identity(record.pid)? {
        if id.start != record.start_identity
            || id.uid != unsafe { libc::geteuid() }
            || (!id.zombie && !os::has_token(&id, &record.token)?)
        {
            return Err(conflict("process leader ownership is ambiguous"));
        }
        if !id.zombie && unsafe { libc::kill(record.pid as i32, libc::SIGKILL) } != 0 {
            let e = std::io::Error::last_os_error();
            if e.raw_os_error() != Some(libc::ESRCH) {
                return Err(e.into());
            }
        }
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match members(record) {
            Ok(members) if members.is_empty() => return Ok(()),
            Ok(_) => {}
            Err(error) => {
                if Instant::now() >= deadline {
                    return Err(error);
                }
                std::thread::sleep(Duration::from_millis(20));
                continue;
            }
        }
        // Group leader + start identity and all members were checked above.
        if unsafe { libc::kill(-(record.pid as i32), libc::SIGKILL) } != 0 {
            let e = std::io::Error::last_os_error();
            if e.raw_os_error() != Some(libc::ESRCH) {
                return Err(e.into());
            }
        }
        if Instant::now() >= deadline {
            return Err(conflict(format!(
                "{} process group did not stop",
                record.role
            )));
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
