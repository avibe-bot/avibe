# Show title-bar hover background

## Contract

Originally based on master `7059339739`, enlarge the native standalone Show window's
four hover backgrounds from 20 × 20 pt to 28 × 28 pt. Retain the 6 pt corner
radius, 14 pt symbols, 28 pt hit targets, 32 pt icon-center spacing, and 40 pt
system compact title bar. The background therefore has 6 pt clearance above
and below. No title, traffic-light, Workbench, action, or App Lock gate changes.

Only the existing `WASH` drawing constant changes. No spacing adjustment is
necessary. App Lock's existing guarded action dispatch remains untouched.

## Validation and delivery

- Compile the actual native module into the existing hermetic AppKit preview.
- Capture real system-composited windows in Chinese and English, light and
  dark, with default and hover states. The Show content is a local static
  fixture; no Runtime or user data is opened.
- Measure native button geometry and the screenshot difference at 2x scale:
  28 × 28 pt background, 6 pt top/bottom clearance, 32 pt center spacing.
- Run the existing native title-bar unit tests and real WebKit sharing/control
  probe. The rendering preview uses the existing explicit lock-free probe
  adapter; it does not claim to exercise authentication.
- Max authorized an independent PR and merge on November 23 at 20:11.
  Rebase onto master `56d35ffbc2`, rerun focused native checks, and require
  exact-head review with no findings, zero unresolved threads, green required
  CI and CLEAN status before a head-matched squash merge. Packaging and D2b
  remain outside this change.
- The earlier AppKit captures demonstrate preview geometry, not manual
  acceptance of an installed application. Do not activate preview windows
  again during this delivery.

Evidence lives in the main Show outbound-share matter's `hover-28-0845/`
directory, including preview source, geometry logs and unedited captures.
