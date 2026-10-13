use super::*;
use std::{fs, io::Read, os::unix::fs::MetadataExt};
fn io_error(error: std::io::Error) -> &'static str {
    if error.kind() == std::io::ErrorKind::NotFound {
        "process_tree_changed"
    } else {
        "probe_unavailable"
    }
}
fn text(path: impl AsRef<std::path::Path>) -> ProbeResult<String> {
    let mut value = String::new();
    fs::File::open(path)
        .map_err(io_error)?
        .take(65537)
        .read_to_string(&mut value)
        .map_err(|_| "probe_unavailable")?;
    if value.len() > 65536 {
        return Err("process_tree_limit");
    }
    Ok(value)
}
fn parse(pid: u32, uid: u32, stat: &str, hz: u64, page: u64) -> ProbeResult<Process> {
    let fields: Vec<_> = stat
        .rsplit_once(')')
        .ok_or("invalid_process_stat")?
        .1
        .split_whitespace()
        .collect();
    let number = |i: usize| -> ProbeResult<u64> {
        fields
            .get(i)
            .and_then(|s| s.parse().ok())
            .ok_or("invalid_process_stat")
    };
    let nanos = |ticks| -> ProbeResult<u64> {
        u64::try_from((ticks as u128) * 1_000_000_000 / (hz as u128))
            .map_err(|_| "counter_overflow")
    };
    if hz == 0 || page == 0 {
        return Err("invalid_clock_units");
    }
    Ok(Process {
        pid,
        uid,
        parent: u32::try_from(number(1)?).map_err(|_| "invalid_process_stat")?,
        start: number(19)?.to_string(),
        cpu_ns: nanos(add(number(11)?, number(12)?)?)?,
        reaped_cpu_ns: nanos(add(number(13)?, number(14)?)?)?,
        rss_bytes: if fields.first() == Some(&"Z") {
            0
        } else {
            number(21)?.checked_mul(page).ok_or("counter_overflow")?
        },
    })
}
pub(super) fn read(pid: u32) -> ProbeResult<Process> {
    let dir = format!("/proc/{pid}");
    let uid = fs::metadata(&dir).map_err(io_error)?.uid();
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    let page = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
    if hz <= 0 || page <= 0 {
        return Err("invalid_clock_units");
    }
    parse(
        pid,
        uid,
        &text(format!("{dir}/stat"))?,
        hz as u64,
        page as u64,
    )
}
pub(super) fn children(pid: u32, deadline: Instant) -> ProbeResult<Vec<u32>> {
    // Child processes can be spawned by any compute_ctl thread. PG backends
    // create new sessions, so a process-group scan alone would omit them.
    let mut children = Vec::new();
    let tasks = fs::read_dir(format!("/proc/{pid}/task")).map_err(io_error)?;
    for (count, task) in tasks.enumerate() {
        check(deadline)?;
        if count >= 1024 {
            return Err("process_tree_limit");
        }
        let path = task
            .map_err(|_| "probe_unavailable")?
            .path()
            .join("children");
        for child in text(path)?.split_whitespace() {
            children.push(child.parse().map_err(|_| "invalid_process_stat")?);
            if children.len() > MAX_PROCESSES {
                return Err("process_tree_limit");
            }
        }
    }
    children.sort_unstable();
    children.dedup();
    Ok(children)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stat_parsing_counts_own_and_reaped_time_without_comm_ambiguity() {
        let mut fields = vec!["0"; 22];
        fields[0] = "S";
        fields[1] = "42";
        fields[11] = "12";
        fields[12] = "3";
        fields[13] = "7";
        fields[14] = "2";
        fields[19] = "99";
        fields[21] = "10";
        let p = parse(
            45,
            1000,
            &format!("45 (name with ) spaces) {}", fields.join(" ")),
            100,
            4096,
        )
        .unwrap();
        assert_eq!(
            (p.parent, p.cpu_ns, p.reaped_cpu_ns, p.rss_bytes),
            (42, 150_000_000, 90_000_000, 40960)
        );
        fields[0] = "Z";
        assert_eq!(
            parse(45, 1000, &format!("45 (x) {}", fields.join(" ")), 100, 4096)
                .unwrap()
                .rss_bytes,
            0
        );
        assert!(parse(45, 1000, "bad", 100, 4096).is_err());
    }
}
