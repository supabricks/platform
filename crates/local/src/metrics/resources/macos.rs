use super::*;
pub(super) fn read(pid: u32) -> ProbeResult<Process> {
    let mut bsd: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of_val(&bsd) as i32;
    if unsafe {
        libc::proc_pidinfo(
            pid as i32,
            libc::PROC_PIDTBSDINFO,
            1,
            (&mut bsd as *mut libc::proc_bsdinfo).cast(),
            size,
        )
    } != size
    {
        return Err(
            if std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
                "process_tree_changed"
            } else {
                "probe_unavailable"
            },
        );
    }
    let mut usage: libc::rusage_info_v2 = unsafe { std::mem::zeroed() };
    if unsafe {
        libc::proc_pid_rusage(
            pid as i32,
            libc::RUSAGE_INFO_V2,
            (&mut usage as *mut libc::rusage_info_v2).cast(),
        )
    } != 0
    {
        return Err("probe_unavailable");
    }
    let mut timebase: libc::mach_timebase_info = unsafe { std::mem::zeroed() };
    if unsafe { libc::mach_timebase_info(&mut timebase) } != 0 || timebase.denom == 0 {
        return Err("invalid_clock_units");
    }
    // proc_pid_rusage CPU counters are Mach absolute time, including on ARM.
    let nanos = |ticks: u64| -> ProbeResult<u64> {
        u64::try_from((ticks as u128) * (timebase.numer as u128) / (timebase.denom as u128))
            .map_err(|_| "counter_overflow")
    };
    Ok(Process {
        pid,
        parent: bsd.pbi_ppid,
        uid: bsd.pbi_uid,
        start: format!("{}:{}", bsd.pbi_start_tvsec, bsd.pbi_start_tvusec),
        cpu_ns: nanos(add(usage.ri_user_time, usage.ri_system_time)?)?,
        reaped_cpu_ns: nanos(add(usage.ri_child_user_time, usage.ri_child_system_time)?)?,
        rss_bytes: if bsd.pbi_status == 5 {
            0
        } else {
            usage.ri_resident_size
        },
    })
}
pub(super) fn children(pid: u32, deadline: Instant) -> ProbeResult<Vec<u32>> {
    check(deadline)?;
    let mut pids = vec![0i32; MAX_PROCESSES + 1];
    // libproc's convenience wrapper returns a PID count, not a byte count.
    // libproc maps syscall errors to zero as well as returning zero for no children.
    unsafe {
        *libc::__error() = 0;
    }
    let count = unsafe {
        libc::proc_listchildpids(
            pid as i32,
            pids.as_mut_ptr().cast(),
            (pids.len() * std::mem::size_of::<i32>()) as i32,
        )
    };
    if count < 0 || (count == 0 && unsafe { *libc::__error() } != 0) {
        return Err("probe_unavailable");
    }
    if count as usize >= pids.len() {
        return Err("process_tree_limit");
    }
    let mut result: Vec<u32> = pids[..count as usize]
        .iter()
        .filter(|p| **p > 0)
        .map(|p| *p as u32)
        .collect();
    result.sort_unstable();
    result.dedup();
    Ok(result)
}
