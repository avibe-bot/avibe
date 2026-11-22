//! System credentials scoped to this application, profile and account.
//! Only the native Windows CI fixture uses the real adapter, with throwaway targets.
#[cfg(windows)]
mod windows;
use avibe_runtime_host::instance_client::{protocol::ClientError, storage::Credentials};

pub struct NativeCredentials {
    service: String,
}
impl NativeCredentials {
    pub fn new(app_id: &str, profile: &str) -> Self {
        Self {
            service: format!("{app_id}.instance-client.{profile}"),
        }
    }
    #[cfg(any(target_os = "macos", windows))]
    fn entry(&self, id: &str) -> Result<keyring::Entry, ClientError> {
        if id.is_empty() || id.len() > 256 {
            return Err(ClientError::new("credentials_unavailable"));
        }
        keyring::Entry::new(&self.service, id).map_err(|_| ClientError::new("credentials_unavailable"))
    }
}
#[cfg(target_os = "macos")]
impl Credentials for NativeCredentials {
    fn read(&self, id: &str) -> Result<Option<Vec<u8>>, ClientError> {
        match self.entry(id)?.get_secret() {
            Ok(bytes) => Ok(Some(bytes)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err(ClientError::new("credentials_unavailable")),
        }
    }
    fn write(&self, id: &str, bytes: &[u8]) -> Result<(), ClientError> {
        self.entry(id)?
            .set_secret(bytes)
            .map_err(|_| ClientError::new("credentials_unavailable"))
    }
    fn erase(&self, id: &str) -> Result<(), ClientError> {
        match self.entry(id)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err(ClientError::new("credentials_unavailable")),
        }
    }
}
#[cfg(not(any(target_os = "macos", windows)))]
impl Credentials for NativeCredentials {
    fn read(&self, _: &str) -> Result<Option<Vec<u8>>, ClientError> {
        Err(ClientError::new("credentials_unavailable"))
    }
    fn write(&self, _: &str, _: &[u8]) -> Result<(), ClientError> {
        Err(ClientError::new("credentials_unavailable"))
    }
    fn erase(&self, _: &str) -> Result<(), ClientError> {
        Err(ClientError::new("credentials_unavailable"))
    }
}
