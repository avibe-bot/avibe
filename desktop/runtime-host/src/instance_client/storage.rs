//! Versioned nonsecret client selection. Credential implementations are native.
use super::protocol::{ClientError, ClientTarget};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, fs, io::Write, path::PathBuf, sync::Arc};

/// Bound per account; older entries fall back to server order.
const RECENT_LIMIT: usize = 64;

/// A fresh read of a present, recognized root. A Windows manifest identifies the
/// generation, not just its decoded bytes. Never serialize or log either.
pub struct CredentialReadback {
    bytes: Vec<u8>,
    prior: bool,
    attempted: bool,
}
fn unavailable() -> ClientError {
    ClientError::new("credentials_unavailable")
}
impl CredentialReadback {
    pub fn from_root(
        root: Vec<u8>,
        prior: Option<&[u8]>,
        attempted: Option<&[u8]>,
        decode: impl FnOnce(Vec<u8>) -> Result<Vec<u8>, ClientError>,
    ) -> Result<Self, ClientError> {
        let prior = prior == Some(root.as_slice());
        let attempted = attempted == Some(root.as_slice());
        if !prior && !attempted {
            return Err(unavailable());
        }
        Ok(Self {
            bytes: decode(root)?,
            prior,
            attempted,
        })
    }
    pub(crate) fn into_prior(self) -> Result<Vec<u8>, ClientError> {
        if self.prior {
            Ok(self.bytes)
        } else {
            Err(unavailable())
        }
    }
    pub fn is_prior(&self, bytes: &[u8]) -> bool {
        self.prior && self.bytes.as_slice() == bytes
    }
    pub fn is_attempted(&self, bytes: &[u8]) -> bool {
        self.attempted && self.bytes.as_slice() == bytes
    }
}

/// Scoped, in-memory undo. Ordinary sign-in can checkpoint and restore an opaque
/// root without decoding it. Only same-key adoption additionally requires observe.
pub trait CredentialWrite: Send {
    /// Read back through this handle, never reopen a platform lock it already owns.
    fn observe(&self) -> Result<CredentialReadback, ClientError>;
    fn publish(&mut self, bytes: &[u8]) -> Result<(), ClientError>;
    fn restore_root(&mut self) -> Result<(), ClientError>;
    fn rollback(mut self: Box<Self>) -> Result<(), ClientError> {
        self.restore_root()
    }
    /// A same-key narrow publication has a readable prior jar. Never restore it over
    /// an absent or unrelated root, or accept the setter's return as proof of undo.
    fn rollback_existing(mut self: Box<Self>, prior: &[u8], attempted: &[u8]) -> Result<(), ClientError> {
        let current = self.observe()?;
        if current.is_prior(prior) {
            return Ok(());
        }
        if !current.is_attempted(attempted) {
            return Err(unavailable());
        }
        let _ = self.restore_root();
        if self.observe()?.is_prior(prior) {
            Ok(())
        } else {
            Err(unavailable())
        }
    }
}

pub trait Credentials: Send + Sync + 'static {
    fn read(&self, account_id: &str) -> Result<Option<Vec<u8>>, ClientError>;
    fn write(&self, account_id: &str, bytes: &[u8]) -> Result<(), ClientError>;
    fn erase(&self, account_id: &str) -> Result<(), ClientError>;
    fn begin_write(self: Arc<Self>, id: &str) -> Result<Box<dyn CredentialWrite>, ClientError> {
        let prior = self.read(id)?;
        Ok(Box::new(OrdinaryWrite {
            store: self,
            id: id.into(),
            prior,
            attempted: None,
        }))
    }
}

struct OrdinaryWrite<T: Credentials + ?Sized> {
    store: Arc<T>,
    id: String,
    prior: Option<Vec<u8>>,
    attempted: Option<Vec<u8>>,
}
impl<T: Credentials + ?Sized> CredentialWrite for OrdinaryWrite<T> {
    fn observe(&self) -> Result<CredentialReadback, ClientError> {
        let root = self.store.read(&self.id)?.ok_or_else(unavailable)?;
        CredentialReadback::from_root(root, self.prior.as_deref(), self.attempted.as_deref(), Ok)
    }
    fn publish(&mut self, bytes: &[u8]) -> Result<(), ClientError> {
        self.attempted = Some(bytes.to_vec());
        self.store.write(&self.id, bytes)
    }
    fn restore_root(&mut self) -> Result<(), ClientError> {
        if self.attempted.is_none() {
            return Ok(());
        }
        match &self.prior {
            Some(bytes) => self.store.write(&self.id, bytes),
            None => self.store.erase(&self.id),
        }
    }
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectionRecord {
    pub schema_version: u8,
    pub profile_id: String,
    pub account_id: Option<String>,
    pub last_committed: ClientTarget,
}
#[derive(Clone)]
pub struct ConnectionStore {
    path: PathBuf,
    writable: bool,
}
impl ConnectionStore {
    pub fn new(path: PathBuf) -> Self {
        Self { path, writable: true }
    }
    /// Preserve damaged/unsupported state while exposing local recovery.
    pub fn read_only(mut self) -> Self {
        self.writable = false;
        self
    }
    pub fn load(&self, local_name: &str) -> Result<ConnectionRecord, ClientError> {
        match fs::read(&self.path) {
            Ok(bytes) => {
                let record: ConnectionRecord =
                    serde_json::from_slice(&bytes).map_err(|_| ClientError::new("connection_record_unavailable"))?;
                if record.schema_version != 1 || uuid::Uuid::parse_str(&record.profile_id).is_err() {
                    return Err(ClientError::new("connection_record_unavailable"));
                }
                Ok(record)
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let record = ConnectionRecord {
                    schema_version: 1,
                    profile_id: uuid::Uuid::new_v4().to_string(),
                    account_id: None,
                    last_committed: ClientTarget::Local {
                        name: local_name.to_owned(),
                        instance_id: None,
                    },
                };
                self.save(&record)?;
                Ok(record)
            }
            Err(_) => Err(ClientError::new("connection_record_unavailable")),
        }
    }
    pub fn save(&self, record: &ConnectionRecord) -> Result<(), ClientError> {
        if !self.writable {
            return Err(ClientError::new("connection_record_unavailable"));
        }
        self.write(record)
            .map_err(|_| ClientError::new("connection_record_unavailable"))
    }
    fn write(&self, record: &ConnectionRecord) -> std::io::Result<()> {
        write_atomic(&self.path, record)
    }

    /// A separate sibling file: the connection record rejects unknown fields,
    /// so recency there would make an older Desktop lose account restore.
    fn recency_path(&self) -> PathBuf {
        self.path.with_file_name("instance-recency.json")
    }
    /// Unusable files read as no recency, so the list keeps server order.
    /// Missing or damaged files are replaced on the next open; a file from a
    /// newer schema, or one that cannot be read, is never overwritten.
    pub fn load_recency(&self) -> InstanceRecency {
        let empty = |writable| InstanceRecency {
            accounts: BTreeMap::new(),
            writable,
        };
        let bytes = match fs::read(self.recency_path()) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return empty(true),
            Err(_) => return empty(false),
        };
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
            return empty(true);
        };
        match value.get("schema_version").and_then(serde_json::Value::as_u64) {
            Some(1) => serde_json::from_value::<RecencyFile>(value).map_or_else(
                |_| empty(true),
                |file| InstanceRecency {
                    accounts: file.accounts,
                    writable: true,
                },
            ),
            Some(_) => empty(false),
            None => empty(true),
        }
    }
    pub fn save_recency(&self, recency: &InstanceRecency) -> Result<(), ClientError> {
        if !self.writable || !recency.writable {
            return Ok(());
        }
        let file = RecencyFile {
            schema_version: 1,
            accounts: recency.accounts.clone(),
        };
        write_atomic(&self.recency_path(), &file).map_err(|_| ClientError::new("connection_record_unavailable"))
    }
}

#[derive(Deserialize, Serialize)]
struct RecencyFile {
    schema_version: u8,
    accounts: BTreeMap<String, Vec<String>>,
}

/// Instances opened as the selected runtime on this device, per account,
/// most recent first. Nonsecret and never sent to a server.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstanceRecency {
    accounts: BTreeMap<String, Vec<String>>,
    writable: bool,
}
impl InstanceRecency {
    pub fn opened(&mut self, account_id: &str, instance_id: &str) {
        let ids = self.accounts.entry(account_id.to_owned()).or_default();
        ids.retain(|id| id != instance_id);
        ids.insert(0, instance_id.to_owned());
        ids.truncate(RECENT_LIMIT);
    }
    pub fn for_account(&self, account_id: &str) -> &[String] {
        self.accounts.get(account_id).map_or(&[], Vec::as_slice)
    }
}

fn write_atomic<T: Serialize>(path: &PathBuf, value: &T) -> std::io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| std::io::Error::other("missing state parent"))?;
    fs::create_dir_all(parent)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer(&mut temporary, value)?;
    temporary.write_all(b"\n")?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    Ok(())
}
