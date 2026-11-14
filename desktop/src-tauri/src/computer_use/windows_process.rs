//! Windows x64 child ownership, deliberately not a Computer Use enablement path.
//!
//! A child is born suspended *inside* an unnamed kill-on-close job. Assigning a
//! job after CreateProcess returns would leave an orphan if the shell died in
//! between. PROC_THREAD_ATTRIBUTE_JOB_LIST closes that gap (Windows 10+).
//! No shell, elevation, breakaway, pipe admission, or resource discovery here.

use std::ffi::{OsStr, OsString};
use std::fs::{File, OpenOptions};
use std::io;
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::path::Path;
use std::ptr::{null, null_mut};
use std::time::Duration;

use windows_sys::Win32::Foundation::{SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT, WAIT_OBJECT_0, WAIT_TIMEOUT};
use windows_sys::Win32::Security::{GetTokenInformation, TokenElevation, TOKEN_ELEVATION, TOKEN_QUERY};
use windows_sys::Win32::System::JobObjects::{
    CreateJobObjectW, IsProcessInJob, JobObjectBasicAccountingInformation, JobObjectExtendedLimitInformation,
    QueryInformationJobObject, SetInformationJobObject, TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, GetCurrentProcess, InitializeProcThreadAttributeList,
    OpenProcessToken, ResumeThread, UpdateProcThreadAttribute, WaitForSingleObject, CREATE_NO_WINDOW, CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT, EXTENDED_STARTUPINFO_PRESENT, LPPROC_THREAD_ATTRIBUTE_LIST, PROCESS_INFORMATION,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROC_THREAD_ATTRIBUTE_JOB_LIST, STARTF_USESTDHANDLES, STARTUPINFOEXW,
};

const FORCE_STOP_LIMIT: Duration = Duration::from_secs(3);
const POLL_INTERVAL: Duration = Duration::from_millis(20);

#[cfg(test)]
#[path = "windows_process_tests.rs"]
mod tests;

/// The caller supplies its approved executable, arguments, working directory,
/// and complete environment. Nothing is implicitly copied from the shell.
/// Dropping at any point, including before resume or during an async stop,
/// closes the non-inherited job handle and terminates its entire process tree.
#[must_use]
pub(crate) struct OwnedDriver {
    job: OwnedHandle,
    process: OwnedHandle,
    initial_thread: Option<OwnedHandle>,
    stdin: Option<OwnedHandle>,
    pid: u32,
}

impl OwnedDriver {
    pub(crate) fn spawn_suspended(
        executable: &Path,
        args: &[OsString],
        environment: &[(OsString, OsString)],
        directory: &Path,
    ) -> io::Result<Self> {
        require_ordinary_user()?;
        if !executable.is_absolute() || !directory.is_absolute() {
            return Err(invalid("driver executable and working directory must be absolute"));
        }
        let application = wide(executable.as_os_str())?;
        let directory = wide(directory.as_os_str())?;
        let mut command = command_line(executable.as_os_str(), args)?;
        let environment = environment_block(environment)?;
        // SAFETY: the handles created below are unique, unnamed, and not
        // inheritable. Each successful API allocation immediately has an owner.
        let job = unsafe { owned(CreateJobObjectW(null(), null()))? };
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        check(unsafe {
            SetInformationJobObject(
                job.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        })?;
        let (stdin_read, stdin_write) = stdin_pipe()?;
        let output: File = OpenOptions::new().write(true).open("NUL")?;
        check(unsafe { SetHandleInformation(output.as_raw_handle(), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) })?;
        let jobs = [job.as_raw_handle()];
        let handles = [stdin_read.as_raw_handle(), output.as_raw_handle()];
        // Attribute values outlive the list, as required by Win32. Neither the
        // job handle nor the stdin writer appears in the inherited handle list.
        let attributes = Attributes::new(&jobs, &handles)?;
        let mut startup: STARTUPINFOEXW = unsafe { zeroed() };
        startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = stdin_read.as_raw_handle();
        startup.StartupInfo.hStdOutput = output.as_raw_handle();
        startup.StartupInfo.hStdError = output.as_raw_handle();
        startup.lpAttributeList = attributes.pointer();
        let mut info: PROCESS_INFORMATION = unsafe { zeroed() };
        // Creation either fails without a child or atomically joins the job.
        // Incompatible enclosing jobs fail closed; there is no unowned retry.
        check(unsafe {
            CreateProcessW(
                application.as_ptr(),
                command.as_mut_ptr(),
                null(),
                null(),
                1,
                CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
                environment.as_ptr().cast(),
                directory.as_ptr(),
                &startup.StartupInfo,
                &mut info,
            )
        })?;
        let child = Self {
            job,
            // SAFETY: successful CreateProcessW returned these owned handles.
            process: unsafe { OwnedHandle::from_raw_handle(info.hProcess) },
            initial_thread: Some(unsafe { OwnedHandle::from_raw_handle(info.hThread) }),
            stdin: Some(stdin_write),
            pid: info.dwProcessId,
        };
        let mut in_job = 0;
        check(unsafe { IsProcessInJob(child.process.as_raw_handle(), child.job.as_raw_handle(), &mut in_job) })?;
        if in_job == 0 {
            return Err(io::Error::other("driver did not join its owned job"));
        }
        Ok(child)
    }

    /// Consumes the suspended owner so a failed resume also tears it down.
    pub(crate) fn resume(mut self) -> io::Result<Self> {
        let thread = self
            .initial_thread
            .take()
            .ok_or_else(|| invalid("driver was already resumed"))?;
        // SAFETY: this is the initial thread created suspended above; no other
        // party owns this handle or is asked to change its suspend count.
        let previous = unsafe { ResumeThread(thread.as_raw_handle()) };
        if previous == u32::MAX {
            return Err(io::Error::last_os_error());
        }
        if previous != 1 {
            return Err(io::Error::other("unexpected driver suspend count"));
        }
        Ok(self)
    }

    pub(crate) fn id(&self) -> u32 {
        self.pid
    }

    pub(crate) fn has_exited(&self) -> io::Result<bool> {
        match unsafe { WaitForSingleObject(self.process.as_raw_handle(), 0) } {
            WAIT_OBJECT_0 => Ok(true),
            WAIT_TIMEOUT => Ok(false),
            _ => Err(io::Error::last_os_error()),
        }
    }

    /// Close parent-liveness stdin, then allow a bounded graceful exit. Always
    /// terminate remaining descendants, even if the direct child already exited.
    /// Cancellation drops this owner and triggers the same job-wide cleanup.
    /// Success means the job has no active processes, not merely that a kill was
    /// requested. A timeout/error must not be treated as a successful restart.
    pub(crate) async fn stop(mut self, grace: Duration) -> io::Result<()> {
        drop(self.stdin.take());
        // A suspended child's stdin cannot be read, so do not delay its abort.
        if self.initial_thread.is_none() && self.wait_empty(grace).await? {
            return Ok(());
        }
        check(unsafe { TerminateJobObject(self.job.as_raw_handle(), 1) })?;
        if self.wait_empty(FORCE_STOP_LIMIT).await? {
            Ok(())
        } else {
            Err(io::Error::new(io::ErrorKind::TimedOut, "driver job did not terminate"))
        }
    }

    async fn wait_empty(&self, limit: Duration) -> io::Result<bool> {
        let deadline = tokio::time::Instant::now() + limit;
        loop {
            let mut accounting: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { zeroed() };
            check(unsafe {
                QueryInformationJobObject(
                    self.job.as_raw_handle(),
                    JobObjectBasicAccountingInformation,
                    (&mut accounting as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                    null_mut(),
                )
            })?;
            if accounting.ActiveProcesses == 0 {
                return Ok(true);
            }
            if tokio::time::Instant::now() >= deadline {
                return Ok(false);
            }
            tokio::time::sleep_until((tokio::time::Instant::now() + POLL_INTERVAL).min(deadline)).await;
        }
    }
}

// No custom Drop is necessary: OwnedHandle closes the job even on panic or
// cancellation, and the OS closes it if the shell is terminated outright.

fn require_ordinary_user() -> io::Result<()> {
    let mut token = null_mut();
    check(unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) })?;
    let token = unsafe { OwnedHandle::from_raw_handle(token) };
    let mut elevation: TOKEN_ELEVATION = unsafe { zeroed() };
    let mut size = 0;
    check(unsafe {
        GetTokenInformation(
            token.as_raw_handle(),
            TokenElevation,
            (&mut elevation as *mut TOKEN_ELEVATION).cast(),
            size_of::<TOKEN_ELEVATION>() as u32,
            &mut size,
        )
    })?;
    if elevation.TokenIsElevated != 0 {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "elevated_shell"));
    }
    Ok(())
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn check(success: i32) -> io::Result<()> {
    if success == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

unsafe fn owned(handle: HANDLE) -> io::Result<OwnedHandle> {
    if handle.is_null() {
        Err(io::Error::last_os_error())
    } else {
        // SAFETY: callers transfer a newly created non-null Win32 handle.
        Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
    }
}

fn stdin_pipe() -> io::Result<(OwnedHandle, OwnedHandle)> {
    let (mut read, mut write) = (null_mut(), null_mut());
    check(unsafe { CreatePipe(&mut read, &mut write, null(), 0) })?;
    let read = unsafe { OwnedHandle::from_raw_handle(read) };
    let write = unsafe { OwnedHandle::from_raw_handle(write) };
    check(unsafe { SetHandleInformation(read.as_raw_handle(), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) })?;
    Ok((read, write))
}

/// Pointer-aligned opaque Win32 storage. The borrowed job/stdio arrays must
/// remain alive until after DeleteProcThreadAttributeList.
struct Attributes<'a> {
    storage: Vec<usize>,
    _values: std::marker::PhantomData<&'a [HANDLE]>,
}

impl<'a> Attributes<'a> {
    fn new(jobs: &'a [HANDLE], handles: &'a [HANDLE]) -> io::Result<Self> {
        let mut bytes = 0;
        unsafe { InitializeProcThreadAttributeList(null_mut(), 2, 0, &mut bytes) };
        if bytes == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut storage = vec![0_usize; bytes.div_ceil(size_of::<usize>())];
        check(unsafe { InitializeProcThreadAttributeList(storage.as_mut_ptr().cast(), 2, 0, &mut bytes) })?;
        let list = Self {
            storage,
            _values: std::marker::PhantomData,
        };
        for (attribute, values) in [
            (PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs),
            (PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles),
        ] {
            check(unsafe {
                UpdateProcThreadAttribute(
                    list.pointer(),
                    0,
                    attribute as usize,
                    values.as_ptr().cast(),
                    std::mem::size_of_val(values),
                    null_mut(),
                    null(),
                )
            })?;
        }
        Ok(list)
    }

    fn pointer(&self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
        self.storage.as_ptr().cast_mut().cast()
    }
}

impl Drop for Attributes<'_> {
    fn drop(&mut self) {
        unsafe { DeleteProcThreadAttributeList(self.pointer()) };
    }
}

fn wide(value: &OsStr) -> io::Result<Vec<u16>> {
    let mut result: Vec<_> = value.encode_wide().collect();
    if result.contains(&0) {
        return Err(invalid("NUL in process launch input"));
    }
    result.push(0);
    Ok(result)
}

fn command_line(executable: &OsStr, args: &[OsString]) -> io::Result<Vec<u16>> {
    let mut result = Vec::new();
    // Direct native executable only: quote for the MSVC/Rust argv parser, never
    // cmd.exe. Every argument is quoted, including empty strings.
    for value in std::iter::once(executable).chain(args.iter().map(OsString::as_os_str)) {
        if !result.is_empty() {
            result.push(b' ' as u16);
        }
        result.push(b'"' as u16);
        let mut slashes = 0;
        for unit in wide(value)?.into_iter().take_while(|unit| *unit != 0) {
            if unit == b'\\' as u16 {
                slashes += 1;
                continue;
            }
            let count = if unit == b'"' as u16 { slashes * 2 + 1 } else { slashes };
            result.extend(std::iter::repeat_n(b'\\' as u16, count));
            result.push(unit);
            slashes = 0;
        }
        result.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2));
        result.push(b'"' as u16);
    }
    result.push(0);
    if result.len() > 32767 {
        return Err(invalid("driver command line too long"));
    }
    Ok(result)
}

fn environment_block(environment: &[(OsString, OsString)]) -> io::Result<Vec<u16>> {
    // Environment keys are deliberately ASCII: the platform and pinned driver
    // keys needed by the eventual adapter are ASCII, with no ambiguous casing.
    let mut entries = Vec::new();
    for (key, value) in environment {
        let key = key
            .to_str()
            .filter(|key| !key.is_empty() && key.is_ascii() && !key.contains(['=', '\0']))
            .ok_or_else(|| invalid("invalid driver environment key"))?;
        let value = wide(value)?;
        entries.push((key.to_ascii_uppercase(), value));
    }
    entries.sort_by(|a, b| a.0.cmp(&b.0));
    if entries.windows(2).any(|pair| pair[0].0 == pair[1].0) {
        return Err(invalid("duplicate driver environment key"));
    }
    let mut result = Vec::new();
    for (key, value) in entries {
        result.extend(key.encode_utf16());
        result.push(b'=' as u16);
        result.extend(value);
    }
    if result.is_empty() {
        result.push(0);
    }
    result.push(0);
    Ok(result)
}
