//! Windows Computer Use's ordinary-user, private-object and interactive boundary.
//! No repair, elevation, desktop switching, or endpoint reclamation occurs here.
//! This crate confines Win32 FFI so runtime-host retains `forbid(unsafe_code)`.

#![cfg(windows)]
#![deny(unsafe_op_in_unsafe_fn)]

use std::ffi::OsStr;
use std::fs::File;
use std::io::{self, Read};
use std::mem::{size_of, size_of_val, zeroed};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, BorrowedHandle, FromRawHandle, OwnedHandle};
use std::path::Path;
use std::ptr::null_mut;

use windows_sys::Win32::Foundation::{LocalFree, HANDLE};
use windows_sys::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW, GetSecurityInfo, SDDL_REVISION_1,
    SE_FILE_OBJECT,
};
use windows_sys::Win32::Security::{
    EqualSid, GetAce, GetTokenInformation, TokenElevation, TokenIntegrityLevel, TokenUIAccess, TokenUser,
    ACCESS_ALLOWED_ACE, ACL, DACL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR,
    SECURITY_ATTRIBUTES, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION, CREATE_NEW, FILE_ATTRIBUTE_NORMAL,
    FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    OPEN_ALWAYS, OPEN_EXISTING,
};
use windows_sys::Win32::System::Pipes::GetNamedPipeServerProcessId;
use windows_sys::Win32::System::RemoteDesktop::{
    ProcessIdToSessionId, WTSActive, WTSConnectState, WTSFreeMemory, WTSQuerySessionInformationW,
};
use windows_sys::Win32::System::StationsAndDesktops::{
    CloseDesktop, GetProcessWindowStation, GetThreadDesktop, GetUserObjectInformationW, OpenInputDesktop,
    DESKTOP_READOBJECTS, UOI_NAME,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetCurrentThreadId, OpenProcessToken};

fn refused(reason: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, reason)
}
fn check(result: i32) -> io::Result<()> {
    if result == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
fn wide(value: &OsStr) -> io::Result<Vec<u16>> {
    let mut value: Vec<_> = value.encode_wide().collect();
    if value.contains(&0) {
        return Err(refused("invalid_path"));
    }
    value.push(0);
    Ok(value)
}
fn token() -> io::Result<OwnedHandle> {
    let mut token = null_mut();
    check(unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) })?;
    Ok(unsafe { OwnedHandle::from_raw_handle(token) })
}
fn information(token: &OwnedHandle, kind: i32) -> io::Result<Vec<usize>> {
    let mut size = 0;
    unsafe { GetTokenInformation(token.as_raw_handle(), kind, null_mut(), 0, &mut size) };
    if size == 0 || size > 65536 {
        return Err(refused("token_unverified"));
    }
    let mut bytes = vec![0usize; (size as usize).div_ceil(size_of::<usize>())];
    check(unsafe { GetTokenInformation(token.as_raw_handle(), kind, bytes.as_mut_ptr().cast(), size, &mut size) })?;
    Ok(bytes)
}

/// Token user, not TokenOwner (which can be BUILTIN\\Administrators).
fn user_sid(token: &OwnedHandle) -> io::Result<Vec<usize>> {
    information(token, TokenUser)
}

pub fn require_ordinary_user() -> io::Result<()> {
    let token = token()?;
    for kind in [TokenElevation, TokenUIAccess] {
        let value = information(&token, kind)?;
        if unsafe { *value.as_ptr().cast::<u32>() } != 0 {
            return Err(refused("elevated_shell"));
        }
    }
    let label = information(&token, TokenIntegrityLevel)?;
    let sid = unsafe { *label.as_ptr().cast::<*const u8>() };
    let offset = (sid as usize)
        .checked_sub(label.as_ptr() as usize)
        .ok_or_else(|| refused("token_unverified"))?;
    if offset < size_of::<TOKEN_USER>() || offset + 12 > label.len() * size_of::<usize>() {
        return Err(refused("token_unverified"));
    }
    let sid = unsafe { std::slice::from_raw_parts(sid, 12) };
    if sid[..8] != [1, 1, 0, 0, 0, 0, 0, 16] || u32::from_le_bytes(sid[8..12].try_into().unwrap()) > 0x2000 {
        return Err(refused("elevated_shell"));
    }
    Ok(())
}

pub fn interactive_session() -> io::Result<u32> {
    let mut session = 0;
    check(unsafe { ProcessIdToSessionId(std::process::id(), &mut session) })?;
    if session == 0 {
        return Err(refused("session_unavailable"));
    }
    let (mut state, mut size) = (null_mut(), 0);
    check(unsafe { WTSQuerySessionInformationW(null_mut(), session, WTSConnectState, &mut state, &mut size) })?;
    let active = !state.is_null() && size as usize == size_of::<i32>() && unsafe { *state.cast::<i32>() } == WTSActive;
    unsafe { WTSFreeMemory(state.cast()) };
    if !active {
        return Err(refused("session_unavailable"));
    }
    if unsafe { GetProcessWindowStation() }.is_null() {
        return Err(refused("session_unavailable"));
    }
    let thread_desktop = unsafe { GetThreadDesktop(GetCurrentThreadId()) };
    if thread_desktop.is_null() || !desktop_name(thread_desktop)?.eq_ignore_ascii_case("default") {
        return Err(refused("session_unavailable"));
    }
    let desktop = unsafe { OpenInputDesktop(0, 0, DESKTOP_READOBJECTS) };
    if desktop.is_null() {
        return Err(refused("session_locked"));
    }
    let name = desktop_name(desktop);
    unsafe { CloseDesktop(desktop) };
    if !name?.eq_ignore_ascii_case("default") {
        return Err(refused("session_locked"));
    }
    Ok(session)
}

fn desktop_name(desktop: HANDLE) -> io::Result<String> {
    let mut name = [0u16; 256];
    let mut needed = 0;
    check(unsafe {
        GetUserObjectInformationW(
            desktop,
            UOI_NAME,
            name.as_mut_ptr().cast(),
            size_of_val(&name) as u32,
            &mut needed,
        )
    })?;
    let end = name
        .iter()
        .position(|value| *value == 0)
        .ok_or_else(|| refused("session_unavailable"))?;
    Ok(String::from_utf16_lossy(&name[..end]))
}

struct Descriptor(PSECURITY_DESCRIPTOR);
impl Drop for Descriptor {
    fn drop(&mut self) {
        unsafe { LocalFree(self.0) };
    }
}
fn descriptor(value: &str) -> io::Result<Descriptor> {
    let value = wide(OsStr::new(value))?;
    let mut descriptor = null_mut();
    check(unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            value.as_ptr(),
            SDDL_REVISION_1,
            &mut descriptor,
            null_mut(),
        )
    })?;
    Ok(Descriptor(descriptor))
}
fn sid_string(sid: *mut std::ffi::c_void) -> io::Result<String> {
    let mut text = null_mut();
    check(unsafe { ConvertSidToStringSidW(sid, &mut text) })?;
    let result = unsafe {
        let mut len = 0;
        while *text.add(len) != 0 {
            len += 1;
        }
        String::from_utf16_lossy(std::slice::from_raw_parts(text, len))
    };
    unsafe { LocalFree(text.cast()) };
    Ok(result)
}

/// Verify the descriptor of the already-open object, never a pathname lookup.
fn require_private_handle(handle: HANDLE) -> io::Result<()> {
    let (mut owner, mut dacl, mut sd) = (null_mut(), null_mut::<ACL>(), null_mut());
    let result = unsafe {
        GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            &mut dacl,
            null_mut(),
            &mut sd,
        )
    };
    if result != 0 {
        return Err(io::Error::from_raw_os_error(result as i32));
    }
    let _descriptor = Descriptor(sd);
    let own_storage = user_sid(&token()?)?;
    let own = unsafe { (*own_storage.as_ptr().cast::<TOKEN_USER>()).User.Sid };
    if own.is_null() {
        return Err(refused("object_unverified"));
    }
    if owner.is_null() || dacl.is_null() || unsafe { EqualSid(owner, own) } == 0 {
        return Err(refused("object_unverified"));
    }
    for index in 0..unsafe { (*dacl).AceCount } {
        let mut ace = null_mut();
        check(unsafe { GetAce(dacl, index as u32, &mut ace) })?;
        let ace = unsafe { &*ace.cast::<ACCESS_ALLOWED_ACE>() };
        if ace.Header.AceType == 1 {
            continue;
        }
        if ace.Header.AceType != 0 || ace.Header.AceSize < 16 {
            return Err(refused("object_unverified"));
        }
        let sid = (&ace.SidStart as *const u32).cast_mut().cast();
        if unsafe { EqualSid(sid, own) } == 0 && sid_string(sid)? != "S-1-5-18" {
            return Err(refused("object_unverified"));
        }
    }
    Ok(())
}

/// Create private state/lock objects without a world-readable intermediate.
/// Existing locks must already be safe; no ACL repair or reparse traversal.
pub fn open_private_file(path: &Path, create_new: bool) -> io::Result<File> {
    open_private(path, if create_new { CREATE_NEW } else { OPEN_ALWAYS }, 0xC0000000)
}

pub fn read_private_file(path: &Path) -> io::Result<Vec<u8>> {
    let mut file = open_private(path, OPEN_EXISTING, 0x80000000)?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    Ok(bytes)
}

fn open_private(path: &Path, disposition: u32, access: u32) -> io::Result<File> {
    let own = user_sid(&token()?)?;
    let sid = sid_string(unsafe { (*own.as_ptr().cast::<TOKEN_USER>()).User.Sid })?;
    let descriptor = descriptor(&format!("O:{sid}D:P(A;;FA;;;{sid})(A;;FA;;;SY)"))?;
    let attrs = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.0,
        bInheritHandle: 0,
    };
    let path = wide(path.as_os_str())?;
    let handle = unsafe {
        CreateFileW(
            path.as_ptr(),
            access,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            &attrs,
            disposition,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            null_mut(),
        )
    };
    if handle as isize == -1 {
        return Err(io::Error::last_os_error());
    }
    let file = unsafe { File::from_raw_handle(handle) };
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { zeroed() };
    check(unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) })?;
    if info.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | 0x10) != 0 {
        return Err(refused("object_unverified"));
    }
    require_private_handle(file.as_raw_handle())?;
    Ok(file)
}

/// Called while the shell retains the exact live OwnedDriver process HANDLE.
/// Verify a live, borrowed pipe without taking ownership or retaining it.
/// The borrow keeps the handle valid for every native query in this call.
pub fn verify_owned_pipe(pipe: BorrowedHandle<'_>, expected_pid: u32) -> io::Result<()> {
    let handle = pipe.as_raw_handle();
    require_ordinary_user()?;
    let session = interactive_session()?;
    require_private_handle(handle)?;
    let mut pid = 0;
    check(unsafe { GetNamedPipeServerProcessId(handle, &mut pid) })?;
    if pid == 0 || pid != expected_pid {
        return Err(refused("pipe_unverified"));
    }
    let mut peer_session = 0;
    check(unsafe { ProcessIdToSessionId(pid, &mut peer_session) })?;
    if peer_session != session {
        return Err(refused("session_mismatch"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows_sys::Win32::Security::Authorization::SetSecurityInfo;
    use windows_sys::Win32::Security::{GetSecurityDescriptorDacl, PROTECTED_DACL_SECURITY_INFORMATION};

    #[test]
    fn private_file_creation_locking_and_unsafe_existing_acl_refusal() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("computer-use.lock");
        assert!(read_private_file(&path).is_err());
        assert!(!path.exists(), "a read must not create missing state");
        let file = open_private_file(&path, false).unwrap();
        assert_eq!(read_private_file(&path).unwrap(), Vec::<u8>::new());
        file.try_lock().unwrap();
        let contender = open_private_file(&path, false).unwrap();
        assert!(contender.try_lock().is_err());
        drop(contender);
        file.unlock().unwrap();
        // An existing foreign-readable file must be refused, never repaired.
        let unsafe_sd = descriptor("D:P(A;;FA;;;WD)").unwrap();
        let (mut present, mut defaulted, mut dacl) = (0, 0, null_mut());
        check(unsafe { GetSecurityDescriptorDacl(unsafe_sd.0, &mut present, &mut dacl, &mut defaulted) }).unwrap();
        assert_ne!(present, 0);
        assert_eq!(
            unsafe {
                SetSecurityInfo(
                    file.as_raw_handle(),
                    SE_FILE_OBJECT,
                    DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                    null_mut(),
                    null_mut(),
                    dacl,
                    null_mut(),
                )
            },
            0
        );
        assert!(require_private_handle(file.as_raw_handle()).is_err());
        drop(file);
        assert!(open_private_file(&path, false).is_err());
        assert!(read_private_file(&path).is_err());
    }
}
