# Vibey macOS tray icon

## Change contract

Preserve the first preview approved by Max on 2026-10-16, without redrawing its
silhouette, eyes, or status marks. The second preview was rejected.

- Serving: solid cloud with transparent eyes.
- Starting and stopping: the same cloud with three transparent dots.
- Unreachable: the same cloud with a transparent exclamation mark.
- Stopped: the original outline cloud.

The macOS status item is a monochrome template at creation and after every
status refresh. Menu text, lifecycle behavior, desktop pet, Dock/application
icons, and non-macOS tray rendering keep their existing behavior.

## Visible-size correction after rc9

The original 36x36px exports contained only about 24x21px of visible artwork.
At the tray's 18pt logical image height, the cloud was only about 10.5pt tall.
The owner requested a size/padding correction while retaining V1's design.

After the first size-correction preview, Max requested exactly 16pt of visible
height. The final exports are 20x18px at 1x and 38x36px at 2x. Visible bounds
are 18x16px and 36x32px respectively. The top/bottom guards are 1px at 1x and
2px at 2x; both exports have a fully transparent 1px horizontal guard.

`icons/tray/source/` preserves the original 192x192px V1 source images. Each
export is resampled directly from its source alpha, never from the old small
PNGs. Find the nonzero-alpha bounding box, center it, and apply one uniform
scale that fits within the target content bounds; use Lanczos downsampling.
Export black RGB with the resampled alpha as RGBA, clearing only the explicit
guard pixels to discard resampling ringing. The outlined state's stroke,
eyes, and all status marks are preserved. The differing logical canvas widths
at 1x/2x follow the requested one-pixel horizontal guard at each resolution;
production embeds the 2x export and displays a 19x18pt image with 16pt artwork.

## Native rendering contract

The pinned `tray-icon` 0.24.1 macOS implementation sets `NSImage` height to
18pt and width to `source_width / source_height * 18`. It preserves aspect
ratio instead of forcing a square. A real isolated Tauri probe verified both
36x36px -> 18x18pt and 40x36px -> 20x18pt, at creation and after image refresh.
The final 38x36px exports similarly display at 19x18pt. All four corrected
status images retained `isTemplate() == true`.

Tauri embeds the 2x files; macOS scales them for 1x displays. The 1x exports
remain available for inspection and are covered by the asset test. There is
no new runtime dependency or image decoder.

Use `set_icon_with_as_template` for refreshes: the pinned macOS implementation
of plain `set_icon` resets the template flag.

## Validation

The existing asset test protects monochrome transparency, dimensions and
canvas boundaries at both sizes. It now also requires exactly 16/32px of visible height and the requested
transparent guards. The initial occupancy assertion failed on rc9 assets for
excess padding; the final exact-height assertion also rejects the intermediate
17pt preview that the owner considered too large. Existing state tests still cover distinct
states and the shared starting/stopping image.

Native visual acceptance compares all four states alongside other installed
apps in the real menu bar, in light and dark appearance. Verify the system
appearance is Automatic before and after capture and restore temporary
transparency changes. Measure the filled cloud and neighboring QQ/WeChat icons
with the same brightness threshold in the 2x screenshot; target 32px each. The probe is temporary, uses a separate identifier,
and enables no product plugins, windows, or Runtime. Its source must be removed
from the worktree after capture. This evidence covers native rendering, not
the full packaged Runtime lifecycle.

Require exact-head Codex review, zero unresolved threads and passing CI. Wait
for Max to approve the screenshots before merging. Do not post a review-trigger
comment, publish a release, or modify the installed application.
