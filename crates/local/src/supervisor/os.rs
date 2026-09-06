//! OS identity, independent of PID reuse. No signal is sent on ambiguous evidence.
use crate::store::{Result, error::conflict};
#[cfg(target_os = "linux")]
use std::fs;
use std::io;

pub struct Identity {
    pub pid: u32,
    pub group: u32,
    pub uid: u32,
    pub start: String,
    pub zombie: bool,
}

#[cfg(target_os = "linux")]
pub fn identity(pid: u32) -> Result<Option<Identity>> {
    use std::os::unix::fs::MetadataExt;
    let dir = format!("/proc/{pid}");
    let stat = match fs::read_to_string(format!("{dir}/stat")) {
        Ok(s) => s,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let fields: Vec<_> = stat
        .rsplit_once(')')
        .ok_or_else(|| conflict("invalid process identity"))?
        .1
        .split_whitespace()
        .collect();
    let group = fields
        .get(2)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| conflict("invalid process group"))?;
    let start = fields
        .get(19)
        .ok_or_else(|| conflict("missing process birth time"))?;
    let uid = match fs::metadata(dir) {
        Ok(m) => m.uid(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    Ok(Some(Identity {
        pid,
        group,
        uid,
        start: format!("{}:{start}", boot.trim()),
        zombie: fields[0] == "Z",
    }))
}

#[cfg(target_os = "linux")]
pub fn group_pids(group: u32) -> Result<Vec<u32>> {
    fs::read_dir("/proc")?
        .filter_map(|e| match e {
            Ok(e) => e.file_name().to_str().and_then(|s| s.parse().ok()).map(Ok),
            Err(e) => Some(Err(e.into())),
        })
        .filter_map(|pid| match pid {
            Ok(pid) => {
                let actual = unsafe { libc::getpgid(pid as i32) };
                if actual < 0 {
                    let e = io::Error::last_os_error();
                    if e.raw_os_error() == Some(libc::ESRCH) {
                        None
                    } else {
                        Some(Err(e.into()))
                    }
                } else if actual as u32 == group {
                    Some(Ok(pid))
                } else {
                    None
                }
            }
            Err(e) => Some(Err(e)),
        })
        .collect()
}

#[cfg(target_os = "linux")]
fn environment(pid: u32) -> Result<Vec<u8>> {
    Ok(fs::read(format!("/proc/{pid}/environ"))?)
}

#[cfg(target_os = "macos")]
pub fn identity(pid: u32) -> Result<Option<Identity>> {
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of_val(&info) as i32;
    let n = unsafe {
        libc::proc_pidinfo(
            pid as i32,
            libc::PROC_PIDTBSDINFO,
            1, // Include zombies: an exited process cannot own a writer.
            (&mut info as *mut libc::proc_bsdinfo).cast(),
            size,
        )
    };
    if n == 0 {
        let e = io::Error::last_os_error();
        if e.raw_os_error() == Some(libc::ESRCH) {
            return Ok(None);
        }
        return Err(io::Error::new(e.kind(), format!("proc_pidinfo({pid}): {e}")).into());
    }
    if n != size {
        return Err(conflict("incomplete process identity"));
    }
    Ok(Some(Identity {
        pid,
        group: info.pbi_pgid,
        uid: info.pbi_uid,
        start: format!("{}:{}", info.pbi_start_tvsec, info.pbi_start_tvusec),
        zombie: info.pbi_status == 5,
    }))
}

#[cfg(target_os = "macos")]
pub fn group_pids(group: u32) -> Result<Vec<u32>> {
    // Ask the kernel for this group. Inspecting every system PID can fail
    // under macOS process protections even before querying its environment.
    let count = unsafe { libc::proc_listpgrppids(group as i32, std::ptr::null_mut(), 0) };
    if count < 0 {
        let e = io::Error::last_os_error();
        return Err(io::Error::new(e.kind(), format!("proc_listpgrppids({group}): {e}")).into());
    }
    let mut pids = vec![0i32; count as usize + 4096];
    let n = unsafe {
        libc::proc_listpgrppids(
            group as i32,
            pids.as_mut_ptr().cast(),
            (pids.len() * 4) as i32,
        )
    };
    if n < 0 || n as usize >= pids.len() {
        return Err(conflict("cannot enumerate process groups"));
    }
    Ok(pids[..n as usize]
        .iter()
        .filter(|p| **p > 0)
        .map(|p| *p as u32)
        .collect())
}

#[cfg(target_os = "macos")]
fn environment(pid: u32) -> Result<Vec<u8>> {
    let mut mib = [libc::CTL_KERN, libc::KERN_PROCARGS2, pid as i32];
    let mut bytes = vec![0u8; 1024 * 1024];
    let mut size = bytes.len();
    if unsafe {
        libc::sysctl(
            mib.as_mut_ptr(),
            3,
            bytes.as_mut_ptr().cast(),
            &mut size,
            std::ptr::null_mut(),
            0,
        )
    } != 0
    {
        let e = io::Error::last_os_error();
        return Err(io::Error::new(e.kind(), format!("KERN_PROCARGS2({pid}): {e}")).into());
    }
    bytes.truncate(size);
    // Skip argc, executable path, padding and exactly argc argv strings.
    if size < 4 {
        return Err(conflict("missing process environment"));
    }
    let argc = i32::from_ne_bytes(bytes[..4].try_into().unwrap());
    if argc < 0 {
        return Err(conflict("invalid process arguments"));
    }
    let mut at = 4;
    while at < size && bytes[at] != 0 {
        at += 1;
    }
    while at < size && bytes[at] == 0 {
        at += 1;
    }
    for _ in 0..argc {
        while at < size && bytes[at] != 0 {
            at += 1;
        }
        at += 1;
    }
    if at > size {
        return Err(conflict("incomplete process environment"));
    }
    Ok(bytes[at..].to_vec())
}

pub fn has_token(id: &Identity, token: &str) -> Result<bool> {
    let expected = format!("SUPABRICKS_PROCESS_TOKEN={token}");
    let bytes = match environment(id.pid) {
        Ok(b) => b,
        Err(e) => {
            if identity(id.pid)?.is_none_or(|i| i.zombie) {
                return Ok(false);
            }
            return Err(e);
        }
    };
    Ok(bytes.split(|b| *b == 0).any(|e| e == expected.as_bytes())
        && identity(id.pid)?
            .is_some_and(|i| i.start == id.start && i.group == id.group && i.uid == id.uid))
}
