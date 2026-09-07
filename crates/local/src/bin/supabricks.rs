use std::process::ExitCode;
fn main() -> ExitCode {
    unsafe {
        libc::umask(0o077);
    }
    match supabricks_local::cli::run() {
        Ok(code) => ExitCode::from(code),
        Err(e) => {
            let diagnostic = supabricks_local::client::diagnostic(&e);
            eprintln!(
                "{}",
                serde_json::json!({"api_version":1,"error":diagnostic})
            );
            ExitCode::from(diagnostic["exit_code"].as_u64().unwrap_or(1) as u8)
        }
    }
}
