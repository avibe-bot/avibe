//! Native-only management decisions for a bundled local Runtime.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

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

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LocalRuntimePreference {
    pub home: Option<PathBuf>,
    pub independent: bool,
}

/// Saved selection, with a session fallback only when AVIBE_HOME already pins
/// the instance. Broken preferences must never invent an implicit data home.
#[derive(Debug)]
pub(crate) struct LocalRuntimePreferences {
    path: PathBuf,
    explicit_session: Mutex<Option<LocalRuntimePreference>>,
}

impl LocalRuntimePreferences {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            explicit_session: Mutex::new(None),
        }
    }

    pub fn read(&self, explicit_home: bool) -> Result<LocalRuntimePreference, LaunchError> {
        if explicit_home {
            if let Some(current) = self
                .explicit_session
                .lock()
                .map_err(|_| LaunchError::DataHomeRequired)?
                .clone()
            {
                return Ok(current);
            }
            return Ok(LocalRuntimePreference::read(&self.path).unwrap_or_default());
        }
        LocalRuntimePreference::read(&self.path)
    }

    pub fn write(&self, preference: LocalRuntimePreference, explicit_home: bool) -> Result<(), LaunchError> {
        let saved = preference.write(&self.path);
        if explicit_home {
            *self.explicit_session.lock().map_err(|_| LaunchError::RuntimeInstall)? = Some(preference);
            // A caller-supplied home remains authoritative even if this optional
            // Desktop setting cannot be persisted. Its choice lasts this launch.
            Ok(())
        } else {
            saved
        }
    }
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
