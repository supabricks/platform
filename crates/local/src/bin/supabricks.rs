use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Write},
    os::unix::{
        fs::{DirBuilderExt, FileTypeExt, OpenOptionsExt},
        net::UnixStream,
        process::CommandExt,
    },
    path::PathBuf,
    process::{Command, ExitCode, Stdio},
    time::{Duration, Instant},
};
use supabricks_local::{
    daemon::{Daemon, Envelope, Request},
    store::{Error, Result, Store},
};
const HELP: &str = "Usage: supabricks up --data-dir PATH [--bundle PATH --helpers PATH]\n       supabricks daemon --data-dir PATH [--bundle PATH --helpers PATH]\n       supabricks status --data-dir PATH\n       supabricks down --data-dir PATH\n\nThe first up requires a verified PG17 bundle and a directory containing\nprocess-compose and SeaweedFS with SQLite support. Subsequent up reconnects.\n";
fn error(s: &str) -> Error {
    std::io::Error::other(s).into()
}
fn request(root: &std::path::Path, request: Request) -> Result<serde_json::Value> {
    let mut stream = UnixStream::connect(root.join("control.sock"))?;
    stream.set_read_timeout(Some(Duration::from_secs(15)))?;
    writeln!(
        stream,
        "{}",
        serde_json::to_string(&Envelope {
            version: 1,
            request
        })?
    )?;
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line)?;
    let value: serde_json::Value = serde_json::from_str(&line)?;
    if value.get("error").is_some() {
        return Err(error("local daemon request failed"));
    }
    Ok(value["result"].clone())
}
fn run() -> Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() == 1 && (args[0] == "--help" || args[0] == "-h") {
        print!("{HELP}");
        return Ok(());
    }
    if args.first().is_some_and(|a| a == "child") {
        if (args.len() != 3 && args.len() != 4)
            || args[1] != "--launch"
            || (args.len() == 4 && args[3] != "--stdin-gate")
        {
            return Err(error(HELP));
        }
        return supabricks_local::supervisor::child(&PathBuf::from(&args[2]), args.len() == 4);
    }
    if args.is_empty() {
        return Err(error(HELP));
    }
    let mut root = None;
    let mut bundle = None;
    let mut helpers = None;
    for pair in args[1..].chunks(2) {
        if pair.len() != 2 {
            return Err(error(HELP));
        }
        let value = Some(PathBuf::from(&pair[1]));
        if pair[0] == "--data-dir" && root.is_none() {
            root = value;
        } else if pair[0] == "--bundle" && bundle.is_none() {
            bundle = value;
        } else if pair[0] == "--helpers" && helpers.is_none() {
            helpers = value;
        } else {
            return Err(error(HELP));
        }
    }
    let root = root.ok_or_else(|| error(HELP))?;
    if bundle.is_some() != helpers.is_some() {
        return Err(error(HELP));
    }
    if args[0] == "daemon" {
        let daemon = Daemon::bind(&root)?;
        return if let (Some(bundle), Some(helpers)) = (bundle, helpers) {
            daemon.enable_engine(&bundle, &helpers)?.serve()
        } else {
            daemon.serve()
        };
    }
    if args[0] == "status" {
        println!("{}", request(&root, Request::Status)?);
        return Ok(());
    }
    if args[0] == "down" {
        if request(&root, Request::Shutdown).is_err() {
            if root.exists() {
                let mut store = Store::open(&root)?;
                supabricks_local::engine::Cell::recover(&mut store)?;
                let socket = store.root().join("control.sock");
                if fs::symlink_metadata(&socket).is_ok_and(|m| m.file_type().is_socket()) {
                    fs::remove_file(socket)?;
                }
            }
            println!("Supabricks stopped.");
            return Ok(());
        }
        let deadline = Instant::now() + Duration::from_secs(60);
        while root.join("control.sock").exists() {
            if Instant::now() >= deadline {
                return Err(error("shutdown is still pending; inspect daemon.log"));
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        println!("Supabricks stopped.");
        return Ok(());
    }
    if args[0] != "up" {
        return Err(error(HELP));
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
        return Err(error("first up requires --bundle and --helpers"));
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
fn wait_ready(root: &std::path::Path, mut child: Option<&mut std::process::Child>) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        if request(root, Request::Status).is_ok_and(|s| s["runtime"]["ready"] == true) {
            println!("Supabricks is ready at {}", root.display());
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
        std::thread::sleep(Duration::from_millis(100));
    }
}
fn main() -> ExitCode {
    // All native children inherit private creation permissions.
    unsafe {
        libc::umask(0o077);
    }
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("supabricks: {e}");
            ExitCode::FAILURE
        }
    }
}
