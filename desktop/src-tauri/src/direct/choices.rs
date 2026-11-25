//! This computer's choice of path to one instance, per account and instance
//! (the instance's "Devices › Connection" page, "this device"): automatic
//! (the direct path when it can, otherwise the Cloudflare tunnel), the
//! Cloudflare tunnel only, or the direct path and the relay only. Kept on
//! this computer only, as the iPhone App keeps its own
//! (`ios/Avibe/DirectPathPreference.swift`): by account ID and instance
//! origin. An automatic entry is kept only while it says that the app set
//! the choice back itself.
//!
//! `Mode::Direct` is the choice, not a path: "the direct path and the relay
//! only". What a window's connections use right now is the core's
//! `PreferredPath`, which a page reads as `direct.status().preferred`
//! (`tunnel` or `direct`). The two share a word and nothing else.
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
    /// The direct path and the relay only: never the tunnel once a direct
    /// link is up (`wiring.rs` has the rules).
    Direct,
}

/// The app set the choice back to automatic itself. The Desktop App does so
/// only from `Mode::Direct`, when the instance could not be reached on a
/// direct path or the relay: it never sets "Cloudflare tunnel only" back,
/// because it cannot reach on a direct path an instance it cannot reach on
/// the tunnel.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Reverted {
    /// Milliseconds since the Unix epoch, as the iPhone App writes it.
    pub at: u64,
    pub reason: Reason,
    /// The choice it was set back from.
    pub from: Mode,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Reason {
    Unreachable,
}

/// One kept choice.
#[derive(Serialize, Deserialize)]
struct Choice {
    mode: Mode,
    #[serde(default)]
    reverted: Option<Reverted>,
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

    /// An entry this build cannot read (a later mode, or a record of a
    /// reversion it does not know) is no entry: automatic, set back by
    /// nobody.
    fn choice(&self, account: &str, origin: &str) -> Option<Choice> {
        self.stored
            .choices
            .get(&entry(account, origin))
            .and_then(|value| serde_json::from_value::<Choice>(value.clone()).ok())
    }

    pub fn mode(&self, account: &str, origin: &str) -> Mode {
        self.choice(account, origin)
            .map_or(Mode::Automatic, |choice| choice.mode)
    }

    /// The app's own setting-back of this choice, while it stands: until the
    /// next choice made for the instance.
    pub fn reverted(&self, account: &str, origin: &str) -> Option<Reverted> {
        self.choice(account, origin)
            .filter(|choice| choice.mode == Mode::Automatic)
            .and_then(|choice| choice.reverted)
    }

    /// Keeps the choice someone made. It applies for this run even when it
    /// cannot be saved. Whatever the app had set back is forgotten.
    pub fn set(&mut self, account: &str, origin: &str, mode: Mode) {
        self.keep(account, origin, mode, None);
    }

    /// The app sets the choice back to automatic itself, and keeps why.
    pub fn revert(&mut self, account: &str, origin: &str, reverted: Reverted) {
        self.keep(account, origin, Mode::Automatic, Some(reverted));
    }

    fn keep(&mut self, account: &str, origin: &str, mode: Mode, reverted: Option<Reverted>) {
        let key = entry(account, origin);
        if mode == Mode::Automatic && reverted.is_none() {
            self.stored.choices.remove(&key);
        } else {
            let choice = Choice { mode, reverted };
            self.stored
                .choices
                .insert(key, serde_json::to_value(choice).expect("choice JSON"));
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
