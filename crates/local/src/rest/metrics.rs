//! Metrics of a branch, from the samples the daemon keeps.
use super::{Call, Local, Problem, Reply, databases, shared::timestamp, tables::place};
use crate::{
    metrics::{RETENTION, points},
    store::Store,
};
use serde_json::json;

/// Each range and the width of one point in it, in seconds.
const RANGES: [(&str, i64, i64); 4] = [
    ("1h", 3600, 60),
    ("6h", 6 * 3600, 300),
    ("24h", 24 * 3600, 900),
    ("7d", 7 * 24 * 3600, 7200),
];

pub fn get(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
) -> Result<Reply, Problem> {
    let (_, branch) = place(store, path)?;
    let asked = call.parameter("range")?.unwrap_or("1h");
    let (range, span, step) = RANGES
        .into_iter()
        .find(|(name, _, _)| *name == asked)
        .ok_or_else(|| Problem::invalid("range must be 1h, 6h, 24h or 7d."))?;
    let metrics = databases::owner(host)?
        .metrics
        .ok_or_else(|| Problem::unavailable("This server is not keeping metrics."))?;
    let interval = metrics.interval().as_millis() as i64;
    // Whole steps, ending with the one now in progress.
    let step_ms = step * 1000;
    let to = (chrono::Utc::now().timestamp_millis() / step_ms + 1) * step_ms;
    let from = to - span * 1000;
    // One sample earlier, so the first point can have a rate.
    let samples = metrics
        .series
        .since(branch, from - interval * 5 / 2)
        .map_err(|e| Problem::from(&e))?;
    Ok(Reply::ok(json!({
        "range": range,
        "from": timestamp(from),
        "to": timestamp(to),
        "step_seconds": step,
        "sample_interval_seconds": metrics.interval().as_secs().max(1),
        "retention_seconds": RETENTION.as_secs(),
        "points": points(&samples, from, to, step_ms, interval),
    })))
}
