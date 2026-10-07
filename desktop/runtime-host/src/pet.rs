//! The desktop pet's decisions that need no window: its durable preferences
//! (`pet.json`), the session binding and its revision, summon delivery around
//! page readiness, and where the window goes when the panel opens.
//!
//! The shell (`src-tauri/src/pet.rs`) owns the window and calls in here. See
//! `docs/plans/2026-10-01-desktop-pet.md`.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::deep_link::parse_deep_link;

/// The only `pet.json` shape this build reads or writes.
pub const PREFERENCES_VERSION: u64 = 1;

/// Whether the pet is on when the user never chose. Off while the pet has no
/// voice; the voice build turns it on, and an explicit choice always wins.
pub const DEFAULT_ENABLED: bool = false;

/// `⌃⌥Space` on macOS, `Ctrl+Alt+Space` on Windows.
pub const DEFAULT_SHORTCUT: &str = "Control+Alt+Space";

/// The shortcuts the tray offers. `pet.json` may name any other accelerator.
pub const SHORTCUT_PRESETS: [&str; 3] = [DEFAULT_SHORTCUT, "Control+Shift+Space", "Alt+Shift+Space"];

/// Why the pet was summoned. Only the hotkey asks to listen; every display or
/// navigation path shows.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PetIntent {
    Listen,
    Show,
}

/// The pet image's top-left corner in logical screen coordinates, and the
/// display it was on. The window frame is derived from it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct PetAnchor {
    pub x: f64,
    pub y: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub monitor: Option<String>,
}

/// Everything durable about the pet. `enabled` is written only by the tray
/// switch, so saving an anchor or a binding never freezes the build default.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct PetPreferences {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enabled: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub shortcut: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub anchor: Option<PetAnchor>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding: Option<String>,
}

#[derive(Serialize)]
struct PreferencesFile<'a> {
    version: u64,
    #[serde(flatten)]
    preferences: &'a PetPreferences,
}

#[derive(Debug, PartialEq)]
pub enum PreferencesLoad {
    /// No file: the build defaults apply.
    Absent,
    Loaded(PetPreferences),
    /// Present but unreadable, malformed, or another version's shape.
    Unusable,
}

pub fn load_preferences(path: &Path) -> PreferencesLoad {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return PreferencesLoad::Absent,
        Err(_) => return PreferencesLoad::Unusable,
    };
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return PreferencesLoad::Unusable;
    };
    if value.get("version").and_then(serde_json::Value::as_u64) != Some(PREFERENCES_VERSION) {
        return PreferencesLoad::Unusable;
    }
    match serde_json::from_value::<PetPreferences>(value) {
        Ok(mut preferences) => {
            // A binding is only ever a session id; anything else names nothing.
            preferences.binding = preferences.binding.filter(|binding| valid_session_id(binding));
            PreferencesLoad::Loaded(preferences)
        }
        Err(_) => PreferencesLoad::Unusable,
    }
}

fn write_preferences(path: &Path, preferences: &PetPreferences) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let bytes = serde_json::to_vec_pretty(&PreferencesFile {
        version: PREFERENCES_VERSION,
        preferences,
    })?;
    let temporary = path.with_extension("tmp");
    let result = std::fs::write(&temporary, bytes).and_then(|()| std::fs::rename(&temporary, path));
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

/// A session id the pet may be bound to: exactly what a session deep link accepts.
pub fn valid_session_id(session_id: &str) -> bool {
    parse_deep_link(&format!("avibe://session/{session_id}")).is_some()
}

/// The binding as the shell reports it. `revision` rises by one on every
/// change, so the page can order reports that cross.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct PetBinding {
    pub binding: Option<String>,
    pub revision: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct PendingSummon {
    pub intent: PetIntent,
}

/// The answer to `pet_ready()`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct PetReady {
    #[serde(flatten)]
    pub binding: PetBinding,
    pub summon_pending: Option<PendingSummon>,
}

/// The pet's preferences and in-run state, one per shell.
pub struct PetStore {
    path: PathBuf,
    preferences: PetPreferences,
    /// False after a present file could not be used. The pet stays off and the
    /// file is left as it is, so a newer build's state and an opt-out both
    /// survive, until the tray switch writes a fresh version-1 file.
    trusted: bool,
    revision: u64,
    page_ready: bool,
    pending_summon: Option<PetIntent>,
}

impl PetStore {
    pub fn load(path: PathBuf) -> Self {
        let (preferences, trusted) = match load_preferences(&path) {
            PreferencesLoad::Absent => (PetPreferences::default(), true),
            PreferencesLoad::Loaded(preferences) => (preferences, true),
            PreferencesLoad::Unusable => {
                eprintln!(
                    "The desktop pet is off for this run: its preferences file could not be read. \
                     Turning the pet on from the tray replaces it."
                );
                (PetPreferences::default(), false)
            }
        };
        Self {
            path,
            preferences,
            trusted,
            revision: 0,
            page_ready: false,
            pending_summon: None,
        }
    }

    pub fn enabled(&self) -> bool {
        self.trusted && self.preferences.enabled.unwrap_or(DEFAULT_ENABLED)
    }

    /// The tray switch: the one writer of `enabled`, and the recovery for an
    /// unusable file. A failed write changes nothing.
    pub fn set_enabled(&mut self, enabled: bool) -> std::io::Result<()> {
        let next = PetPreferences {
            enabled: Some(enabled),
            ..self.preferences.clone()
        };
        write_preferences(&self.path, &next)?;
        self.preferences = next;
        self.trusted = true;
        Ok(())
    }

    pub fn shortcut(&self) -> &str {
        self.preferences.shortcut.as_deref().unwrap_or(DEFAULT_SHORTCUT)
    }

    pub fn set_shortcut(&mut self, shortcut: &str) {
        if self.shortcut() == shortcut {
            return;
        }
        self.preferences.shortcut = Some(shortcut.to_owned());
        self.persist();
    }

    pub fn anchor(&self) -> Option<&PetAnchor> {
        self.preferences.anchor.as_ref()
    }

    pub fn set_anchor(&mut self, anchor: PetAnchor) {
        if self.preferences.anchor.as_ref() == Some(&anchor) {
            return;
        }
        self.preferences.anchor = Some(anchor);
        self.persist();
    }

    pub fn binding(&self) -> PetBinding {
        PetBinding {
            binding: self.preferences.binding.clone(),
            revision: self.revision,
        }
    }

    /// Binds `session_id`. The caller has checked it with [`valid_session_id`].
    pub fn bind(&mut self, session_id: &str) -> PetBinding {
        if self.preferences.binding.as_deref() != Some(session_id) {
            self.preferences.binding = Some(session_id.to_owned());
            self.revision += 1;
            self.persist();
        }
        self.binding()
    }

    /// Clears the binding only if it is still `session_id`, so a late result
    /// about an old session can never clear a newer one.
    pub fn unbind(&mut self, session_id: &str) -> PetBinding {
        if self.preferences.binding.as_deref() == Some(session_id) {
            self.preferences.binding = None;
            self.revision += 1;
            self.persist();
        }
        self.binding()
    }

    /// A new document is loading in the pet window: summons wait for its
    /// `pet_ready()`. A summon already waiting is kept for it.
    pub fn page_loading(&mut self) {
        self.page_ready = false;
    }

    /// The window is gone; nothing is waiting for it.
    pub fn window_gone(&mut self) {
        self.page_ready = false;
        self.pending_summon = None;
    }

    pub fn page_is_ready(&self) -> bool {
        self.page_ready
    }

    /// The page has its listeners installed. Returns the binding and consumes
    /// any summon that arrived before.
    pub fn page_ready(&mut self) -> PetReady {
        self.page_ready = true;
        PetReady {
            binding: self.binding(),
            summon_pending: self.pending_summon.take().map(|intent| PendingSummon { intent }),
        }
    }

    /// Returns the intent to deliver now, or keeps it for `pet_ready()`. A
    /// later summon replaces a waiting one.
    pub fn summon(&mut self, intent: PetIntent) -> Option<PetIntent> {
        if self.page_ready {
            return Some(intent);
        }
        self.pending_summon = Some(intent);
        None
    }

    fn persist(&self) {
        if !self.trusted {
            return;
        }
        if write_preferences(&self.path, &self.preferences).is_err() {
            eprintln!("The desktop pet preferences could not be saved; this run keeps them in memory.");
        }
    }
}

/// A rectangle in logical screen coordinates.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LogicalRect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

impl LogicalRect {
    fn right(&self) -> f64 {
        self.x + self.width
    }

    fn bottom(&self) -> f64 {
        self.y + self.height
    }

    fn contains(&self, other: &LogicalRect) -> bool {
        other.x >= self.x && other.y >= self.y && other.right() <= self.right() && other.bottom() <= self.bottom()
    }

    fn overlap(&self, other: &LogicalRect) -> f64 {
        let width = self.right().min(other.right()) - self.x.max(other.x);
        let height = self.bottom().min(other.bottom()) - self.y.max(other.y);
        if width > 0.0 && height > 0.0 {
            width * height
        } else {
            0.0
        }
    }
}

/// A display's work area and the name the shell knows it by, in the space the
/// anchor is stored in. `unit` is how long one of the display's own points is
/// in that space: the pet is `PET_SIZE` points on whichever display shows it,
/// so its extent there is `PET_SIZE * unit`.
#[derive(Clone, Debug, PartialEq)]
pub struct WorkArea {
    pub name: Option<String>,
    pub area: LogicalRect,
    pub unit: f64,
}

impl WorkArea {
    fn pet_extent(&self) -> f64 {
        PET_SIZE * self.unit
    }

    fn pet_at(&self, anchor: (f64, f64)) -> LogicalRect {
        LogicalRect {
            x: anchor.0,
            y: anchor.1,
            width: self.pet_extent(),
            height: self.pet_extent(),
        }
    }
}

// These match the `/pet` route's layout: an 88 px avatar inside `p-2`, a
// 320 px panel, and `gap-2` between them (`ui/src/pet/`).
pub const PET_SIZE: f64 = 88.0;
const PADDING: f64 = 8.0;
const GAP: f64 = 8.0;
const PANEL_WIDTH: f64 = 320.0;
const EXPANDED_HEIGHT: f64 = 480.0;
/// Distance from the work area's corner for a pet with no saved place.
const DEFAULT_MARGIN: f64 = 24.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PanelSide {
    Left,
    Right,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum PanelEdge {
    Top,
    Bottom,
}

/// Where the panel sits relative to the pet, shared by the native frame and
/// the page. `edge: bottom` aligns the panel's bottom with the pet's, so the
/// panel grows upward.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct PetLayout {
    pub panel_side: PanelSide,
    pub panel_edge: PanelEdge,
}

impl Default for PetLayout {
    fn default() -> Self {
        Self {
            panel_side: PanelSide::Left,
            panel_edge: PanelEdge::Bottom,
        }
    }
}

pub fn window_size(expanded: bool) -> (f64, f64) {
    if expanded {
        (PADDING + PANEL_WIDTH + GAP + PET_SIZE + PADDING, EXPANDED_HEIGHT)
    } else {
        (PET_SIZE + 2.0 * PADDING, PET_SIZE + 2.0 * PADDING)
    }
}

/// Where the pet image sits inside the window.
pub fn pet_offset(expanded: bool, layout: PetLayout) -> (f64, f64) {
    if !expanded {
        return (PADDING, PADDING);
    }
    let (width, height) = window_size(true);
    let x = match layout.panel_side {
        PanelSide::Left => width - PADDING - PET_SIZE,
        PanelSide::Right => PADDING,
    };
    let y = match layout.panel_edge {
        PanelEdge::Bottom => height - PADDING - PET_SIZE,
        PanelEdge::Top => PADDING,
    };
    (x, y)
}

pub fn window_origin(anchor: (f64, f64), expanded: bool, layout: PetLayout) -> (f64, f64) {
    let (dx, dy) = pet_offset(expanded, layout);
    (anchor.0 - dx, anchor.1 - dy)
}

pub fn anchor_from_window(origin: (f64, f64), expanded: bool, layout: PetLayout) -> (f64, f64) {
    let (dx, dy) = pet_offset(expanded, layout);
    (origin.0 + dx, origin.1 + dy)
}

/// Moves a point between two views of one physical desktop, each that desktop
/// divided by a scale. The pet window's own scale changes as it crosses
/// displays, so a saved anchor uses one fixed scale that the next restore
/// shares; scale 1 is the physical desktop itself.
pub fn rescale(point: (f64, f64), from_scale: f64, to_scale: f64) -> (f64, f64) {
    let factor = from_scale / to_scale;
    (point.0 * factor, point.1 * factor)
}

/// Opens the panel toward the room it has: left and above by default, flipped
/// near the left or top edge, and toward the larger room when neither fits.
pub fn choose_layout(anchor: (f64, f64), work_area: LogicalRect) -> PetLayout {
    let (width, height) = window_size(true);
    let fits = |start: f64, end: f64, low: f64, high: f64| (start - low, high - end);
    let left = PetLayout {
        panel_side: PanelSide::Left,
        panel_edge: PanelEdge::Bottom,
    };
    let right = PetLayout {
        panel_side: PanelSide::Right,
        panel_edge: PanelEdge::Top,
    };
    let left_x = window_origin(anchor, true, left).0;
    let right_x = window_origin(anchor, true, right).0;
    let (room_left, _) = fits(left_x, left_x + width, work_area.x, work_area.right());
    let (_, room_right) = fits(right_x, right_x + width, work_area.x, work_area.right());
    let panel_side = if room_left >= 0.0 || room_left >= room_right {
        PanelSide::Left
    } else {
        PanelSide::Right
    };
    let above_y = window_origin(anchor, true, left).1;
    let below_y = window_origin(anchor, true, right).1;
    let (room_above, _) = fits(above_y, above_y + height, work_area.y, work_area.bottom());
    let (_, room_below) = fits(below_y, below_y + height, work_area.y, work_area.bottom());
    let panel_edge = if room_above >= 0.0 || room_above >= room_below {
        PanelEdge::Bottom
    } else {
        PanelEdge::Top
    };
    PetLayout { panel_side, panel_edge }
}

/// The place for a pet that has none: the work area's bottom-right corner.
pub fn default_anchor(work_area: &WorkArea) -> PetAnchor {
    let inset = (PET_SIZE + DEFAULT_MARGIN) * work_area.unit;
    PetAnchor {
        x: work_area.area.right() - inset,
        y: work_area.area.bottom() - inset,
        monitor: work_area.name.clone(),
    }
}

/// The display the pet's centre is on, else the first one (the primary).
pub fn area_for(areas: &[WorkArea], anchor: (f64, f64)) -> Option<&WorkArea> {
    areas
        .iter()
        .find(|area| {
            let half = area.pet_extent() / 2.0;
            let center = (anchor.0 + half, anchor.1 + half);
            center.0 >= area.area.x
                && center.0 < area.area.right()
                && center.1 >= area.area.y
                && center.1 < area.area.bottom()
        })
        .or_else(|| areas.first())
}

/// Where a collapsed pet window starts so the pet sits on `anchor`, on a
/// display whose points are `unit` long in the anchor's space.
pub fn restored_window_origin(anchor: (f64, f64), unit: f64) -> (f64, f64) {
    let (dx, dy) = pet_offset(false, PetLayout::default());
    (anchor.0 - dx * unit, anchor.1 - dy * unit)
}

/// Keeps a restored pet whole on a connected display. A pet already inside a
/// work area stays exactly where it was. Otherwise it moves into the area it
/// overlaps most; with no overlap, into its own display if that is still
/// connected, else the first area (the primary display).
pub fn clamp_anchor(anchor: &PetAnchor, work_areas: &[WorkArea]) -> PetAnchor {
    let at = (anchor.x, anchor.y);
    let usable: Vec<&WorkArea> = work_areas
        .iter()
        .filter(|area| area.area.width >= area.pet_extent() && area.area.height >= area.pet_extent())
        .collect();
    if let Some(home) = usable.iter().find(|area| area.area.contains(&area.pet_at(at))) {
        return PetAnchor {
            monitor: home.name.clone().or_else(|| anchor.monitor.clone()),
            ..anchor.clone()
        };
    }
    let overlapping = usable
        .iter()
        .map(|area| (area.area.overlap(&area.pet_at(at)), *area))
        .filter(|(overlap, _)| *overlap > 0.0)
        .max_by(|a, b| a.0.total_cmp(&b.0))
        .map(|(_, area)| area);
    let named = || {
        usable
            .iter()
            .find(|area| area.name.is_some() && area.name == anchor.monitor)
            .copied()
    };
    let Some(target) = overlapping.or_else(named).or_else(|| usable.first().copied()) else {
        return anchor.clone();
    };
    PetAnchor {
        x: anchor.x.clamp(target.area.x, target.area.right() - target.pet_extent()),
        y: anchor
            .y
            .clamp(target.area.y, target.area.bottom() - target.pet_extent()),
        monitor: target.name.clone(),
    }
}

/// How the platform writes `accelerator` in its menus: `⌃⌥Space` on macOS,
/// `Ctrl+Alt+Space` elsewhere.
pub fn shortcut_label(accelerator: &str, macos: bool) -> String {
    let parts: Vec<String> = accelerator
        .split('+')
        .map(|part| {
            let lowered = part.trim().to_ascii_lowercase();
            let (mac, other) = match lowered.as_str() {
                "control" | "ctrl" => ("⌃", "Ctrl"),
                "alt" | "option" => ("⌥", "Alt"),
                "shift" => ("⇧", "Shift"),
                "super" | "command" | "cmd" | "meta" => ("⌘", "Win"),
                "cmdorctrl" | "commandorcontrol" | "cmdorcontrol" | "commandorctrl" => ("⌘", "Ctrl"),
                _ => {
                    let key = part.trim();
                    let key = key
                        .strip_prefix("Key")
                        .or_else(|| key.strip_prefix("Digit"))
                        .unwrap_or(key);
                    return key.to_owned();
                }
            };
            (if macos { mac } else { other }).to_owned()
        })
        .collect();
    parts.join(if macos { "" } else { "+" })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let directory = std::env::temp_dir().join(format!(
                "avibe-pet-preferences-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::SeqCst)
            ));
            std::fs::create_dir(&directory).unwrap();
            Self(directory)
        }

        fn file(&self) -> PathBuf {
            self.0.join("pet.json")
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn area(name: &str, x: f64, y: f64, width: f64, height: f64) -> WorkArea {
        WorkArea {
            name: Some(name.to_owned()),
            area: LogicalRect { x, y, width, height },
            unit: 1.0,
        }
    }

    #[test]
    fn an_absent_file_or_absent_enabled_follows_the_build_default() {
        let directory = TestDirectory::new();
        assert_eq!(load_preferences(&directory.file()), PreferencesLoad::Absent);
        assert_eq!(PetStore::load(directory.file()).enabled(), DEFAULT_ENABLED);
        assert!(!directory.file().exists(), "loading never writes");

        std::fs::write(directory.file(), r#"{"version":1,"binding":"s1"}"#).unwrap();
        let store = PetStore::load(directory.file());
        assert_eq!(store.enabled(), DEFAULT_ENABLED);
        assert_eq!(store.binding().binding.as_deref(), Some("s1"));
    }

    #[test]
    fn an_explicit_choice_survives_the_default_and_other_writes() {
        let directory = TestDirectory::new();
        std::fs::write(directory.file(), r#"{"version":1,"enabled":false}"#).unwrap();
        let mut store = PetStore::load(directory.file());
        assert!(!store.enabled());
        store.bind("s1");
        store.set_anchor(PetAnchor {
            x: 10.0,
            y: 20.0,
            monitor: None,
        });
        assert_eq!(
            load_preferences(&directory.file()),
            PreferencesLoad::Loaded(PetPreferences {
                enabled: Some(false),
                shortcut: None,
                anchor: Some(PetAnchor {
                    x: 10.0,
                    y: 20.0,
                    monitor: None
                }),
                binding: Some("s1".into()),
            })
        );

        // A user who never touched the switch carries no `enabled`, so the
        // build default decides for them.
        let fresh = TestDirectory::new();
        let mut store = PetStore::load(fresh.file());
        store.bind("s2");
        let written = std::fs::read_to_string(fresh.file()).unwrap();
        assert!(!written.contains("enabled"), "{written}");
        assert!(written.contains("\"version\": 1"), "{written}");
    }

    #[test]
    fn an_unreadable_or_unknown_version_file_fails_closed_and_is_left_untouched() {
        for content in [
            "not json",
            r#"{"version":2,"enabled":true}"#,
            r#"{"enabled":true}"#,
            r#"{"version":1,"enabled":"yes"}"#,
        ] {
            let directory = TestDirectory::new();
            std::fs::write(directory.file(), content).unwrap();
            let mut store = PetStore::load(directory.file());
            assert!(!store.enabled(), "{content}");
            store.bind("s1");
            store.set_shortcut("Alt+Shift+Space");
            store.set_anchor(PetAnchor {
                x: 1.0,
                y: 2.0,
                monitor: None,
            });
            assert_eq!(std::fs::read_to_string(directory.file()).unwrap(), content);
            // This run still uses what the user chose.
            assert_eq!(store.binding().binding.as_deref(), Some("s1"));
            assert_eq!(store.shortcut(), "Alt+Shift+Space");

            // The tray switch is the recovery: a fresh version-1 file.
            store.set_enabled(true).unwrap();
            assert!(store.enabled());
            let PreferencesLoad::Loaded(written) = load_preferences(&directory.file()) else {
                panic!("the switch writes a readable file");
            };
            assert_eq!(written.enabled, Some(true));
            assert_eq!(written.binding.as_deref(), Some("s1"));
        }
    }

    #[test]
    fn a_failed_switch_write_changes_nothing() {
        let directory = TestDirectory::new();
        let mut store = PetStore::load(directory.file());
        std::fs::create_dir(directory.file()).unwrap();
        assert!(store.set_enabled(true).is_err());
        assert_eq!(store.enabled(), DEFAULT_ENABLED);
        assert!(!directory.file().with_extension("tmp").exists());
    }

    #[test]
    fn an_invalid_saved_binding_is_dropped() {
        let directory = TestDirectory::new();
        std::fs::write(directory.file(), r#"{"version":1,"binding":"../etc"}"#).unwrap();
        assert_eq!(PetStore::load(directory.file()).binding().binding, None);
        assert!(valid_session_id("abc_123-x.y"));
        for invalid in ["", "..", "a/b", "a?b", &"x".repeat(129)] {
            assert!(!valid_session_id(invalid), "{invalid}");
        }
    }

    #[test]
    fn every_binding_change_raises_the_revision_by_one_and_unbind_compares() {
        let directory = TestDirectory::new();
        let mut store = PetStore::load(directory.file());
        assert_eq!(
            store.binding(),
            PetBinding {
                binding: None,
                revision: 0
            }
        );
        assert_eq!(store.bind("a").revision, 1);
        assert_eq!(store.bind("a").revision, 1, "binding the same session is not a change");
        assert_eq!(
            store.bind("b"),
            PetBinding {
                binding: Some("b".into()),
                revision: 2
            }
        );
        assert_eq!(
            store.unbind("a"),
            PetBinding {
                binding: Some("b".into()),
                revision: 2
            },
            "a late clear about an old session keeps the newer binding"
        );
        assert_eq!(
            store.unbind("b"),
            PetBinding {
                binding: None,
                revision: 3
            }
        );
        assert_eq!(store.page_ready().binding.revision, 3);
        // The binding is the shell's, not the Runtime origin's: it survives a restart.
        store.bind("c");
        let restarted = PetStore::load(directory.file());
        assert_eq!(restarted.binding().binding.as_deref(), Some("c"));
    }

    #[test]
    fn a_summon_before_the_page_is_ready_waits_and_keeps_its_intent() {
        let directory = TestDirectory::new();
        let mut store = PetStore::load(directory.file());
        store.page_loading();
        assert_eq!(store.summon(PetIntent::Show), None);
        assert_eq!(
            store.summon(PetIntent::Listen),
            None,
            "a later summon replaces a waiting one"
        );
        // A reload while it waits keeps it for the next document.
        store.page_loading();
        let ready = store.page_ready();
        assert_eq!(
            ready.summon_pending,
            Some(PendingSummon {
                intent: PetIntent::Listen
            })
        );
        assert_eq!(store.page_ready().summon_pending, None, "delivered once");
        assert_eq!(
            store.summon(PetIntent::Show),
            Some(PetIntent::Show),
            "a ready page gets it at once"
        );
        assert_eq!(store.page_ready().summon_pending, None);

        store.page_loading();
        store.summon(PetIntent::Show);
        store.window_gone();
        assert_eq!(
            store.page_ready().summon_pending,
            None,
            "a destroyed window takes nothing with it"
        );
    }

    #[test]
    fn pet_ready_serializes_to_the_page_contract() {
        let ready = PetReady {
            binding: PetBinding {
                binding: Some("s".into()),
                revision: 4,
            },
            summon_pending: Some(PendingSummon {
                intent: PetIntent::Listen,
            }),
        };
        assert_eq!(
            serde_json::to_value(ready).unwrap(),
            serde_json::json!({"binding": "s", "revision": 4, "summon_pending": {"intent": "listen"}})
        );
        assert_eq!(
            serde_json::to_value(PetLayout::default()).unwrap(),
            serde_json::json!({"panel_side": "left", "panel_edge": "bottom"})
        );
    }

    #[test]
    fn the_anchor_is_the_pet_image_whichever_way_the_panel_opened() {
        let anchor = (1000.0, 700.0);
        for expanded in [false, true] {
            for panel_side in [PanelSide::Left, PanelSide::Right] {
                for panel_edge in [PanelEdge::Top, PanelEdge::Bottom] {
                    let layout = PetLayout { panel_side, panel_edge };
                    let origin = window_origin(anchor, expanded, layout);
                    assert_eq!(anchor_from_window(origin, expanded, layout), anchor);
                    let (width, height) = window_size(expanded);
                    let (dx, dy) = pet_offset(expanded, layout);
                    assert!(dx >= 0.0 && dx + PET_SIZE <= width && dy >= 0.0 && dy + PET_SIZE <= height);
                }
            }
        }
        // The panel opening to the left moves the window's origin, never the pet.
        let left = window_origin(anchor, true, PetLayout::default());
        assert!(left.0 < anchor.0 - 300.0);
    }

    #[test]
    fn the_panel_flips_toward_the_room_near_each_screen_edge() {
        let screen = LogicalRect {
            x: 0.0,
            y: 25.0,
            width: 1440.0,
            height: 875.0,
        };
        let cases = [
            ((1300.0, 780.0), PanelSide::Left, PanelEdge::Bottom),
            ((20.0, 780.0), PanelSide::Right, PanelEdge::Bottom),
            ((1300.0, 40.0), PanelSide::Left, PanelEdge::Top),
            ((20.0, 40.0), PanelSide::Right, PanelEdge::Top),
            ((700.0, 450.0), PanelSide::Left, PanelEdge::Bottom),
        ];
        for (anchor, side, edge) in cases {
            let layout = choose_layout(anchor, screen);
            assert_eq!((layout.panel_side, layout.panel_edge), (side, edge), "{anchor:?}");
            let origin = window_origin(anchor, true, layout);
            let (width, height) = window_size(true);
            assert!(origin.0 >= screen.x && origin.0 + width <= screen.right(), "{anchor:?}");
            assert!(
                origin.1 >= screen.y && origin.1 + height <= screen.bottom(),
                "{anchor:?}"
            );
        }
        // A screen too small for the panel either way opens toward the larger room.
        let tiny = LogicalRect {
            x: 0.0,
            y: 0.0,
            width: 400.0,
            height: 400.0,
        };
        assert_eq!(choose_layout((300.0, 300.0), tiny).panel_side, PanelSide::Left);
        assert_eq!(choose_layout((10.0, 10.0), tiny).panel_side, PanelSide::Right);
    }

    #[test]
    fn a_restore_keeps_a_visible_pet_and_clamps_one_whose_display_is_gone() {
        let areas = [
            area("Built-in", 0.0, 25.0, 1440.0, 875.0),
            area("External", 1440.0, 0.0, 1920.0, 1055.0),
        ];
        let on_external = PetAnchor {
            x: 3000.0,
            y: 900.0,
            monitor: Some("External".into()),
        };
        assert_eq!(clamp_anchor(&on_external, &areas), on_external);

        // The external display is unplugged: the pet lands whole on the built-in one.
        let clamped = clamp_anchor(&on_external, &areas[..1]);
        assert_eq!((clamped.x, clamped.y), (1440.0 - PET_SIZE, 900.0 - PET_SIZE));
        assert_eq!(clamped.monitor.as_deref(), Some("Built-in"));

        // Half off an edge: pulled in on the display it overlaps.
        let straddling = PetAnchor {
            x: -40.0,
            y: 400.0,
            monitor: None,
        };
        let pulled = clamp_anchor(&straddling, &areas);
        assert_eq!((pulled.x, pulled.y), (0.0, 400.0));

        // With no overlap, its own display wins over the primary one.
        let lost = PetAnchor {
            x: 9000.0,
            y: 9000.0,
            monitor: Some("External".into()),
        };
        let home = clamp_anchor(&lost, &areas);
        assert_eq!(home.monitor.as_deref(), Some("External"));
        assert_eq!((home.x, home.y), (1440.0 + 1920.0 - PET_SIZE, 1055.0 - PET_SIZE));

        assert_eq!(clamp_anchor(&lost, &[]), lost, "no display information moves nothing");

        let corner = default_anchor(&areas[0]);
        assert_eq!(clamp_anchor(&corner, &areas), corner);
    }

    #[test]
    fn shortcuts_are_labelled_the_way_each_platform_writes_them() {
        assert_eq!(shortcut_label(DEFAULT_SHORTCUT, true), "⌃⌥Space");
        assert_eq!(shortcut_label(DEFAULT_SHORTCUT, false), "Ctrl+Alt+Space");
        assert_eq!(shortcut_label("Alt+Shift+Space", true), "⌥⇧Space");
        assert_eq!(shortcut_label("Super+Shift+KeyP", false), "Win+Shift+P");
        assert_eq!(SHORTCUT_PRESETS[0], DEFAULT_SHORTCUT);
    }

    #[test]
    fn a_drag_on_a_scaled_secondary_display_restores_to_the_same_physical_spot() {
        // One physical desktop: a 125% primary and a 150% display to its right.
        // The pet sits at physical (3000, 200) on the second one.
        let (primary, window) = (1.25, 1.5);
        let in_window_points = rescale((3000.0, 200.0), 1.0, window);
        let saved = rescale(in_window_points, window, primary);
        let areas = [
            area("primary", 0.0, 0.0, 2560.0 / primary, 1440.0 / primary),
            WorkArea {
                unit: window / primary,
                ..area("side", 2560.0 / primary, 0.0, 3840.0 / primary, 2160.0 / primary)
            },
        ];
        let restored = clamp_anchor(
            &PetAnchor {
                x: saved.0,
                y: saved.1,
                monitor: Some("side".to_owned()),
            },
            &areas,
        );
        let physical = rescale((restored.x, restored.y), primary, 1.0);
        assert!((physical.0 - 3000.0).abs() < 1e-9 && (physical.1 - 200.0).abs() < 1e-9);
        assert_eq!(restored.monitor.as_deref(), Some("side"));
        assert_eq!(
            area_for(&areas, saved).and_then(|area| area.name.as_deref()),
            Some("side")
        );
        // The window starts the pet's 8-point padding, at the side display's
        // scale, above and left of it.
        let window_at = rescale(restored_window_origin(saved, window / primary), primary, 1.0);
        assert!((window_at.0 - (3000.0 - 12.0)).abs() < 1e-9 && (window_at.1 - (200.0 - 12.0)).abs() < 1e-9);
    }

    #[test]
    fn a_pet_clamped_onto_a_denser_display_stays_whole_in_its_own_points() {
        // A 100% primary and a 150% display to its right, in primary points.
        // There the 88-point pet is 132 units wide.
        let side = WorkArea {
            unit: 1.5,
            ..area("side", 1920.0, 0.0, 2560.0, 1440.0)
        };
        let areas = [area("primary", 0.0, 0.0, 1920.0, 1080.0), side.clone()];
        let edge = (side.area.right() - 132.0, side.area.bottom() - 132.0);
        // Overlapping the right edge: pulled in by its scaled extent, not 88.
        let past_edge = PetAnchor {
            x: side.area.right() - 100.0,
            y: 300.0,
            monitor: Some("side".to_owned()),
        };
        let pulled = clamp_anchor(&past_edge, &areas);
        assert_eq!((pulled.x, pulled.y), (edge.0, 300.0));
        // Inside by 88 but not by 132 is not whole.
        let almost = PetAnchor {
            x: side.area.right() - 100.0,
            y: side.area.bottom() - 100.0,
            monitor: Some("side".to_owned()),
        };
        assert_eq!(clamp_anchor(&almost, &areas).x, edge.0);
        // The display is gone from its saved place: it lands whole inside.
        let lost = PetAnchor {
            x: 9000.0,
            y: 9000.0,
            monitor: Some("side".to_owned()),
        };
        let home = clamp_anchor(&lost, &areas);
        assert_eq!((home.x, home.y), edge);
        // A pet with no place keeps the margin in the display's own points.
        let corner = default_anchor(&side);
        assert_eq!(
            (corner.x, corner.y),
            (side.area.right() - 168.0, side.area.bottom() - 168.0)
        );
        assert_eq!(clamp_anchor(&corner, &areas), corner);
    }
}
