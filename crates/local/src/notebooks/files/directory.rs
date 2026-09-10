//! Resolve notebook paths relative to open directories without following symlinks.
use crate::store::{
    Result,
    error::{conflict, invalid},
};
use std::{
    ffi::{CStr, CString, OsStr},
    fs::{File, OpenOptions},
    io,
    os::{
        fd::{AsRawFd, FromRawFd, IntoRawFd},
        unix::{ffi::OsStrExt, fs::OpenOptionsExt},
    },
    path::Path,
};
pub struct Directory(File);
fn name(value: &OsStr) -> Result<CString> {
    CString::new(value.as_bytes()).map_err(|_| invalid("invalid notebook name"))
}
impl Directory {
    pub fn move_to(&self, source: &OsStr, destination: &Directory, name_to: &OsStr) -> Result<()> {
        let a = name(source)?;
        let b = name(name_to)?;
        #[cfg(target_os = "linux")]
        let status = unsafe {
            libc::syscall(
                libc::SYS_renameat2,
                self.0.as_raw_fd(),
                a.as_ptr(),
                destination.0.as_raw_fd(),
                b.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        };
        #[cfg(target_os = "macos")]
        let status = unsafe {
            libc::renameatx_np(
                self.0.as_raw_fd(),
                a.as_ptr(),
                destination.0.as_raw_fd(),
                b.as_ptr(),
                libc::RENAME_EXCL,
            )
        };
        if status < 0 {
            let e = io::Error::last_os_error();
            return Err(if e.kind() == io::ErrorKind::AlreadyExists {
                conflict("Notebook already exists; choose another name")
            } else {
                e.into()
            });
        }
        self.0.sync_all()?;
        destination.0.sync_all()?;
        Ok(())
    }
    pub fn project(path: &Path) -> Result<Self> {
        Ok(Self(
            OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(path)?,
        ))
    }
    pub fn child(&self, value: &OsStr, create: bool) -> Result<Self> {
        let n = name(value)?;
        if create && unsafe { libc::mkdirat(self.0.as_raw_fd(), n.as_ptr(), 0o755) } < 0 {
            let e = io::Error::last_os_error();
            if e.kind() != io::ErrorKind::AlreadyExists {
                return Err(e.into());
            }
        }
        Ok(Self(self.open(value, libc::O_RDONLY | libc::O_DIRECTORY)?))
    }
    pub fn open(&self, value: &OsStr, flags: i32) -> Result<File> {
        let n = name(value)?;
        let fd = unsafe {
            libc::openat(
                self.0.as_raw_fd(),
                n.as_ptr(),
                flags | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK,
                0o600,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error().into());
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }
    pub fn exists(&self, value: &OsStr) -> Result<bool> {
        let n = name(value)?;
        let mut st = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe {
            libc::fstatat(
                self.0.as_raw_fd(),
                n.as_ptr(),
                st.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        } == 0
        {
            return Ok(true);
        }
        let e = io::Error::last_os_error();
        if e.kind() == io::ErrorKind::NotFound {
            Ok(false)
        } else {
            Err(e.into())
        }
    }
    pub fn unlink(&self, value: &OsStr) -> Result<()> {
        let n = name(value)?;
        if unsafe { libc::unlinkat(self.0.as_raw_fd(), n.as_ptr(), 0) } < 0 {
            return Err(io::Error::last_os_error().into());
        }
        Ok(())
    }
    pub fn publish(&self, temporary: &OsStr, destination: &OsStr, replace: bool) -> Result<()> {
        let a = name(temporary)?;
        let b = name(destination)?;
        let fd = self.0.as_raw_fd();
        let status = unsafe {
            if replace {
                libc::renameat(fd, a.as_ptr(), fd, b.as_ptr())
            } else {
                libc::linkat(fd, a.as_ptr(), fd, b.as_ptr(), 0)
            }
        };
        if status < 0 {
            let e = io::Error::last_os_error();
            return Err(if e.kind() == io::ErrorKind::AlreadyExists {
                conflict("Notebook already exists; open it before saving")
            } else {
                e.into()
            });
        }
        if !replace {
            self.unlink(temporary)?;
        }
        self.0.sync_all()?;
        Ok(())
    }
    pub fn entries(&self, budget: &mut usize) -> Result<Vec<(String, u32)>> {
        let fd = self
            .open(OsStr::new("."), libc::O_RDONLY | libc::O_DIRECTORY)?
            .into_raw_fd();
        let ptr = unsafe { libc::fdopendir(fd) };
        if ptr.is_null() {
            let e = io::Error::last_os_error();
            unsafe {
                libc::close(fd);
            }
            return Err(e.into());
        }
        struct Entries(*mut libc::DIR);
        impl Drop for Entries {
            fn drop(&mut self) {
                unsafe {
                    libc::closedir(self.0);
                }
            }
        }
        let entries = Entries(ptr);
        let mut result = Vec::new();
        loop {
            // readdir uses null for both EOF and errors; reset errno to distinguish them.
            #[cfg(target_os = "linux")]
            unsafe {
                *libc::__errno_location() = 0;
            }
            #[cfg(target_os = "macos")]
            unsafe {
                *libc::__error() = 0;
            }
            let entry = unsafe { libc::readdir(entries.0) };
            if entry.is_null() {
                let error = io::Error::last_os_error();
                if error.raw_os_error() != Some(0) {
                    return Err(error.into());
                }
                break;
            }
            let bytes = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
            if bytes == b"." || bytes == b".." {
                continue;
            }
            if *budget == 0 {
                return Err(invalid(
                    "Notebook directory has too many entries (maximum 4096)",
                ));
            }
            *budget -= 1;
            let n = CString::new(bytes).unwrap();
            let mut st = std::mem::MaybeUninit::<libc::stat>::uninit();
            if unsafe {
                libc::fstatat(
                    self.0.as_raw_fd(),
                    n.as_ptr(),
                    st.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            } < 0
            {
                return Err(io::Error::last_os_error().into());
            }
            let kind = unsafe { st.assume_init().st_mode } as u32 & libc::S_IFMT as u32;
            if kind == libc::S_IFLNK as u32 {
                return Err(conflict("Notebook tree contains a symlink"));
            }
            if let Ok(n) = std::str::from_utf8(bytes) {
                result.push((n.into(), kind));
            }
        }
        Ok(result)
    }
}
