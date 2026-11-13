//! Versioned nonsecret client selection. Credential implementations are native.
use super::protocol::{ClientError, ClientTarget};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, fs, io::Write, path::PathBuf};

/// Bound per account; older entries fall back to server order.
const RECENT_LIMIT: usize = 64;

pub trait Credentials: Send + Sync {
    fn read(&self, account_id: &str) -> Result<Option<Vec<u8>>, ClientError>;
    fn write(&self, account_id: &str, bytes: &[u8]) -> Result<(), ClientError>;
    fn erase(&self, account_id: &str) -> Result<(), ClientError>;
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
