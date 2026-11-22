# Main Workbench native title-bar spacing

## Contract

Match the standalone Show compact title bar's native traffic-light placement:
left 12 pt, top 13 pt, center 20 pt on the local macOS 27.2 system. Preserve
the main window's transparent overlay, drag regions and full-screen lifecycle.
Move the sidebar brand row and ordinary content top spacing down coherently.
The main Workbench change is separate from Show PR 4108; other native app
windows and sidebar footer are outside its scope.

The existing configured native-position path owns traffic-light placement.
Tauri's configured `y` controls the title-bar container rather than the
button's top edge: changing 16→22 yields measured top 7→13. Horizontal 12
is unchanged. The page inset becomes 34; sidebar content moves down 6 pt.
Ordinary desktop content uses inset+4 for top padding (32→38). Chat's
existing header and Settings' existing inset layout remain their own paths.

## Validation and delivery

- `npm run build` passes.
- The AppShell and desktop shell consumer suites pass (87 tests). The native
  title-bar unit tests (2) and existing overlay geometry boundary test pass.
- A real native preview using the existing placement function measures
  left 12/top 13, matching Show.
- The actual product UI uses hermetic fixture APIs and an incognito WebView.
  No live Runtime or user state is used.
- After the console was unlocked at 11:29, real system-composited windows
  were captured and inspected at 1200×800 logical points: Chinese light
  whole-window and top-left before/after, dark, and collapsed capability/project
  sections. Traffic lights measure left 12/top 13 in all three candidate captures.
- The sidebar brand row moves down 6 pt; the main container's top padding
  changes 32→38 pt. The vertically centered home content consequently moves
  about 3 pt. The sidebar footer retains its existing position.
- There is no full desktop sidebar collapse in the current product; the
  collapsed capture shows the existing capability/project section controls.
- Max approved the real-window v2 comparison on November 22 at 12:04.
  Run focused native/consumer checks, then push and follow PR delivery gates.

Evidence: the main matter's `design/titlebar-38/` directory. Its historical
name retains the earlier request; Max approved the system 40 pt row at 09:43
on November 22. Invalid locked-console screenshots are explicitly segregated.

## November 22, 11:42 refinement

Max confirmed that the brand row should move by the same 6 pt as the traffic
lights, preserving their original gap. V2 therefore uses a 34 pt main-window
content clearance (28+6), while Show retains its 40 pt system toolbar. The
ordinary right-side content padding follows the same 6 pt increment (32→38).
Home retains its existing vertical centering, so the home group moves about 3
pt; no extra transform or bottom-spacing adjustment is introduced.

V2 native screenshots were rendered with the candidate's actual inset script
and control placement function. The before/after brand row pixels are exactly
identical when shifted 12 physical pixels at 2x; the sidebar footer pixels are
identical with no shift. The left/top traffic-light measurement remains 12/13.
V2 whole-window and top-left comparisons use the `-v2.png` suffix. Max approved
the traffic lights and logo at 12:04, authorizing the separate Workbench PR.
The UI build passes.
