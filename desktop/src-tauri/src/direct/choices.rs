//! This computer's choice of path to one instance, per account and instance
//! (the instance's "Devices › Connection" page, "this device"): automatic
//! (the direct path when it can, otherwise the Cloudflare tunnel), or the
//! Cloudflare tunnel only. Kept on this computer only, as the iPhone App
//! keeps its own (`ios/Avibe/DirectPathPreference.swift`): by account ID and
//! instance origin, automatic entries not kept.
//!
//! The same file remembers which instances have answered a rendezvous for
//! this account before. That fact is an account ID and an origin: no secret,
//! no address, no endpoint ID, no installation ID.
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::PathBuf,
};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    #[default]
    Automatic,
    Tunnel,
}

/// One kept choice. `reverted` is the iPhone App's record of having set the
/// choice back to automatic itself; the Desktop App never does (it cannot
/// reach on a direct path an instance it cannot reach on the tunnel), so it
/// is always absent here. It is kept in the shape so both read alike.
#[derive(Serialize, Deserialize)]
struct Choice {
    mode: Mode,
    #[serde(default)]
    reverted: Option<serde_json::Value>,
}

#[derive(Default, Serialize, Deserialize)]
struct Stored {
    version: u8,
    /// An entry this build cannot read (a later mode) reads as automatic.
    #[serde(default)]
    choices: BTreeMap<String, serde_json::Value>,
    #[serde(default)]
    reached: BTreeSet<String>,
}

/// More instances than one account opens from one computer.
const MAX_REACHED: usize = 256;

pub struct Choices {
    path: Option<PathBuf>,
    stored: Stored,
}

fn entry(account: &str, origin: &str) -> String {
    format!("{account} {origin}")
}

impl Choices {
    /// Reads `path`. A file that is missing, unreadable or of another version
    /// is an empty one: every choice automatic, nothing remembered.
    pub fn load(path: PathBuf) -> Self {
        let stored = std::fs::read(&path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Stored>(&bytes).ok())
            .filter(|stored| stored.version == 1)
            .unwrap_or_default();
        Self {
            path: Some(path),
            stored,
        }
    }

    /// Choices for this run only, when the app has nowhere to keep them.
    pub fn unsaved() -> Self {
        Self {
            path: None,
            stored: Stored::default(),
        }
    }

    pub fn mode(&self, account: &str, origin: &str) -> Mode {
        self.stored
            .choices
            .get(&entry(account, origin))
            .and_then(|value| serde_json::from_value::<Choice>(value.clone()).ok())
            .map_or(Mode::Automatic, |choice| choice.mode)
    }

    /// Keeps the choice. It applies for this run even when it cannot be saved.
    pub fn set(&mut self, account: &str, origin: &str, mode: Mode) {
        let key = entry(account, origin);
        match mode {
            Mode::Automatic => {
                self.stored.choices.remove(&key);
            }
            Mode::Tunnel => {
                let choice = Choice { mode, reverted: None };
                self.stored
                    .choices
                    .insert(key, serde_json::to_value(choice).expect("choice JSON"));
            }
        }
        self.save();
    }

    /// Whether this account's instance at `origin` has answered a rendezvous
    /// in an earlier session.
    pub fn reached(&self, account: &str, origin: &str) -> bool {
        self.stored.reached.contains(&entry(account, origin))
    }

    pub fn remember(&mut self, account: &str, origin: &str) {
        let key = entry(account, origin);
        if self.stored.reached.contains(&key) {
            return;
        }
        while self.stored.reached.len() >= MAX_REACHED {
            self.stored.reached.pop_first();
        }
        self.stored.reached.insert(key);
        self.save();
    }

    fn save(&mut self) {
        let Some(path) = &self.path else { return };
        self.stored.version = 1;
        let Ok(bytes) = serde_json::to_vec(&self.stored) else {
            return;
        };
        // Written beside the file and renamed over it, so a reader never
        // sees half of one.
        let Some(directory) = path.parent() else { return };
        let written = std::fs::create_dir_all(directory)
            .and_then(|_| tempfile::NamedTempFile::new_in(directory))
            .and_then(|mut file| {
                std::io::Write::write_all(&mut file, &bytes)?;
                file.persist(path).map(|_| ()).map_err(|error| error.error)
            });
        if written.is_err() {
            eprintln!("the desktop shell could not save this computer's connection choices");
        }
    }
}
