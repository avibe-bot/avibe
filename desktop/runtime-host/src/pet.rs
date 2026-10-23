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

    /// Where `frame` puts the window for a pet at `anchor` on this display.
    pub fn window_rect(&self, anchor: (f64, f64), frame: PetFrame) -> LogicalRect {
        let (x, y) = frame.origin(anchor, self.unit);
        LogicalRect {
            x,
            y,
            width: WINDOW_WIDTH * self.unit,
            height: frame.height * self.unit,
        }
    }
}

// Shared with pet-mini.css. Bottom layout: image top-left (172.5, 449), dock at
// y=552. Top layout: image top-left (172.5, 12), dock at y=115.
pub const PET_SIZE: f64 = 115.0;
pub const WINDOW_WIDTH: f64 = 460.0;
pub const WINDOW_HEIGHT: f64 = 600.0;
const PET_BOTTOM: f64 = 36.0;
/// The image's inset from the window's top in the top layout; it keeps the
/// listening glow (8px above the image) inside the window.
const PET_TOP: f64 = 12.0;
// pet-mini.css: the dock starts 103px below the image top and is 40px high.
const PET_AND_DOCK_HEIGHT: f64 = 143.0;
/// The strip a dragged window shrinks to: Vibey and its dock, `PET_TOP` above
/// the image and `PET_BOTTOM` below it, at full width. Both layouts draw Vibey
/// at the same place in it, and it is all a drag must keep on the display.
pub const DRAG_HEIGHT: f64 = PET_TOP + PET_SIZE + PET_BOTTOM;
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

/// Which side of Vibey the column opens on, shared by the native frame and the
/// page. `edge: bottom` puts Vibey at the bottom of the window, so the column
/// grows upward; `edge: top` puts Vibey at the top and the column grows down.
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

impl PetLayout {
    fn edge(panel_edge: PanelEdge) -> Self {
        Self {
            panel_edge,
            ..Self::default()
        }
    }
}

/// The native window around the pet: its layout and its height in the pet
/// display's points. The width is always `WINDOW_WIDTH`, and nothing in the
/// page's interaction states changes either.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PetFrame {
    pub layout: PetLayout,
    pub height: f64,
}

impl Default for PetFrame {
    fn default() -> Self {
        Self {
            layout: PetLayout::default(),
            height: WINDOW_HEIGHT,
        }
    }
}

impl PetFrame {
    /// The dragged strip (see `DRAG_HEIGHT`). It keeps `layout`, which places
    /// the image at the same point in it either way.
    pub fn dragging(layout: PetLayout) -> Self {
        Self {
            layout,
            height: DRAG_HEIGHT,
        }
    }

    pub fn size(&self) -> (f64, f64) {
        (WINDOW_WIDTH, self.height)
    }

    /// The image's top-left inside the window, in window points.
    pub fn pet_offset(&self) -> (f64, f64) {
        let x = (WINDOW_WIDTH - PET_SIZE) / 2.0;
        match self.layout.panel_edge {
            PanelEdge::Bottom => (x, self.height - PET_BOTTOM - PET_SIZE),
            PanelEdge::Top => (x, PET_TOP),
        }
    }

    /// The window origin that puts the image on `anchor`, on a display whose
    /// points are `unit` long in the anchor's space.
    pub fn origin(&self, anchor: (f64, f64), unit: f64) -> (f64, f64) {
        let (dx, dy) = self.pet_offset();
        (anchor.0 - dx * unit, anchor.1 - dy * unit)
    }

    /// The anchor (the image's top-left) of a window at `origin`.
    pub fn anchor(&self, origin: (f64, f64), unit: f64) -> (f64, f64) {
        let (dx, dy) = self.pet_offset();
        (origin.0 + dx * unit, origin.1 + dy * unit)
    }
}

/// Moves a point between two views of one physical desktop, each that desktop
/// divided by a scale. The pet window's own scale changes as it crosses
/// displays, so a saved anchor uses one fixed scale that the next restore
/// shares; scale 1 is the physical desktop itself.
pub fn rescale(point: (f64, f64), from_scale: f64, to_scale: f64) -> (f64, f64) {
    let factor = from_scale / to_scale;
    (point.0 * factor, point.1 * factor)
}

/// The window for a pet at `anchor` on `area` (decision 2026-10-23). The
/// column opens upward, as it always has, while the full window fits above
/// Vibey, or while there is at least as much room above as below; otherwise
/// it flips and opens downward. Either way the window extends from Vibey
/// toward the column only as far as the work area goes, up to its full height,
/// so no part of it is ever off the display: macOS never lets a window rise
/// above the menu bar, so a frame that did would move Vibey instead.
pub fn settle_frame(anchor: (f64, f64), area: &WorkArea) -> PetFrame {
    let unit = area.unit;
    let above = (anchor.1 - area.area.y) / unit;
    let below = (area.area.bottom() - anchor.1) / unit - PET_AND_DOCK_HEIGHT;
    let full_above = WINDOW_HEIGHT - PET_BOTTOM - PET_SIZE;
    if above >= full_above || above >= below {
        return upward_frame(anchor, area);
    }
    PetFrame {
        layout: PetLayout::edge(PanelEdge::Top),
        height: (PET_TOP + PET_AND_DOCK_HEIGHT + below).clamp(DRAG_HEIGHT, WINDOW_HEIGHT),
    }
}

/// The window with the column opening upward whatever the room, as tall as
/// the room above Vibey allows: for a page that predates the top layout (a
/// shell can adopt an older running Runtime). Vibey still stays put.
pub fn upward_frame(anchor: (f64, f64), area: &WorkArea) -> PetFrame {
    let above = (anchor.1 - area.area.y) / area.unit;
    PetFrame {
        layout: PetLayout::default(),
        height: (above + PET_SIZE + PET_BOTTOM).clamp(DRAG_HEIGHT, WINDOW_HEIGHT),
    }
}

pub fn default_anchor(work_area: &WorkArea) -> PetAnchor {
    let (dx, dy) = PetFrame::default().pet_offset();
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

/// Keeps a restored pet whole on a connected display. A pet already inside a
/// work area stays exactly where it was. Otherwise it moves into the area it
/// overlaps most; with no overlap, into its own display if that is still
/// connected, else the first area (the primary display).
///
/// What must be inside is what a drag must keep on the display: Vibey and the
/// dock with the strip's margins above and below them (`DRAG_HEIGHT`), which
/// leaves room for the window's edge on the column's side in either layout
/// (`settle_frame`). It spans the window's width on a display that fits the
/// whole window, else Vibey's.
pub fn clamp_anchor(anchor: &PetAnchor, work_areas: &[WorkArea]) -> PetAnchor {
    let at = (anchor.x, anchor.y);
    let footprint = |area: &WorkArea| {
        let unit = area.unit;
        let strip = area.window_rect(at, PetFrame::dragging(PetLayout::default()));
        if area.area.width >= WINDOW_WIDTH * unit && area.area.height >= WINDOW_HEIGHT * unit {
            strip
        } else {
            LogicalRect {
                x: anchor.x,
                width: PET_SIZE * unit,
                ..strip
            }
        }
    };
    if let Some(home) = work_areas.iter().find(|area| area.area.contains(&footprint(area))) {
        return PetAnchor {
            monitor: home.name.clone().or_else(|| anchor.monitor.clone()),
            ..anchor.clone()
        };
    }
    let image = |area: &WorkArea| LogicalRect {
        x: anchor.x,
        y: anchor.y,
        width: PET_SIZE * area.unit,
        height: PET_SIZE * area.unit,
    };
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

/// The window for a pet at `anchor`, on the display it is on (else the
/// first): `settle_frame` there, or the full window with no display at all.
pub fn frame_at(anchor: (f64, f64), areas: &[WorkArea]) -> PetFrame {
    area_for(areas, anchor).map_or_else(PetFrame::default, |area| settle_frame(anchor, area))
}

/// Where a dropped pet comes to rest, and the window for it there. A drag can
/// leave the pet partly off every display (Windows lets a window go), so it
/// comes back whole like a restored one, recording the display it is on.
pub fn place_dropped(dropped: (f64, f64), areas: &[WorkArea]) -> (PetAnchor, PetFrame) {
    let anchor = clamp_anchor(
        &PetAnchor {
            x: dropped.0,
            y: dropped.1,
            monitor: None,
        },
        areas,
    );
    let frame = frame_at((anchor.x, anchor.y), areas);
    (anchor, frame)
}

/// Where a pet moved by something other than the shell's own drag comes to
/// rest, and its window: the platform after a display change, or an older
/// page dragging the window natively. Like a drop (`place_dropped`), except
/// that a page without the top layout always gets the upward window.
pub fn place_moved(moved: (f64, f64), areas: &[WorkArea], page_layouts: bool) -> (PetAnchor, PetFrame) {
    let (anchor, frame) = place_dropped(moved, areas);
    if page_layouts {
        return (anchor, frame);
    }
    let at = (anchor.x, anchor.y);
    let frame = area_for(areas, at).map_or_else(PetFrame::default, |area| upward_frame(at, area));
    (anchor, frame)
}

/// The layout the page should draw, and its revision. The shell is its only
/// source (spec §9.1): the page reads it from `pet_ready` and from the layout
/// event, keeps only a newer revision, and acknowledges each one it draws.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct LayoutSnapshot {
    pub revision: u64,
    pub layout: PetLayout,
}

/// What the shell does with the native window after a `FrameState` step.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum FrameStep {
    /// The window stays as it is.
    Keep,
    /// Put the window at `anchor` (window points) as `frame`, now.
    Apply { frame: PetFrame, anchor: (f64, f64) },
    /// Tell the page to draw `snapshot`; its frame waits for the page's
    /// acknowledgement of that revision (or `expire`).
    Publish(LayoutSnapshot),
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct Held {
    frame: PetFrame,
    anchor: (f64, f64),
    revision: u64,
}

/// The pet window's frame, owned by the shell, and the layout its page must
/// draw. A change of layout is never shown in the full window before the page
/// has drawn it: the frame is held until the page acknowledges that revision.
/// A frame change that keeps the layout applies at once.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct FrameState {
    current: PetFrame,
    dragging: bool,
    held: Option<Held>,
    snapshot: LayoutSnapshot,
}

impl FrameState {
    pub fn new(frame: PetFrame) -> Self {
        Self {
            current: frame,
            dragging: false,
            held: None,
            snapshot: LayoutSnapshot {
                revision: 1,
                layout: frame.layout,
            },
        }
    }

    /// A new window. Revisions keep growing, so an answer meant for the old
    /// window can never match the new one.
    pub fn rebuild(&mut self, frame: PetFrame) {
        let revision = self.snapshot.revision + 1;
        *self = Self::new(frame);
        self.snapshot.revision = revision;
    }

    pub fn current(&self) -> PetFrame {
        self.current
    }

    pub fn snapshot(&self) -> LayoutSnapshot {
        self.snapshot
    }

    /// The shell's own drag, or a frame waiting for its page: both place the
    /// window themselves, so nothing else may.
    pub fn busy(&self) -> bool {
        self.dragging || self.held.is_some()
    }

    /// Starts the shell's drag: returns the strip to apply, or `None` while
    /// another drag runs or a frame waits for its page.
    pub fn begin_drag(&mut self) -> Option<PetFrame> {
        if self.busy() {
            return None;
        }
        self.dragging = true;
        self.current = PetFrame::dragging(self.current.layout);
        Some(self.current)
    }

    /// The native drag did not start: the window goes back to `before`.
    pub fn cancel_drag(&mut self, before: PetFrame) {
        self.current = before;
        self.dragging = false;
    }

    /// The drag ended with the pet at `anchor`, framed `next`. A page that
    /// draws layouts draws the drop's revision before its window comes back;
    /// for one that does not, the (upward) window comes back at once.
    pub fn end_drag(&mut self, next: PetFrame, anchor: (f64, f64), page_layouts: bool) -> FrameStep {
        self.dragging = false;
        if page_layouts {
            self.hold(next, anchor)
        } else {
            self.apply(next, anchor)
        }
    }

    /// Something other than the shell's drag moved the window; `next` is the
    /// window for where it now is. Ignored while the shell places it itself.
    pub fn follow(&mut self, next: PetFrame, anchor: (f64, f64), moved: bool, page_layouts: bool) -> FrameStep {
        if self.busy() {
            return FrameStep::Keep;
        }
        if page_layouts && next.layout != self.current.layout {
            return self.hold(next, anchor);
        }
        if !moved && next == self.current {
            return FrameStep::Keep;
        }
        self.apply(next, anchor)
    }

    /// The page drew `revision`. Only the waiting frame's own revision
    /// releases it; any other acknowledgement changes nothing.
    pub fn ack(&mut self, revision: u64) -> FrameStep {
        match self.held {
            Some(held) if held.revision == revision => {
                self.held = None;
                self.current = held.frame;
                FrameStep::Apply {
                    frame: held.frame,
                    anchor: held.anchor,
                }
            }
            _ => FrameStep::Keep,
        }
    }

    /// No acknowledgement of `revision` came in time: its frame applies anyway.
    pub fn expire(&mut self, revision: u64) -> FrameStep {
        self.ack(revision)
    }

    fn hold(&mut self, frame: PetFrame, anchor: (f64, f64)) -> FrameStep {
        self.snapshot = LayoutSnapshot {
            revision: self.snapshot.revision + 1,
            layout: frame.layout,
        };
        self.held = Some(Held {
            frame,
            anchor,
            revision: self.snapshot.revision,
        });
        FrameStep::Publish(self.snapshot)
    }

    fn apply(&mut self, frame: PetFrame, anchor: (f64, f64)) -> FrameStep {
        if frame.layout != self.snapshot.layout {
            self.snapshot = LayoutSnapshot {
                revision: self.snapshot.revision + 1,
                layout: frame.layout,
            };
        }
        self.current = frame;
        FrameStep::Apply { frame, anchor }
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
    fn the_anchor_is_the_pet_image_in_every_frame() {
        let anchor = (1000.0, 700.0);
        for unit in [1.0, 1.25, 2.0] {
            for panel_side in [PanelSide::Left, PanelSide::Right] {
                for panel_edge in [PanelEdge::Top, PanelEdge::Bottom] {
                    let layout = PetLayout { panel_side, panel_edge };
                    for frame in [
                        PetFrame {
                            layout,
                            height: WINDOW_HEIGHT,
                        },
                        PetFrame { layout, height: 420.0 },
                        PetFrame::dragging(layout),
                    ] {
                        let origin = frame.origin(anchor, unit);
                        assert_eq!(frame.anchor(origin, unit), anchor);
                        let (width, height) = frame.size();
                        let (dx, dy) = frame.pet_offset();
                        assert!(dx >= 0.0 && dy >= 0.0 && dx + PET_SIZE <= width);
                        assert!(dy + PET_AND_DOCK_HEIGHT <= height, "the dock is inside the window");
                    }
                }
            }
        }
        // The dragged strip draws Vibey at the same place in either layout,
        // so a flip on drop needs no move while the window is the strip.
        assert_eq!(
            PetFrame::dragging(PetLayout::edge(PanelEdge::Top)).pet_offset(),
            PetFrame::dragging(PetLayout::edge(PanelEdge::Bottom)).pet_offset()
        );
        assert_eq!(DRAG_HEIGHT, 163.0);
        // The default (bottom) frame keeps today's image box.
        assert_eq!(PetFrame::default().pet_offset(), (172.5, 449.0));
        assert_eq!(PetFrame::default().size(), (WINDOW_WIDTH, WINDOW_HEIGHT));
    }

    #[test]
    fn the_column_opens_toward_its_room_and_the_window_never_leaves_the_display() {
        // A 13-inch MacBook's work area below the menu bar, a 125% 1366x768
        // laptop above a taskbar, and a tall external display.
        for area in [
            area("laptop", 0.0, 33.0, 1470.0, 923.0),
            area("small", 0.0, 0.0, 1093.0, 574.0),
            area("tall", 1470.0, -400.0, 1440.0, 1415.0),
            WorkArea {
                unit: 2.0,
                ..area("dense", 0.0, 0.0, 2880.0, 1800.0)
            },
        ] {
            let unit = area.unit;
            let top = area.area.y + PET_TOP * unit;
            let bottom = area.area.bottom() - (PET_SIZE + PET_BOTTOM) * unit;
            let mut y = top;
            while y <= bottom {
                let anchor = (area.area.x + 400.0 * unit, y);
                let frame = settle_frame(anchor, &area);
                let window = area.window_rect(anchor, frame);
                assert!(area.area.contains(&window), "{:?} at {y}: {window:?}", area.name);
                assert_eq!(
                    frame.anchor(frame.origin(anchor, unit), unit),
                    anchor,
                    "Vibey stays put"
                );
                assert!((DRAG_HEIGHT..=WINDOW_HEIGHT).contains(&frame.height));
                let above = (y - area.area.y) / unit;
                let below = (area.area.bottom() - y) / unit - PET_AND_DOCK_HEIGHT;
                let flipped = frame.layout.panel_edge == PanelEdge::Top;
                assert_eq!(flipped, above < 449.0 && below > above, "{:?} at {y}", area.name);
                // The column gets the full window whenever its side has room.
                let room = if flipped {
                    PET_TOP + PET_AND_DOCK_HEIGHT + below
                } else {
                    above + 151.0
                };
                assert_eq!(frame.height, room.min(WINDOW_HEIGHT));
                y += 7.0 * unit;
            }
        }

        let laptop = area("laptop", 0.0, 33.0, 1470.0, 923.0);
        // Dragged to the very top: the column drops below Vibey, full height.
        let at_top = settle_frame((600.0, 45.0), &laptop);
        assert_eq!(
            at_top,
            PetFrame {
                layout: PetLayout::edge(PanelEdge::Top),
                height: WINDOW_HEIGHT
            }
        );
        assert_eq!(laptop.window_rect((600.0, 45.0), at_top).y, 33.0);
        // Room for the full column above: today's layout and window.
        assert_eq!(settle_frame((600.0, 600.0), &laptop), PetFrame::default());
        // Between the two, the larger side wins and the window stops at the
        // menu bar instead of moving Vibey down.
        let middle = settle_frame((600.0, 433.0), &laptop);
        assert_eq!(
            middle,
            PetFrame {
                layout: PetLayout::default(),
                height: 551.0
            }
        );
        assert_eq!(laptop.window_rect((600.0, 433.0), middle).y, 33.0);
        assert_eq!(settle_frame((600.0, 333.0), &laptop).layout.panel_edge, PanelEdge::Top);

        let anchor = default_anchor(&laptop);
        let origin = PetFrame::default().origin((anchor.x, anchor.y), 1.0);
        assert_eq!(
            origin,
            (
                laptop.area.right() - WINDOW_WIDTH - DEFAULT_MARGIN,
                laptop.area.bottom() - WINDOW_HEIGHT - DEFAULT_MARGIN
            )
        );
        assert_eq!(settle_frame((anchor.x, anchor.y), &laptop), PetFrame::default());
    }

    #[test]
    fn a_page_without_the_top_layout_gets_an_upward_window_that_keeps_vibey_put() {
        let laptop = area("laptop", 0.0, 33.0, 1470.0, 923.0);
        for y in [45.0, 200.0, 433.0, 700.0] {
            let frame = upward_frame((600.0, y), &laptop);
            assert_eq!(frame.layout, PetLayout::default());
            let window = laptop.window_rect((600.0, y), frame);
            assert!(laptop.area.contains(&window), "{y}: {window:?}");
            assert_eq!(frame.anchor(frame.origin((600.0, y), 1.0), 1.0), (600.0, y));
        }
        // Near the top the window is only the strip; with room it is whole.
        assert_eq!(upward_frame((600.0, 45.0), &laptop).height, DRAG_HEIGHT);
        assert_eq!(upward_frame((600.0, 700.0), &laptop), PetFrame::default());
        // Where the column opens upward anyway, both choices agree.
        assert_eq!(
            settle_frame((600.0, 433.0), &laptop),
            upward_frame((600.0, 433.0), &laptop)
        );
    }

    #[test]
    fn a_pet_moved_outside_a_shell_drag_gets_the_window_for_where_it_now_is() {
        let laptop = area("laptop", 0.0, 33.0, 1470.0, 923.0);
        let short = area("short", 0.0, 0.0, 1093.0, 574.0);
        // An older page dragged its strip-high window down: the upward
        // window grows back to its full height.
        let (anchor, frame) = place_moved((600.0, 700.0), std::slice::from_ref(&laptop), false);
        assert_eq!((anchor.x, anchor.y), (600.0, 700.0));
        assert_eq!(frame, PetFrame::default());
        // Near the top an older page still opens upward; a current one flips.
        assert_eq!(
            place_moved((600.0, 45.0), std::slice::from_ref(&laptop), false)
                .1
                .layout,
            PetLayout::default()
        );
        assert_eq!(
            place_moved((600.0, 45.0), std::slice::from_ref(&laptop), true)
                .1
                .layout
                .panel_edge,
            PanelEdge::Top
        );
        // The display it was on is gone: it lands whole on the remaining one,
        // in a window that fits its height (a display narrower than the full
        // window keeps Vibey, not the transparent sides, inside).
        let (anchor, frame) = place_moved((3000.0, 900.0), std::slice::from_ref(&short), true);
        let window = short.window_rect((anchor.x, anchor.y), frame);
        assert!(window.y >= short.area.y && window.bottom() <= short.area.bottom());
        assert!(anchor.x + PET_SIZE <= short.area.right());
        assert_eq!(anchor.monitor.as_deref(), Some("short"));
    }

    /// A page in the model: the document now showing, and what it drew.
    #[derive(Clone, Copy)]
    struct Page {
        document: u32,
        drawn: Option<LayoutSnapshot>,
    }

    enum Message {
        /// Shell to page: a snapshot (layout event or `pet_ready` reply),
        /// for the document it was sent to.
        Snapshot(u32, LayoutSnapshot),
        /// Page to shell: `pet_settle(revision)` for the layout it drew.
        Ack(u64, PetLayout),
    }

    #[test]
    fn a_held_frame_comes_back_only_in_the_layout_its_page_drew_whatever_the_interleaving() {
        let frames = [
            PetFrame::default(),
            PetFrame {
                layout: PetLayout::edge(PanelEdge::Top),
                height: WINDOW_HEIGHT,
            },
            PetFrame {
                layout: PetLayout::default(),
                height: 420.0,
            },
            PetFrame {
                layout: PetLayout::edge(PanelEdge::Top),
                height: 300.0,
            },
        ];
        // A small deterministic generator: many seeds, no shared randomness.
        for seed in 1..400u64 {
            let mut next = seed;
            let mut roll = |n: u64| {
                next = next.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                (next >> 33) % n
            };
            let mut state = FrameState::new(frames[0]);
            let mut page = Page {
                document: 0,
                drawn: None,
            };
            let mut queue: Vec<Message> = vec![Message::Snapshot(0, state.snapshot())];
            let mut published = vec![state.snapshot().revision];
            let mut before_drag = None;
            for _ in 0..60 {
                let revision = state.snapshot().revision;
                let step = match roll(8) {
                    0 => {
                        let before = state.current();
                        let strip = state.begin_drag();
                        if strip.is_some() {
                            before_drag = Some(before);
                        }
                        assert_eq!(strip.is_some(), before_drag.is_some() && strip == Some(state.current()));
                        FrameStep::Keep
                    }
                    1 => match before_drag.take() {
                        Some(before) if roll(4) == 0 => {
                            state.cancel_drag(before);
                            FrameStep::Keep
                        }
                        Some(_) => state.end_drag(frames[roll(4) as usize], (0.0, 0.0), true),
                        None => FrameStep::Keep,
                    },
                    2 => state.follow(frames[roll(4) as usize], (0.0, 0.0), roll(2) == 0, true),
                    3 => {
                        // The document reloads; the new one reads `pet_ready`.
                        page = Page {
                            document: page.document + 1,
                            drawn: None,
                        };
                        queue.push(Message::Snapshot(page.document, state.snapshot()));
                        FrameStep::Keep
                    }
                    4 => {
                        let stale = published[roll(published.len() as u64) as usize];
                        state.expire(stale)
                    }
                    _ => {
                        if queue.is_empty() {
                            FrameStep::Keep
                        } else {
                            match queue.remove(roll(queue.len() as u64) as usize) {
                                Message::Snapshot(document, snapshot) => {
                                    let newer = page.drawn.is_none_or(|drawn| snapshot.revision > drawn.revision);
                                    if document == page.document && newer {
                                        page.drawn = Some(snapshot);
                                        queue.push(Message::Ack(snapshot.revision, snapshot.layout));
                                    }
                                    FrameStep::Keep
                                }
                                Message::Ack(acked, drawn) => {
                                    let before = state;
                                    let step = state.ack(acked);
                                    if let FrameStep::Apply { frame, .. } = step {
                                        assert_eq!(
                                            frame.layout, drawn,
                                            "seed {seed}: a held frame came back in another layout"
                                        );
                                    } else {
                                        assert_eq!(
                                            state, before,
                                            "seed {seed}: a stale acknowledgement changed something"
                                        );
                                    }
                                    step
                                }
                            }
                        }
                    }
                };
                if let FrameStep::Publish(snapshot) = step {
                    queue.push(Message::Snapshot(page.document, snapshot));
                    published.push(snapshot.revision);
                }
                assert!(
                    state.snapshot().revision >= revision,
                    "seed {seed}: revisions only grow"
                );
                if !state.busy() {
                    assert_eq!(state.snapshot().layout, state.current().layout, "seed {seed}");
                }
            }
            // Quiescence: finish any drag, deliver everything, and the page
            // draws exactly the layout of the window the shell shows.
            if let Some(before) = before_drag {
                state.cancel_drag(before);
            }
            while !queue.is_empty() {
                match queue.remove(0) {
                    Message::Snapshot(document, snapshot) => {
                        let newer = page.drawn.is_none_or(|drawn| snapshot.revision > drawn.revision);
                        if document == page.document && newer {
                            page.drawn = Some(snapshot);
                            queue.push(Message::Ack(snapshot.revision, snapshot.layout));
                        }
                    }
                    Message::Ack(acked, _) => {
                        state.ack(acked);
                    }
                }
            }
            assert!(
                !state.busy(),
                "seed {seed}: a frame is still held after its page answered"
            );
            let drawn = page.drawn.expect("the current document read a snapshot");
            assert_eq!(drawn, state.snapshot(), "seed {seed}");
            assert_eq!(drawn.layout, state.current().layout, "seed {seed}");
        }
    }

    #[test]
    fn a_frame_change_waits_for_its_page_only_when_the_layout_changes() {
        let top = PetFrame {
            layout: PetLayout::edge(PanelEdge::Top),
            height: WINDOW_HEIGHT,
        };
        let mut state = FrameState::new(PetFrame::default());
        // A shorter window in the same layout applies at once.
        let shorter = PetFrame {
            height: 420.0,
            ..PetFrame::default()
        };
        assert_eq!(
            state.follow(shorter, (1.0, 2.0), true, true),
            FrameStep::Apply {
                frame: shorter,
                anchor: (1.0, 2.0)
            }
        );
        assert_eq!(state.snapshot().revision, 1);
        // Another layout is held and published.
        let FrameStep::Publish(snapshot) = state.follow(top, (1.0, 2.0), true, true) else {
            panic!()
        };
        assert_eq!(
            snapshot,
            LayoutSnapshot {
                revision: 2,
                layout: top.layout
            }
        );
        assert!(state.busy());
        assert_eq!(
            state.follow(PetFrame::default(), (0.0, 0.0), true, true),
            FrameStep::Keep,
            "held: nothing else places it"
        );
        assert_eq!(state.begin_drag(), None, "no drag while a frame waits for its page");
        assert_eq!(state.ack(1), FrameStep::Keep);
        assert_eq!(
            state.ack(2),
            FrameStep::Apply {
                frame: top,
                anchor: (1.0, 2.0)
            }
        );
        assert_eq!(
            state.ack(2),
            FrameStep::Keep,
            "a duplicate acknowledgement changes nothing"
        );
        // Every drop is held for a page that draws layouts, even in the same
        // layout, so its full window returns only after the page has drawn.
        let strip = state.begin_drag().unwrap();
        assert_eq!(strip.height, DRAG_HEIGHT);
        assert_eq!(state.begin_drag(), None);
        assert!(matches!(
            state.end_drag(top, (3.0, 4.0), true),
            FrameStep::Publish(LayoutSnapshot { revision: 3, .. })
        ));
        assert_eq!(
            state.expire(3),
            FrameStep::Apply {
                frame: top,
                anchor: (3.0, 4.0)
            }
        );
        // An older page never holds anything.
        state.begin_drag();
        assert_eq!(
            state.end_drag(PetFrame::default(), (5.0, 6.0), false),
            FrameStep::Apply {
                frame: PetFrame::default(),
                anchor: (5.0, 6.0)
            }
        );
        assert_eq!(
            state.snapshot(),
            LayoutSnapshot {
                revision: 4,
                layout: PetLayout::default()
            }
        );
        // A failed drag start puts the window back; a new window keeps revisions growing.
        let before = state.current();
        state.begin_drag();
        state.cancel_drag(before);
        assert_eq!((state.current(), state.busy()), (before, false));
        state.rebuild(top);
        assert_eq!(
            state.snapshot(),
            LayoutSnapshot {
                revision: 5,
                layout: top.layout
            }
        );
    }

    #[test]
    fn a_dropped_pet_stays_where_it_lands_unless_it_left_every_display() {
        let areas = [
            area("laptop", 0.0, 33.0, 1470.0, 923.0),
            area("external", 1470.0, -400.0, 1440.0, 1415.0),
        ];
        // Dropped at the top of the laptop: it stays, and its column flips.
        let (anchor, frame) = place_dropped((600.0, 45.0), &areas);
        assert_eq!(
            (anchor.x, anchor.y, anchor.monitor.as_deref()),
            (600.0, 45.0, Some("laptop"))
        );
        assert_eq!(frame.layout.panel_edge, PanelEdge::Top);
        // Halfway up the external display: today's layout, the full window.
        let (anchor, frame) = place_dropped((2000.0, 300.0), &areas);
        assert_eq!(
            (anchor.x, anchor.y, anchor.monitor.as_deref()),
            (2000.0, 300.0, Some("external"))
        );
        assert_eq!(frame, PetFrame::default());
        // Partly off the laptop's top-left corner: it comes back whole, just
        // inside, with its column below it.
        let (anchor, frame) = place_dropped((-80.0, 10.0), &areas);
        assert_eq!((anchor.x, anchor.y), (172.5, 33.0 + 12.0));
        assert_eq!(frame.layout.panel_edge, PanelEdge::Top);
        assert!(areas[0]
            .area
            .contains(&areas[0].window_rect((anchor.x, anchor.y), frame)));
        // Without any display information nothing moves.
        assert_eq!(
            place_dropped((5.0, 6.0), &[]),
            (
                PetAnchor {
                    x: 5.0,
                    y: 6.0,
                    monitor: None
                },
                PetFrame::default()
            )
        );
    }

    #[test]
    fn a_display_change_settles_the_same_spot_in_the_new_display_points() {
        // The saved pet sits near the top of a 100% display, which is then
        // replaced by a 200% one of the same point size: the same anchor
        // flips and fills the same points, now twice as many pixels.
        let anchor = (500.0, 60.0);
        let plain = area("display", 0.0, 0.0, 1440.0, 900.0);
        let dense = WorkArea {
            unit: 2.0,
            ..plain.clone()
        };
        let before = settle_frame(anchor, &plain);
        assert_eq!(before.layout.panel_edge, PanelEdge::Top);
        // On the dense display the strip needs twice the room above Vibey.
        let clamped = clamp_anchor(
            &PetAnchor {
                x: anchor.0,
                y: anchor.1 - 50.0,
                monitor: Some("display".into()),
            },
            std::slice::from_ref(&dense),
        );
        assert_eq!(clamped.y, PET_TOP * 2.0);
        let after = settle_frame((clamped.x, clamped.y), &dense);
        assert_eq!(after.layout.panel_edge, PanelEdge::Top);
        assert!(dense.area.contains(&dense.window_rect((clamped.x, clamped.y), after)));
        assert_eq!(after.height, WINDOW_HEIGHT.min(PET_TOP + (900.0 - clamped.y) / 2.0));
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
        assert_eq!(
            (pulled.x, pulled.y),
            (172.5, 400.0),
            "only pulled sideways: near the top it flips"
        );

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
            assert_eq!(restored.y, 399.0, "the dock and the strip's margin below it must fit");
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
        assert_eq!((low_laptop.x, low_laptop.y), (100.0, 399.0));
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
        let at = (external.x, external.y);
        assert!(areas[1]
            .area
            .contains(&areas[1].window_rect(at, settle_frame(at, &areas[1]))));
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
        // Near the display's top it stays exactly where it was dragged.
        let physical = rescale((restored.x, restored.y), primary, 1.0);
        assert!((physical.0 - 3000.0).abs() < 1e-9 && (physical.1 - 200.0).abs() < 1e-9);
        assert_eq!(restored.monitor.as_deref(), Some("side"));
        assert_eq!(
            area_for(&areas, saved).and_then(|area| area.name.as_deref()),
            Some("side")
        );
        // The window starts the image's offset, at the side display's scale,
        // above and left of it: its column opens below Vibey there.
        let at = (restored.x, restored.y);
        let frame = settle_frame(at, &areas[1]);
        assert_eq!(frame.layout.panel_edge, PanelEdge::Top);
        let window_at = rescale(frame.origin(at, window / primary), primary, 1.0);
        assert!((window_at.0 - (3000.0 - 258.75)).abs() < 1e-9 && (window_at.1 - (200.0 - 18.0)).abs() < 1e-9);
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
            side.area.right() - (WINDOW_WIDTH - PetFrame::default().pet_offset().0) * side.unit,
            side.area.bottom() - (WINDOW_HEIGHT - PetFrame::default().pet_offset().1) * side.unit,
        );
        // Overlapping the right edge: pulled in by the window's width, and
        // left at its height, where its column opens below it.
        let past_edge = PetAnchor {
            x: side.area.right() - 100.0,
            y: 300.0,
            monitor: Some("side".to_owned()),
        };
        let pulled = clamp_anchor(&past_edge, &areas);
        assert_eq!((pulled.x, pulled.y), (edge.0, 300.0));
        assert_eq!(
            settle_frame((pulled.x, pulled.y), &side).layout.panel_edge,
            PanelEdge::Top
        );
        // Inside by the image size but not by the window's width is not whole.
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
        let (dx, dy) = PetFrame::default().pet_offset();
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
