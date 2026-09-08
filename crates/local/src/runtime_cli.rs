use crate::client::request;
use crate::{
    daemon::{Daemon, Request},
    store::{Error, Result, Store},
};
use std::{
    fs::{self, OpenOptions},
    os::unix::{
        fs::{DirBuilderExt, FileTypeExt, OpenOptionsExt},
        process::CommandExt,
    },
    path::PathBuf,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
fn error(s: &str) -> Error {
    supabricks_core::error::OperationError::Unavailable(s.into()).into()
}
pub fn run(
    command: &str,
    root: PathBuf,
    bundle: Option<PathBuf>,
    helpers: Option<PathBuf>,
) -> Result<()> {
    if matches!(command, "up" | "daemon") {
        crate::installation::check_data_root(&root)?;
    }
    let installed = if matches!(command, "up" | "daemon") && bundle.is_none() {
        crate::installation::Installation::discover()?
    } else {
        None
    };
    let bundle = bundle.or_else(|| installed.as_ref().map(|i| i.bundle()));
    let helpers = helpers.or_else(|| installed.as_ref().map(|i| i.helpers()));
    if command == "daemon" {
        if let Some(installed) = crate::installation::Installation::discover()? {
            installed.verify()?;
        }
        let daemon = Daemon::bind(&root)?;
        return if let (Some(bundle), Some(helpers)) = (bundle, helpers) {
            daemon.enable_engine(&bundle, &helpers)?.serve()
        } else {
            daemon.serve()
        };
    }
    if command == "status" {
        println!("{}", request(&root, Request::Status)?);
        return Ok(());
    }
    if command == "down" {
        shutdown(&root)?;
        println!(
            "{}",
            serde_json::json!({"status":"stopped","data_retained":true})
        );
        return Ok(());
    }
    if command != "up" {
        return Err(error("unknown runtime command"));
    }
    if let Ok(status) = request(&root, Request::Status) {
        if status["engine_execution"] != true {
            return Err(error(
                "existing daemon has no engine; stop it before enabling the native cell",
            ));
        }
        return wait_ready(&root, None);
    }
    if !root.join("runtime.json").exists() && bundle.is_none() {
        return Err(crate::store::error::invalid(
            "no installed engine found; source builds require --bundle and --helpers",
        ));
    }
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&root)?;
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(root.join("daemon.log"))?;
    let mut cmd = Command::new(std::env::current_exe()?);
    cmd.args(["daemon", "--data-dir"])
        .arg(&root)
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log)
        .process_group(0);
    if let (Some(bundle), Some(helpers)) = (bundle, helpers) {
        cmd.arg("--bundle")
            .arg(bundle)
            .arg("--helpers")
            .arg(helpers);
    }
    let mut child = cmd.spawn()?;
    wait_ready(&root, Some(&mut child))
}
fn acquire_after_shutdown(root: &std::path::Path, deadline: Instant) -> Result<Store> {
    let mut progress = Instant::now();
    loop {
        match Store::open(root) {
            Ok(store) => return Ok(store),
            Err(Error::Operation(supabricks_core::error::OperationError::Conflict(message)))
                if message == "another daemon owns this data root" =>
            {
                // Drop unlinks the socket before SQLite's final close/fsync
                // and the ownership lock release. Unlink is not completion.
                if Instant::now() >= deadline {
                    return Err(error(
                        "shutdown ownership release is still pending; inspect daemon.log",
                    ));
                }
                if progress.elapsed() >= Duration::from_secs(1) {
                    eprintln!(
                        "{}",
                        serde_json::json!({"progress":"waiting for shutdown ownership release","data_dir":root})
                    );
                    progress = Instant::now();
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            Err(error) => return Err(error),
        }
    }
}
fn wait_ready(root: &std::path::Path, mut child: Option<&mut std::process::Child>) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut progress = Instant::now();
    loop {
        if request(root, Request::Status).is_ok_and(|s| s["runtime"]["ready"] == true) {
            println!("{}", serde_json::json!({"status":"ready","data_dir":root}));
            return Ok(());
        }
        if let Some(child) = &mut child
            && child.try_wait()?.is_some()
        {
            return Err(error("daemon startup failed; inspect daemon.log"));
        }
        if Instant::now() >= deadline {
            return Err(error("daemon startup is still pending; inspect daemon.log"));
        }
        if progress.elapsed() >= Duration::from_secs(1) {
            eprintln!(
                "{}",
                serde_json::json!({"progress":"waiting for runtime; status and doctor remain available","data_dir":root})
            );
            progress = Instant::now();
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

pub(crate) fn shutdown(root: &std::path::Path) -> Result<()> {
    if request(root, Request::Shutdown).is_err() {
        if root.exists() {
            let mut store = acquire_after_shutdown(root, Instant::now() + Duration::from_secs(60))?;
            crate::engine::Cell::recover(&mut store)?;
            crate::sessions::Sessions::recover(&mut store)?.stop(&mut store)?;
            let socket = store.root().join("control.sock");
            if fs::symlink_metadata(&socket).is_ok_and(|m| m.file_type().is_socket()) {
                fs::remove_file(socket)?;
            }
        }
        return Ok(());
    }
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut progress = Instant::now();
    while root.join("control.sock").exists() {
        if Instant::now() >= deadline {
            return Err(error("shutdown is still pending; inspect daemon.log"));
        }
        if progress.elapsed() >= Duration::from_secs(1) {
            eprintln!(
                "{}",
                serde_json::json!({"progress":"waiting for runtime; status and doctor remain available","data_dir":root})
            );
            progress = Instant::now();
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    // Socket disappearance can also mean a crashed daemon. Reacquire the
    // ownership lock and account for every recorded writer before success.
    let mut store = acquire_after_shutdown(root, deadline)?;
    crate::engine::Cell::recover(&mut store)?;
    crate::sessions::Sessions::recover(&mut store)?.stop(&mut store)?;
    return Ok(());
}
