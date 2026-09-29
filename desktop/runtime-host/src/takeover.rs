//! Native-only management decisions for a bundled local Runtime.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::LaunchError;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub created: f64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ExternalRuntime {
    pub home: String,
    pub service: Option<ProcessIdentity>,
    pub ui: Option<ProcessIdentity>,
    pub reason: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Discovery {
    pub home: PathBuf,
    pub origin: String,
    pub external: Option<ExternalRuntime>,
}

#[derive(Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LocalRuntimePreference {
    pub home: Option<PathBuf>,
    pub independent: bool,
}

impl LocalRuntimePreference {
    pub fn read(path: &Path) -> Result<Self, LaunchError> {
        match std::fs::read(path) {
            Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| LaunchError::DataHomeRequired),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(_) => Err(LaunchError::DataHomeRequired),
        }
    }

    pub fn write(&self, path: &Path) -> Result<(), LaunchError> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|_| LaunchError::RuntimeInstall)?;
        }
        // A partial write is rejected on the next read and asks for a home;
        // it can never silently switch to a new empty default instance.
        std::fs::write(path, serde_json::to_vec(self).map_err(|_| LaunchError::RuntimeInstall)?)
            .map_err(|_| LaunchError::RuntimeInstall)
    }
}
