//! The released credential is the commit record. A large record selects one of two fixed banks;
//! only its entropy opens that generation. An interrupted staging write never changes the root.
mod os;
#[cfg(test)]
mod tests;

use super::{ClientError, Credentials, NativeCredentials};
use avibe_runtime_host::{hashing::lower_hex, instance_client::storage::CredentialWrite};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions, TryLockError},
    os::windows::fs::OpenOptionsExt,
    sync::Arc,
    time::{Duration, Instant},
};
use windows_sys::Win32::Security::Credentials::CRED_MAX_CREDENTIAL_BLOB_SIZE;
use zeroize::Zeroizing;

type Result<T> = std::result::Result<T, ClientError>;
const CHUNK: usize = CRED_MAX_CREDENTIAL_BLOB_SIZE as usize;
const MAX_LOCK_WAIT: Duration = Duration::from_secs(5);
const LOCK_RETRY: Duration = Duration::from_millis(40);
const MAX_JAR: usize = 64 * 1024;
const MAX_SEALED: usize = 72 * 1024;
const SLOTS: usize = MAX_SEALED.div_ceil(CHUNK);
const MAGIC: &[u8; 5] = b"\xffAVJ\x01";
fn unavailable() -> ClientError {
    ClientError::new("credentials_unavailable")
}

fn read_entry(entry: &keyring::Entry) -> Result<Option<Vec<u8>>> {
    match entry.get_secret() {
        Ok(blob) => Ok(Some(blob)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(_) => Err(unavailable()),
    }
}
fn delete(entry: &keyring::Entry) -> Result<()> {
    match entry.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(_) => Err(unavailable()),
    }
}

struct Manifest {
    bank: u8,
    len: usize,
    nonce: [u8; 32],
}
impl Manifest {
    fn parse(root: &[u8]) -> Result<Self> {
        let data = root.strip_prefix(MAGIC.as_slice()).ok_or_else(unavailable)?;
        if data.len() != 37 || data[0] > 1 {
            return Err(unavailable());
        }
        let len = u32::from_le_bytes(data[1..5].try_into().map_err(|_| unavailable())?) as usize;
        if len == 0 || len > MAX_SEALED {
            return Err(unavailable());
        }
        Ok(Self {
            bank: data[0],
            len,
            nonce: data[5..].try_into().map_err(|_| unavailable())?,
        })
    }
    fn encode(&self) -> Vec<u8> {
        [
            MAGIC.as_slice(),
            &[self.bank],
            &(self.len as u32).to_le_bytes(),
            self.nonce.as_slice(),
        ]
        .concat()
    }
}

// Each API call/checkpoint runs on a blocking worker. Use one owner-local lock for all scopes so
// case aliases cannot take distinct locks. The file contains no material; OS close/death releases it.
struct Scope {
    root: keyring::Entry,
    namespace: String,
    _lock: File,
}
impl Scope {
    fn open(owner: &NativeCredentials, id: &str) -> Result<Self> {
        if id.contains('\0') || owner.service.contains('\0') {
            return Err(unavailable());
        }
        let root = owner.entry(id)?;
        // Unit-test binaries never fall back to the user's real Desktop lock.
        #[cfg(test)]
        let local = tests::fixture_local_data();
        #[cfg(not(test))]
        let local = dirs::data_local_dir();
        let dir = local.ok_or_else(unavailable)?.join("Avibe").join("credential-locks");
        fs::create_dir_all(&dir).map_err(|_| unavailable())?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .share_mode(3) // FILE_SHARE_READ | FILE_SHARE_WRITE: no rename/unlink while locked
            .open(dir.join("instance-client.lock"))
            .map_err(|_| unavailable())?;
        let deadline = Instant::now() + MAX_LOCK_WAIT;
        loop {
            match lock.try_lock() {
                Ok(()) => break,
                Err(TryLockError::Error(_)) => return Err(unavailable()),
                Err(TryLockError::WouldBlock) => {
                    let remaining = deadline.saturating_duration_since(Instant::now());
                    std::thread::sleep(remaining.min(LOCK_RETRY));
                    if Instant::now() >= deadline {
                        return Err(unavailable());
                    }
                }
            }
        }
        let mut hash = Sha256::new();
        hash.update(b"avibe/desktop/account-jar/scope/v1\0");
        for part in [&*owner.service, id] {
            let canonical = part.to_lowercase();
            hash.update((canonical.len() as u64).to_le_bytes());
            hash.update(canonical.as_bytes());
        }
        Ok(Self {
            root,
            namespace: format!("AvibeJar:v1:{}", lower_hex(&hash.finalize())),
            _lock: lock,
        })
    }
    fn target(&self, bank: u8, index: usize) -> String {
        format!("{}:{bank}:{index}", self.namespace)
    }
    fn chunk(&self, bank: u8, index: usize) -> Result<keyring::Entry> {
        keyring::Entry::new_with_target(&self.target(bank, index), "AvibeJar", "ciphertext").map_err(|_| unavailable())
    }
    fn entropy(&self, nonce: &[u8; 32]) -> [u8; 32] {
        let mut h = Sha256::new();
        h.update(b"avibe/desktop/account-jar/DPAPI/v1\0");
        h.update(self.namespace.as_bytes());
        h.update(nonce);
        h.finalize().into()
    }
    fn read(&self) -> Result<Option<Vec<u8>>> {
        let Some(root) = read_entry(&self.root)? else {
            return Ok(None);
        };
        if root.first() != Some(&0xff) {
            return Ok(Some(root));
        }
        let m = Manifest::parse(&root)?;
        let mut sealed = Vec::with_capacity(m.len);
        for i in 0..m.len.div_ceil(CHUNK) {
            let piece = read_entry(&self.chunk(m.bank, i)?)?.ok_or_else(unavailable)?;
            if piece.len() != (m.len - sealed.len()).min(CHUNK) {
                return Err(unavailable());
            }
            sealed.extend(piece);
        }
        let plain = os::dpapi(false, &sealed, &self.entropy(&m.nonce))?;
        if !(CHUNK + 1..=MAX_JAR).contains(&plain.len()) || read_entry(&self.root)?.as_ref() != Some(&root) {
            return Err(unavailable());
        }
        Ok(Some(plain))
    }
    fn stage(&self, prior: Option<&[u8]>, bytes: &[u8]) -> Result<Vec<u8>> {
        if bytes.is_empty() || bytes.len() > MAX_JAR || bytes.first() == Some(&0xff) {
            return Err(unavailable());
        }
        if bytes.len() <= CHUNK {
            return Ok(bytes.to_vec());
        }
        let bank = prior.and_then(|r| Manifest::parse(r).ok()).map_or(0, |m| 1 - m.bank);
        let mut nonce = [0; 32];
        getrandom::fill(&mut nonce).map_err(|_| unavailable())?;
        let sealed = os::dpapi(true, bytes, &self.entropy(&nonce))?;
        if sealed.is_empty() || sealed.len() > MAX_SEALED {
            return Err(unavailable());
        }
        for (i, chunk) in sealed.chunks(CHUNK).enumerate() {
            os::write_chunk(&self.target(bank, i), chunk)?;
        }
        Ok(Manifest {
            bank,
            len: sealed.len(),
            nonce,
        }
        .encode())
    }
    fn erase(&self) -> Result<()> {
        self.erase_and_sweep(|bank, i| delete(&self.chunk(bank, i)?))
    }
    fn erase_and_sweep(&self, mut delete_chunk: impl FnMut(u8, usize) -> Result<()>) -> Result<()> {
        // A root deletion error is a real sign-out failure. Once it succeeds no fragment can
        // restore this generation; a cleanup error must not appear on the next account. An erase
        // retries every slot, including when the root is already absent. Writes reuse fixed slots.
        delete(&self.root)?;
        for bank in 0..2 {
            for i in 0..SLOTS {
                let _ = delete_chunk(bank, i);
            }
        }
        Ok(())
    }
}

// Save the opaque OS root, not the decoded jar. This also lets a new authenticated sign-in replace
// a roamed/damaged unreadable generation. Staging deliberately does not reclaim the old bank.
struct Change {
    scope: Scope,
    prior: Option<Zeroizing<Vec<u8>>>,
    attempted: Option<Zeroizing<Vec<u8>>>,
}
impl Change {
    fn begin(owner: &NativeCredentials, id: &str) -> Result<Self> {
        let scope = Scope::open(owner, id)?;
        let prior = read_entry(&scope.root)?.map(Zeroizing::new);
        Ok(Self {
            scope,
            prior,
            attempted: None,
        })
    }
    fn prior(&self) -> Option<&[u8]> {
        self.prior.as_ref().map(|r| r.as_slice())
    }
}
impl CredentialWrite for Change {
    fn publish(&mut self, bytes: &[u8]) -> Result<()> {
        let next = Zeroizing::new(self.scope.stage(self.prior(), bytes)?);
        // A released binary does not share the lock. Never knowingly publish against a changed root.
        if read_entry(&self.scope.root)?.as_deref() != self.prior() {
            return Err(unavailable());
        }
        self.attempted = Some(next);
        let next = self.attempted.as_ref().ok_or_else(unavailable)?;
        if self.scope.root.set_secret(next).is_ok() || read_entry(&self.scope.root)?.as_deref() == Some(next.as_slice())
        {
            Ok(())
        } else {
            Err(unavailable())
        }
    }
    fn rollback(self: Box<Self>) -> Result<()> {
        let Some(attempted) = &self.attempted else {
            return Ok(());
        };
        let current = read_entry(&self.scope.root)?;
        if current.as_deref() == self.prior() {
            return Ok(());
        }
        if current.as_deref() != Some(attempted.as_slice()) {
            return Err(unavailable());
        }
        match self.prior() {
            Some(root) => self.scope.root.set_secret(root).map_err(|_| unavailable()),
            None => delete(&self.scope.root),
        }
    }
}
impl Credentials for NativeCredentials {
    fn read(&self, id: &str) -> Result<Option<Vec<u8>>> {
        Scope::open(self, id)?.read()
    }
    fn write(&self, id: &str, bytes: &[u8]) -> Result<()> {
        Change::begin(self, id)?.publish(bytes)
    }
    fn erase(&self, id: &str) -> Result<()> {
        Scope::open(self, id)?.erase()
    }
    fn begin_write(self: Arc<Self>, id: &str) -> Result<Box<dyn CredentialWrite>> {
        Ok(Box::new(Change::begin(&self, id)?))
    }
}
