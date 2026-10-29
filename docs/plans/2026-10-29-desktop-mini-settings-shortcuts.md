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
