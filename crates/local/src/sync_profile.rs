//! Opt-in diagnostic spans. No credentials, SQL, row values, or synchronous writes.
#[cfg(feature = "sync-profile")]
mod enabled {
    use serde::Serialize;
    use std::{
        cell::RefCell,
        collections::BTreeMap,
        fs::OpenOptions,
        io::Write,
        os::unix::fs::OpenOptionsExt,
        path::{Path, PathBuf},
        sync::{Mutex, OnceLock},
        time::Instant,
    };
    #[derive(Default, Serialize)]
    struct Metric {
        calls: u64,
        total_ns: u64,
        self_ns: u64,
        max_ns: u64,
    }
    struct State {
        path: PathBuf,
        metrics: BTreeMap<&'static str, Metric>,
        last: Instant,
        bytes: usize,
        write_ns: u64,
        write_errors: u64,
    }
    static STATE: OnceLock<Mutex<State>> = OnceLock::new();
    thread_local! { static STACK: RefCell<Vec<u64>> = const { RefCell::new(Vec::new()) }; }
    pub struct Session;
    pub fn init(root: &Path) -> Option<Session> {
        let dir = root.join("sync-profile");
        if !dir.join("enabled").is_file() {
            return None;
        }
        STATE
            .set(Mutex::new(State {
                path: dir.join("daemon.jsonl"),
                metrics: BTreeMap::new(),
                last: Instant::now(),
                bytes: 0,
                write_ns: 0,
                write_errors: 0,
            }))
            .ok()?;
        Some(Session)
    }
    fn flush(state: &mut State, final_record: bool) {
        if !final_record
            && (state.last.elapsed().as_secs_f64() < 1.0 || state.bytes >= 8 * 1024 * 1024)
        {
            return;
        }
        let start = Instant::now();
        let row = serde_json::json!({"role":"daemon", "at_ms":chrono::Utc::now().timestamp_millis(), "final":final_record, "metrics":state.metrics, "profile_write_ns":state.write_ns, "profile_write_errors":state.write_errors,"budget_exceeded":state.bytes>=8*1024*1024});
        let result = (|| -> std::io::Result<()> {
            let mut bytes = serde_json::to_vec(&row)?;
            bytes.push(b'\n');
            let mut f = OpenOptions::new()
                .create(true)
                .append(true)
                .mode(0o600)
                .open(&state.path)?;
            f.write_all(&bytes)?;
            state.bytes += bytes.len();
            Ok(())
        })();
        if result.is_err() {
            state.write_errors += 1;
        }
        state.last = Instant::now();
        state.write_ns += start.elapsed().as_nanos() as u64;
    }
    impl Drop for Session {
        fn drop(&mut self) {
            if let Some(state) = STATE.get()
                && let Ok(mut state) = state.lock()
            {
                flush(&mut state, true);
            }
        }
    }
    pub struct Span {
        name: &'static str,
        start: Instant,
    }
    pub fn span(name: &'static str) -> Option<Span> {
        STATE.get()?;
        STACK.with(|s| s.borrow_mut().push(0));
        Some(Span {
            name,
            start: Instant::now(),
        })
    }
    impl Drop for Span {
        fn drop(&mut self) {
            let elapsed = self.start.elapsed().as_nanos() as u64;
            let (children, outer) = STACK.with(|s| {
                let mut stack = s.borrow_mut();
                let children = stack.pop().unwrap_or(0);
                if let Some(parent) = stack.last_mut() {
                    *parent += elapsed;
                }
                (children, stack.is_empty())
            });
            if let Some(state) = STATE.get()
                && let Ok(mut state) = state.lock()
            {
                let metric = state.metrics.entry(self.name).or_default();
                metric.calls += 1;
                metric.total_ns += elapsed;
                metric.self_ns += elapsed.saturating_sub(children);
                metric.max_ns = metric.max_ns.max(elapsed);
                if outer {
                    flush(&mut state, false);
                }
            }
        }
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn nested_spans_and_final_snapshot() {
            let root = tempfile::tempdir().unwrap();
            std::fs::create_dir(root.path().join("sync-profile")).unwrap();
            std::fs::write(root.path().join("sync-profile/enabled"), "").unwrap();
            let session = init(root.path()).unwrap();
            {
                let _outer = span("outer");
                {
                    let _inner = span("inner");
                    std::thread::sleep(std::time::Duration::from_millis(2));
                }
            }
            drop(session);
            let row: serde_json::Value = serde_json::from_str(
                std::fs::read_to_string(root.path().join("sync-profile/daemon.jsonl"))
                    .unwrap()
                    .trim(),
            )
            .unwrap();
            assert_eq!(row["final"], true);
            assert_eq!(row["profile_write_errors"], 0);
            assert!(
                row["metrics"]["outer"]["total_ns"].as_u64().unwrap()
                    >= row["metrics"]["inner"]["total_ns"].as_u64().unwrap()
            );
            assert_eq!(row["metrics"]["inner"]["calls"], 1);
        }
    }
}
#[cfg(feature = "sync-profile")]
pub(crate) use enabled::{init, span};
#[cfg(not(feature = "sync-profile"))]
pub(crate) struct Disabled;
#[cfg(not(feature = "sync-profile"))]
#[inline]
pub(crate) fn init(_: &std::path::Path) -> Disabled {
    Disabled
}
#[cfg(not(feature = "sync-profile"))]
#[inline]
pub(crate) fn span(_: &'static str) -> Disabled {
    Disabled
}
