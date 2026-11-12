//! Versioned nonsecret client selection. Credential implementations are native.
use super::protocol::{ClientError, ClientTarget};
use serde::{Deserialize, Serialize};
use std::{fs, io::Write, path::PathBuf};

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
        let parent = self
            .path
            .parent()
            .ok_or_else(|| std::io::Error::other("missing state parent"))?;
        fs::create_dir_all(parent)?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
        serde_json::to_writer(&mut temporary, record)?;
        temporary.write_all(b"\n")?;
        temporary.as_file().sync_all()?;
        temporary.persist(&self.path).map_err(|error| error.error)?;
        Ok(())
    }
}
