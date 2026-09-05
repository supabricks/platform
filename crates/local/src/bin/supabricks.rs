use std::{path::PathBuf, process::ExitCode};
fn main() -> ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() == 1 && (args[0] == "--help" || args[0] == "-h") {
        println!(
            "Usage: supabricks daemon --data-dir PATH\n\nRun the local state daemon. Engine execution is not implemented yet."
        );
        return ExitCode::SUCCESS;
    }
    if args.len() != 3 || args[0] != "daemon" || args[1] != "--data-dir" {
        eprintln!("Usage: supabricks daemon --data-dir PATH");
        return ExitCode::FAILURE;
    }
    match supabricks_local::daemon::Daemon::bind(&PathBuf::from(&args[2])).and_then(|d| d.serve()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("supabricks: {error}");
            ExitCode::FAILURE
        }
    }
}
