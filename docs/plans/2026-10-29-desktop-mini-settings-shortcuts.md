# Desktop Mini settings and shortcuts

## Background

Desktop Mini currently owns one persisted shortcut and registers it only while
the Mini window exists. Settings cannot control the Mini, and the tray preset
menu is a second shortcut state.

## Goal

Expose one persisted Desktop Mini state to General Settings, Shortcuts, and the
tray. Support separate voice and show/hide shortcuts, hold-to-talk and
press-to-toggle voice input, safe registration replacement, and capability
gating for Web, remote browser, and older desktop shells.

## Contract

- `pet.json` keeps `shortcut` as the voice shortcut for existing users and adds
  optional show/hide and voice-mode fields.
- The show/hide shortcut remains registered while Mini is disabled. The voice
  shortcut is registered only while Mini is enabled.
- Rust owns persistence and registration. A refused replacement leaves the
  previous persisted and registered shortcut active and reports an inline
  error.
- Lifecycle reconciliation independently attempts every desired registration
  and retains every accepted member. All refused accelerators are returned to
  Settings. Settings edits instead commit the complete candidate transaction.
- Native voice conversation state belongs to the Mini document. Loading,
  destroying, replacing, or hiding it cancels its conversation and pending
  listen intent. A physical key latch survives until release while that
  accelerator remains registered.
- The tray keeps one switch and opens Shortcut Settings; it does not maintain a
  preset list.
- The Web UI renders new Desktop Mini controls only when the shell advertises
  the dedicated settings capability.

## Work items

- Extend runtime-host preferences and voice shortcut state transitions.
- Replace the tray preset submenu with a Shortcut Settings command.
- Add main-window settings IPC, event synchronization, and registration
  rollback.
- Add capability-gated General and Shortcuts controls with English/Chinese
  copy.
- Add focused Rust and Vitest coverage, then run desktop and UI build gates.

## Defaults and migration decision

On 2026-10-29 the owner selected `Alt+Shift+Space` for macOS voice input;
other platforms keep `Control+Alt+Space`. Show/hide stays
`Control+Alt+KeyM`, and the voice mode defaults to hold-to-talk.
`shortcut: Some(...)` is an explicit persisted choice, including the old
`Control+Alt+Space` default. It is preserved verbatim. An absent/null field
uses the platform default. Nonconflicting files are not rewritten. If the
resolved toggle collides with the voice accelerator, load keeps the voice
verbatim and persists the first nonconflicting former tray preset as toggle,
skipping reserved Alt+Z and Alt+X. Native accelerator equality also recognizes
aliases such as `Ctrl+Alt+M`. If that write fails, the saved file remains intact
and the deterministic nonconflicting pair is used in memory, with a native
warning. Reset is one native transaction for the complete pair and hold mode,
so swapped shortcuts can be restored without an intermediate conflict.

Apple's Mac keyboard shortcuts documentation lists Control-Space and
Control-Option-Space as selecting the previous and next input sources when
multiple sources are configured: <https://support.apple.com/en-us/102650>.
This is the conflict avoided by the new macOS default.

## Review evidence

The first reviewed head, `aabeb8fb81ebe2f0d0473bca21b7560dbee211d6`,
had five findings: show/hide key latch lifetime, lost registration failure
identity, non-atomic reset, native voice state after page-owned completion,
and hidden native persistence errors. The repair uses the real mutation
helpers in tests with isolated preference files and a simulated OS registry;
UI tests exercise capture rejection, native reset, and stop/cancel/failure/
completion of recording. Self-review also found the missing capability in the
Tauri enabled list; boundary coverage now includes that enabled grant.
Real-device global key delivery and microphone checks remain deferred to the
next rc, as requested.

The second findings-bearing head was
`a6fedb393b6b950b9c4c0118e8c0b6f58914dbe2`: lifecycle registration recovery
and native voice lifecycle ownership recurred, plus a new saved-voice/default-
toggle collision. The lane stopped editing and sent the complete eight-thread
inventory to the orchestrator. On 2026-10-29 the orchestrator authorized one
contract repair: independent lifecycle registration (including both refusals),
transactional edits, document-owned voice reset, and persisted collision
migration preserving the voice key.

The repair's consuming tests cover all registration outcomes with Mini on/off,
retrying only missing members, hiding after recovery, transactional refusal or
disk failure for either key, both voice modes across Runtime/document
replacement, physical repeats until key-up, stale queued listens, and migrated
voice aliases through the same action resolver used by the global handler.
Settings tests render both native errors and remove only the recovered one.
