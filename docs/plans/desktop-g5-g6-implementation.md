# Desktop deep-link and window-frame implementation

## Change contract

Implements the authoritative `desktop-deep-links.md` and
`desktop-window-state.md` contracts from PR #1979, including the 4bd96455c
amendment. Requires #1979 merged first; its branch is not stacked into this lane.

- Native argv, single-instance delivery, and a macOS raw GURL Apple Event handler share a
  Tauri-free parser and one pending target. The first valid link in one delivery
  wins; a later valid delivery replaces an unconsumed target.
- Only a ready, adopted Runtime supplies the origin. Bootstrap consumes the
  pending target once; bootstrap failure drops it. A hot delivery waits until
  the WebView has reached that origin before applying the same route mapping.
- Raw links never become JavaScript or error copy. Complete dot segments are
  rejected; ordinary identifier dots remain valid. Percent escapes fail the
  literal identifier character rule. The kind is the allow-listed URL authority;
  userinfo, ports, host-less spellings, and normalized nonliteral input are rejected.
- The window-state plugin is the only frame store. It restores native geometry
  before showing `main`; a native frame hook clamps against monitor work areas
  with DPI-aware minimum dimensions. Content navigation never reapplies defaults.
- Native move/resize events debounce the plugin's existing save operation. Only
  position, size, and maximized state are enabled; visibility and fullscreen are
  excluded. Hide/Open keeps the same window and frame.

## Boundaries and non-goals

No new application command, capability, remote grant, React route, Python link
producer, notification, or second native window. The local-first data-location
and bootstrap-only IPC boundaries remain unchanged. G8 and producer adoption are
explicitly deferred; malformed links are silently dropped.

## Verification

- Parser properties cover exact mappings, literal identifiers, origin stability,
  malformed grammar, and one-shot hot delivery without Tauri.
- Existing fake-Runtime bootstrap tests exercise cold staging, failure discard,
  and retry without replay. Native source-boundary tests check OS-only entry
  points, unchanged commands/capabilities, and frame-only plugin lifecycle hooks.
- Clamp properties check title-bar visibility, DPI-aware minimum dimensions,
  idempotence, and preservation of accessible frames.
- macOS descriptor and Objective-C selector tests retain literal strings through
  the native adapter into the same stash, including rejected dot segments,
  percent-encoded dot segments, embedded NUL, and Unicode. No AppKit event loop,
  installed application, or external Apple Event delivery is required.
- Local gates: bootstrap npm install/build/i18n, Rust formatting, all-target
  all-feature clippy, and all-feature workspace tests. CI repeats on macOS and
  Windows. Packaged OS scheme registration and native frame round-trip remain
  manual acceptance checks when an isolated graphical environment is unavailable.

## Review correction: native input fidelity

Review 5165620370 on head 722e04e143 identified one input-fidelity class: Tao
0.35.3 parses NSURL text before Tauri exposes `RunEvent::Opened`, so stringifying
that event cannot enforce the literal grammar. The orchestrator approved a raw
GURL handler in plugin setup and removal on Exit, with no normalized fallback.
The grammar and other OS/G6 behavior are unchanged.

Only macOS directly depends on the already-locked objc2 0.6.4 and Foundation
0.3.2, adding the descriptor/manager features. The adapter uses Foundation's
documented selectors with 32-bit FourCC values, not Core Services bindings or a
new framework. The Objective-C handler owns its Rust callback, is retained on
the installing thread until Exit removes the registration, and never writes a
reply descriptor. No delegate replacement, swizzle, or current-event lookup is
used. Plugin setup runs before Tauri's event loop, while the process-level stash
already exists, so an early event does not depend on window or Runtime readiness.
