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
uses the platform default without rewriting the file. Reset is one native
transaction for the complete pair and hold mode, so swapped shortcuts can be
restored without an intermediate conflict.

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
