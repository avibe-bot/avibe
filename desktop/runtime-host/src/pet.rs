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
    Release,
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
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub dismissed_ids: Vec<String>,
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
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub dismissed_ids: Vec<String>,
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
            self.preferences.dismissed_ids.clear();
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
            self.preferences.dismissed_ids.clear();
            self.revision += 1;
            self.persist();
        }
        self.binding()
    }

    pub fn set_dismissed_ids(&mut self, session_id: &str, ids: Vec<String>) -> std::io::Result<()> {
        if self.preferences.binding.as_deref() != Some(session_id) {
            return Ok(());
        }
        if self.preferences.dismissed_ids == ids {
            return Ok(());
        }
        let next = PetPreferences {
            dismissed_ids: ids,
            ..self.preferences.clone()
        };
        write_preferences(&self.path, &next)?;
        self.preferences = next;
        Ok(())
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
            dismissed_ids: self.preferences.dismissed_ids.clone(),
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

    /// A summon while no window can exist yet, because the Workbench has no
    /// Runtime. A hotkey becomes a released hold, opening text when ready;
    /// a show action still opens the stack. Startup never starts a stale mic.
    pub fn summon_before_window(&mut self, intent: PetIntent) {
        self.page_ready = false;
        self.pending_summon = Some(if intent == PetIntent::Listen {
            PetIntent::Release
        } else {
            PetIntent::Show
        });
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
            x: restored_window_origin(anchor, self.unit).0,
            y: restored_window_origin(anchor, self.unit).1,
            width: WINDOW_WIDTH * self.unit,
            height: WINDOW_HEIGHT * self.unit,
        }
    }
}

// Shared with pet-mini.css: image top-left (172.5, 449), dock at y=552.
pub const PET_SIZE: f64 = 115.0;
pub const WINDOW_WIDTH: f64 = 460.0;
pub const WINDOW_HEIGHT: f64 = 600.0;
const PET_BOTTOM: f64 = 36.0;
// pet-mini.css: the dock starts 103px below the image top and is 40px high.
const PET_AND_DOCK_HEIGHT: f64 = 143.0;
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

pub fn window_size(_expanded: bool) -> (f64, f64) {
    (WINDOW_WIDTH, WINDOW_HEIGHT)
}

/// The image stays at the same position in every interaction state.
pub fn pet_offset(_expanded: bool, _layout: PetLayout) -> (f64, f64) {
    ((WINDOW_WIDTH - PET_SIZE) / 2.0, WINDOW_HEIGHT - PET_BOTTOM - PET_SIZE)
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

/// The mini has one bottom-anchored column; it never flips or moves on expand.
pub fn choose_layout(_anchor: (f64, f64), _work_area: LogicalRect) -> PetLayout {
    PetLayout::default()
}

pub fn default_anchor(work_area: &WorkArea) -> PetAnchor {
    let (dx, dy) = pet_offset(false, PetLayout::default());
    PetAnchor {
        x: work_area.area.right() - (WINDOW_WIDTH + DEFAULT_MARGIN) * work_area.unit + dx * work_area.unit,
        y: work_area.area.bottom() - (WINDOW_HEIGHT + DEFAULT_MARGIN) * work_area.unit + dy * work_area.unit,
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
/// connected, else the first area (the primary display). Use the fixed window
/// on a display that fits it, otherwise preserve access to the image and dock.
pub fn clamp_anchor(anchor: &PetAnchor, work_areas: &[WorkArea]) -> PetAnchor {
    let at = (anchor.x, anchor.y);
    let image = |area: &WorkArea| LogicalRect {
        x: anchor.x,
        y: anchor.y,
        width: PET_SIZE * area.unit,
        height: PET_SIZE * area.unit,
    };
    let footprint = |area: &WorkArea| {
        if area.area.width >= WINDOW_WIDTH * area.unit && area.area.height >= WINDOW_HEIGHT * area.unit {
            area.pet_at(at)
        } else {
            LogicalRect {
                height: PET_AND_DOCK_HEIGHT * area.unit,
                ..image(area)
            }
        }
    };
    if let Some(home) = work_areas.iter().find(|area| area.area.contains(&footprint(area))) {
        return PetAnchor {
            monitor: home.name.clone().or_else(|| anchor.monitor.clone()),
            ..anchor.clone()
        };
    }
    let overlapping = work_areas
        .iter()
        .map(|area| (area.area.overlap(&image(area)), area))
        .filter(|(overlap, _)| *overlap > 0.0)
        .max_by(|a, b| a.0.total_cmp(&b.0))
        .map(|(_, area)| area);
    let named = || {
        work_areas
            .iter()
            .find(|area| area.name.is_some() && area.name == anchor.monitor)
    };
    let Some(target) = overlapping.or_else(named).or_else(|| work_areas.first()) else {
        return anchor.clone();
    };
    let bounds = footprint(target);
    let left = target.area.x + anchor.x - bounds.x;
    let top = target.area.y + anchor.y - bounds.y;
    PetAnchor {
        x: anchor
            .x
            .clamp(left, (left + target.area.width - bounds.width).max(left)),
        y: anchor.y.clamp(top, (top + target.area.height - bounds.height).max(top)),
        monitor: target.name.clone(),
    }
}

/// Window-local logical coordinates; shadows deliberately do not enlarge these.
#[derive(Clone, Copy, Debug, Deserialize)]
pub struct HitRect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
    /// Part of the page's hover zone (Vibey and its dock). Absent from older
    /// pages, which then get no hover events.
    #[serde(default)]
    pub hover: bool,
}

/// Whether the cursor is over the page's hover zone. The shell reports this to
/// the page because a webview in a window that is not key (the pet never takes
/// focus on its own) receives no hover on macOS.
pub fn hovering(rects: &[HitRect], x: f64, y: f64) -> bool {
    rects.iter().any(|rect| rect.hover && rect.contains(x, y))
}
impl HitRect {
    pub fn valid(&self) -> bool {
        [self.x, self.y, self.width, self.height].iter().all(|v| v.is_finite())
            && self.width >= 0.0
            && self.height >= 0.0
    }
    pub fn contains(&self, x: f64, y: f64) -> bool {
        x >= self.x && x <= self.x + self.width && y >= self.y && y <= self.y + self.height
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
    fn dismissed_cards_survive_restart_and_stay_scoped_to_the_binding() {
        let directory = TestDirectory::new();
        // Released files without the optional field retain their binding.
        std::fs::write(directory.file(), r#"{"version":1,"enabled":true,"binding":"s1"}"#).unwrap();
        let mut store = PetStore::load(directory.file());
        assert!(store.page_ready().dismissed_ids.is_empty());
        store
            .set_dismissed_ids("s1", vec!["watch:w1".into(), "task:t1".into()])
            .unwrap();
        let mut restored = PetStore::load(directory.file());
        assert_eq!(restored.page_ready().dismissed_ids, vec!["watch:w1", "task:t1"]);
        restored.bind("s2");
        restored.set_dismissed_ids("s1", vec!["stale".into()]).unwrap();
        let mut next = PetStore::load(directory.file());
        assert_eq!(next.binding().binding.as_deref(), Some("s2"));
        assert!(next.page_ready().dismissed_ids.is_empty());
    }

    #[test]
    fn hit_regions_accept_only_finite_nonnegative_extents() {
        let rect = HitRect {
            x: 10.0,
            y: 20.0,
            width: 30.0,
            height: 40.0,
            hover: false,
        };
        assert!(rect.valid());
        assert!(rect.contains(10.0, 20.0));
        assert!(!rect.contains(9.9, 20.0));
        assert!(!rect.contains(40.1, 20.0));
        for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert!(!HitRect { x: invalid, ..rect }.valid());
            assert!(!HitRect { y: invalid, ..rect }.valid());
            assert!(!HitRect { width: invalid, ..rect }.valid());
            assert!(!HitRect {
                height: invalid,
                ..rect
            }
            .valid());
        }
        assert!(!HitRect { width: -1.0, ..rect }.valid());
        assert!(!HitRect { height: -1.0, ..rect }.valid());
    }

    #[test]
    fn only_hover_rects_make_the_cursor_hover_and_older_pages_omit_the_flag() {
        let rects: Vec<HitRect> = serde_json::from_str(
            r#"[{"x":0,"y":0,"width":100,"height":50},{"x":0,"y":60,"width":100,"height":50,"hover":true}]"#,
        )
        .unwrap();
        assert!(!rects[0].hover);
        assert!(!hovering(&rects, 50.0, 25.0));
        assert!(hovering(&rects, 50.0, 80.0));
        assert!(!hovering(&rects, 50.0, 55.0));
        assert!(!hovering(&[], 50.0, 80.0));
    }

    #[test]
    fn releasing_a_shortcut_during_page_load_never_replays_a_held_microphone() {
        let directory = TestDirectory::new();
        let mut store = PetStore::load(directory.file());
        assert_eq!(store.summon(PetIntent::Listen), None);
        assert_eq!(store.summon(PetIntent::Release), None);
        assert_eq!(
            store.page_ready().summon_pending,
            Some(PendingSummon {
                intent: PetIntent::Release
            })
        );
        assert_eq!(store.page_ready().summon_pending, None);
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
                dismissed_ids: Vec::new(),
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
            dismissed_ids: Vec::new(),
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
        // Expansion changes no geometry in the fixed mini window.
        assert_eq!(
            window_origin(anchor, false, PetLayout::default()),
            window_origin(anchor, true, PetLayout::default())
        );
    }

    #[test]
    fn the_fixed_window_stays_whole_and_layout_never_flips() {
        let screen = LogicalRect {
            x: 0.0,
            y: 25.0,
            width: 1440.0,
            height: 875.0,
        };
        for anchor in [(1300.0, 780.0), (20.0, 40.0), (700.0, 450.0)] {
            let layout = choose_layout(anchor, screen);
            assert_eq!(layout, PetLayout::default());
        }
        let area = WorkArea {
            name: Some("primary".into()),
            area: screen,
            unit: 1.0,
        };
        let anchor = default_anchor(&area);
        let origin = window_origin((anchor.x, anchor.y), false, PetLayout::default());
        assert_eq!(
            origin,
            (
                screen.right() - WINDOW_WIDTH - DEFAULT_MARGIN,
                screen.bottom() - WINDOW_HEIGHT - DEFAULT_MARGIN
            )
        );
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
        assert_eq!((clamped.x, clamped.y), (1152.5, 749.0));
        assert_eq!(clamped.monitor.as_deref(), Some("Built-in"));

        // Half off an edge: pulled in on the display it overlaps.
        let straddling = PetAnchor {
            x: -40.0,
            y: 400.0,
            monitor: None,
        };
        let pulled = clamp_anchor(&straddling, &areas);
        assert_eq!((pulled.x, pulled.y), (172.5, 474.0));

        // With no overlap, its own display wins over the primary one.
        let lost = PetAnchor {
            x: 9000.0,
            y: 9000.0,
            monitor: Some("External".into()),
        };
        let home = clamp_anchor(&lost, &areas);
        assert_eq!(home.monitor.as_deref(), Some("External"));
        assert_eq!((home.x, home.y), (3072.5, 904.0));

        assert_eq!(clamp_anchor(&lost, &[]), lost, "no display information moves nothing");

        let corner = default_anchor(&areas[0]);
        assert_eq!(clamp_anchor(&corner, &areas), corner);
    }

    #[test]
    fn short_display_fallback_selects_by_image_overlap_then_saved_display_then_primary() {
        let areas = [
            area("primary", 0.0, 0.0, 800.0, 550.0),
            area("side", 800.0, 0.0, 800.0, 550.0),
        ];
        for (x, saved, expected) in [
            (750.0, "primary", "side"), // Image overlaps side most; transparent frame overlaps primary most.
            (9000.0, "side", "side"),
            (9000.0, "detached", "primary"),
        ] {
            let restored = clamp_anchor(
                &PetAnchor {
                    x,
                    y: 500.0,
                    monitor: Some(saved.into()),
                },
                &areas,
            );
            assert_eq!(restored.monitor.as_deref(), Some(expected));
            let target = areas
                .iter()
                .find(|area| area.name.as_deref() == Some(expected))
                .unwrap();
            assert!(target.area.contains(&LogicalRect {
                x: restored.x,
                y: restored.y,
                width: PET_SIZE,
                height: PET_AND_DOCK_HEIGHT,
            }));
            assert_eq!(restored.y, 407.0, "the dock, not only the image, must fit");
        }
    }

    #[test]
    fn a_small_laptop_keeps_its_pet_when_a_full_frame_external_display_is_connected() {
        let areas = [
            area("short", 0.0, 0.0, 800.0, 550.0),
            area("full", 800.0, -100.0, 1000.0, 900.0),
        ];
        let on_laptop = PetAnchor {
            x: 100.0,
            y: 100.0,
            monitor: Some("short".into()),
        };
        assert_eq!(clamp_anchor(&on_laptop, &areas), on_laptop);
        let low_laptop = clamp_anchor(&PetAnchor { y: 500.0, ..on_laptop }, &areas);
        assert_eq!(low_laptop.monitor.as_deref(), Some("short"));
        assert_eq!((low_laptop.x, low_laptop.y), (100.0, 407.0));
        assert!(areas[0].area.contains(&LogicalRect {
            x: low_laptop.x,
            y: low_laptop.y,
            width: PET_SIZE,
            height: PET_AND_DOCK_HEIGHT,
        }));
        let external = clamp_anchor(
            &PetAnchor {
                x: 900.0,
                y: 0.0,
                monitor: Some("full".into()),
            },
            &areas,
        );
        assert_eq!(external.monitor.as_deref(), Some("full"));
        assert!(areas[1].area.contains(&areas[1].pet_at((external.x, external.y))));
    }

    #[test]
    fn a_hotkey_before_any_runtime_opens_text_without_replaying_capture() {
        let directory = TestDirectory::new();
        let mut store = PetStore::load(directory.file());
        // The hotkey during Runtime startup: no window can exist yet.
        store.summon_before_window(PetIntent::Listen);
        // The Runtime becomes ready and the window's page loads.
        store.page_loading();
        assert_eq!(
            store.page_ready().summon_pending,
            Some(PendingSummon {
                intent: PetIntent::Release
            })
        );
        // Delivered once.
        store.page_loading();
        assert_eq!(store.page_ready().summon_pending, None);
        // A Runtime that goes away before any window takes the wait with it.
        store.summon_before_window(PetIntent::Listen);
        store.window_gone();
        assert_eq!(store.page_ready().summon_pending, None);
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
        let saved = rescale((3000.0, 200.0), 1.0, primary);
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
        assert!((physical.0 - 3000.0).abs() < 1e-9 && (physical.1 - 673.5).abs() < 1e-9);
        assert_eq!(restored.monitor.as_deref(), Some("side"));
        assert_eq!(
            area_for(&areas, saved).and_then(|area| area.name.as_deref()),
            Some("side")
        );
        // The window starts the pet's 8-point padding, at the side display's
        // scale, above and left of it.
        let window_at = rescale(
            restored_window_origin((restored.x, restored.y), window / primary),
            primary,
            1.0,
        );
        assert!((window_at.0 - (3000.0 - 258.75)).abs() < 1e-9 && window_at.1.abs() < 1e-9);
    }

    #[test]
    fn a_pet_clamped_onto_a_denser_display_stays_whole_in_its_own_points() {
        // A 100% primary and a 150% display to its right, in primary points.
        // The full fixed window is 690 by 900 units on the dense display.
        let side = WorkArea {
            unit: 1.5,
            ..area("side", 1920.0, 0.0, 2560.0, 1440.0)
        };
        let areas = [area("primary", 0.0, 0.0, 1920.0, 1080.0), side.clone()];
        let edge = (
            side.area.right() - (WINDOW_WIDTH - pet_offset(false, PetLayout::default()).0) * side.unit,
            side.area.bottom() - (WINDOW_HEIGHT - pet_offset(false, PetLayout::default()).1) * side.unit,
        );
        // Overlapping the right edge: pulled in by the fixed window extent.
        let past_edge = PetAnchor {
            x: side.area.right() - 100.0,
            y: 300.0,
            monitor: Some("side".to_owned()),
        };
        let pulled = clamp_anchor(&past_edge, &areas);
        assert_eq!(
            (pulled.x, pulled.y),
            (
                edge.0,
                side.area.y + pet_offset(false, PetLayout::default()).1 * side.unit
            )
        );
        // Inside by the image size but not by the full window is not whole.
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
        let (dx, dy) = pet_offset(false, PetLayout::default());
        assert_eq!(
            (corner.x, corner.y),
            (
                side.area.right() - (WINDOW_WIDTH + DEFAULT_MARGIN - dx) * side.unit,
                side.area.bottom() - (WINDOW_HEIGHT + DEFAULT_MARGIN - dy) * side.unit,
            )
        );
        assert_eq!(clamp_anchor(&corner, &areas), corner);
    }
}
