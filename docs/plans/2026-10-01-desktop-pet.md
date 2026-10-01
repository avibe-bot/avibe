# Desktop Pet

Status: proposed (2026-10-01)

## Background

Avibe Desktop is a single-window Tauri v2 shell around the Workbench SPA. To
talk to an agent the user has to bring that window forward, find the session,
and type. Users want a lighter, always-available entry point: summon the agent
with a hotkey, speak, see the words appear, and see at a glance whether the
agent is working, waiting for them, or done.

Codex ships the same idea as "Pets". Lessons from its reception:

- an animated companion that reflects agent state;
- a global hotkey that opens a quick chat bar with voice;
- a hard off switch that every wake path respects;
- a rebindable hotkey;
- states that stay accurate;
- negligible idle cost.

The long-term target is the main agent: one long-lived session the user talks
to (see `2026-10-01-session-context-rotation.md`). Session types do not exist
yet, so v1 binds the pet to any session the user picks. When the main-agent
session type lands, the pet binds to it and the picker goes away.

## Concrete example

1. The user is in a browser. They press `⌃⌥Space`.
2. The pet, a small character in the screen corner, opens its panel. The mic
   starts, and the panel shows "Listening".
3. The user says "Summarize yesterday's PR reviews and open an issue for
   anything unresolved." The words stream into the panel as they speak.
4. They press `⌃⌥Space` again. The text is sent to the bound session `S`
   through the normal message API. The pet switches to **Running**, and the
   panel streams the agent's reply.
5. The agent starts a delegated run. The panel's activity strip shows "1 run".
6. The user clicks back into the browser. The panel collapses; the pet keeps
   its running animation.
7. The run finishes and the agent replies. The pet switches to **Ready** and
   shows a badge. The user clicks the pet, reads the reply, and the badge
   clears. That is the same "read" that the Workbench sidebar shows.
8. If the agent had needed a vault approval, the pet would have shown **Needs
   input**, and the panel would have offered "Open in Avibe" for that request.

## Scope

In v1:

- a pet window;
- a global hotkey;
- voice and text input to one bound session;
- live state, reply, and Harness progress for that session;
- an off switch.

Not in v1:

- custom or user-generated pets;
- multiple pets;
- binding the pet to more than one session;
- answering approvals inside the pet;
- a hotkey recorder UI.

## Design

### Window (desktop shell)

The pet is a second native window, label `pet`. This reopens G8 for one
window kind; see "G8 lifecycle" below.

- It is built by the shell with `WebviewWindowBuilder`:
  - `transparent`, `decorations(false)`, `always_on_top`;
  - `skip_taskbar`, `visible_on_all_workspaces`, not resizable by the user.
  - macOS transparency needs `app.macOSPrivateApi: true`. That rules out the
    Mac App Store, which Avibe does not ship to.
- It loads `<runtime-origin>/pet` directly. That is the same loopback origin
  and auth model as `main`, which treats loopback requests as local.
- **Two sizes.** Collapsed, the window is only as big as the pet sprite, so
  transparent areas around it do not swallow clicks. Expanded, the window
  grows to fit the panel, anchored at the pet. The panel flips to the side with
  room near screen edges. The shell performs the resize.
- The user moves it by dragging the pet (`start-dragging`). Its position
  persists through the existing window-state plugin, extended to the `pet`
  label with `POSITION` only. This is the path `desktop-window-state.md`
  reserves for new windows; no second persistence path is added.

### G8 lifecycle invariants

These are the multi-window invariants that G8 said must ship with the first
extra window:

- Only `main` bootstraps. `pet` is remote from its first frame. It never loads
  the bootstrap page and is granted no bootstrap capability; existing
  capability files stay keyed to `main`.
- `pet` exists only while a Runtime is ready. It is created when the Runtime
  becomes ready and the pet is enabled, and it is destroyed when the Runtime
  stops or the pet is disabled.
- Closing or destroying `pet` never stops the Runtime and never quits the app.
  Single-instance hand-off and deep links keep targeting `main`.
- Navigation in `pet` is confined to the Runtime origin and the `/pet` path,
  so the pet can never become a second Workbench. New-window requests are
  denied, as they are in `main`.
- The notification focus gate stays `main`-only, unchanged. Pet visibility does
  not suppress native notifications in v1.

### Shell ↔ pet IPC

The IPC surface is kept minimal. It is granted to window `pet` for the remote
loopback origin only, through a new capability file:

- event `pet:summon`, sent from the shell to the pet when the hotkey fires;
- command `pet_set_expanded(expanded: bool)`, to resize and anchor the window;
- command `pet_open(link)`, which takes an `avibe://session/<id>` or
  `avibe://vaults/request/<request_id>` link. It is parsed and validated by the existing
  deep-link parser (`desktop-deep-links.md`) and then focuses `main` through
  the same path a clicked deep link uses. The pet gets no new routing logic;
  anything the parser rejects is dropped;
- permission `start-dragging`.

Everything else the pet needs comes from the Runtime HTTP and SSE API, exactly
as it does for the Workbench.

### Global hotkey and off switch

- Add `tauri-plugin-global-shortcut`. This is a new dependency, recorded in
  `desktop/src-tauri/Cargo.toml` in the implementing PR. The shell registers
  the shortcut in Rust, so no JS plugin package is needed.
- **Default shortcut.** `⌃⌥Space` on macOS, `Ctrl+Alt+Space` on Windows. The
  common alternatives are taken: `⌥Space` is the Codex and ChatGPT summon key
  on macOS, and `Alt+Space` opens the window menu on Windows.
- **Rebinding.** v1 has a tray submenu of presets, plus a `shortcut` field in
  the pet preferences file. A recorder UI is a follow-up.
- **Registration failure.** If the OS refuses the shortcut, for example because
  another app holds it, the failure is shown in the tray submenu. It is never
  swallowed.
- **One wake function.** Every wake path (hotkey, tray item, any future one)
  calls `pet_wake()`. It checks the enabled flag first, then shows the pet and
  emits `pet:summon`.
- **Off switch.** A tray `CheckMenuItem` "Show pet", next to "Notifications".
  Off destroys the window and unregisters the shortcut, so nothing can wake it.
- **Preferences.** `pet.json` in `app_local_data_dir`, the same pattern as
  `notifications.json`: `{enabled, shortcut}`. Pet visibility defaults to off
  until the art ships, then on.

### Pet route (web UI)

`/pet` is a new route in the same SPA.

- It sits inside `AuthGuard` and the existing providers (Api, Inbox), but
  outside `AppShell`, so it has no sidebar or chrome.
- It reuses the provider's single `EventSource('/api/events')`. That is one
  connection per window. The server has no per-session filter, so the route
  filters on `session_id` on the client.

**Session binding.** The route reads the bound session id from `localStorage`
key `avibe.pet.sessionId` on the Runtime origin.

- The Workbench chat page gets a "Show in pet" action that writes the key.
- The `storage` event updates an open pet live.
- The panel also has a compact recent-session switcher.
- Both windows use the default WebKit/WebView2 data store for the same origin,
  so they share the key. This must be verified (see Risks).
- When the main-agent session type lands, the route resolves the main-agent
  session instead, and the switcher is removed.

### Pet state

One pure function, `derivePetState(inputs)`, unit-tested in Vitest. Each input
comes from an API the Workbench already uses:

| State | Condition | Source |
|---|---|---|
| Needs input | pending vault request for `S`, or the latest agent result has unanswered `quick_replies` | `GET /api/vault/requests?status=pending&session=S` (`usePendingVaultRequests`, refreshed on `vaults.updated` and expiry); message `content.quick_replies` and `quick_reply_chosen` |
| Blocked | `agent_status = failed` | `session.status`; bootstrap |
| Ready | foreground idle and `unread_count > 0` | `inbox.unread.changed` (`unread_by_session`) |
| Running | `turn_state.foreground = running` or `in_flight` | `turn.start`/`turn.end` with `GET /api/sessions/S/turn-state`, as the chat page does |
| Idle | none of the above | |

- **Priority.** Needs input > Blocked > Ready > Running > Idle, as in Codex.
- **Background work.** Delegated runs, watches, and tasks
  (`turn_state.background_activities`) do not change the state. The panel
  shows them as an activity strip, and the collapsed pet shows a count. The pet
  is "Running" only when the agent itself is working.
- **Reading.** Viewing a reply in the expanded panel calls the existing
  `POST /api/sessions/S/mark-read`, so Ready clears in the pet and in the
  Workbench together.

Tool approvals and `AskUserQuestion`-style waits do not exist today. Claude
runs with permissions bypassed and Codex auto-approves, so vault requests and
quick replies are the complete "needs input" set for now. A future backend that
asks the user must add its source to `derivePetState`.

### Panel

Expanded, top to bottom:

1. **Live transcript** while listening, then an editable input that is always
   available for typing.
2. **Latest exchange.** The last user message and the streaming agent reply,
   from `message.new`/`message.updated` for `S`, rendered with the existing
   message renderer in a compact variant.
3. **Activity strip** for `turn_state.background_activities`, reusing the chat
   page's strip in a compact variant.
4. **Needs input.** Pending vault requests and quick-reply buttons. Quick
   replies are answered in place, through the same path `QuickReplies` uses.
   Vault requests get "Open in Avibe" (`pet_open` with the request's vaults
   link), because approval UI stays in the Workbench in v1.

Sending uses the same `POST /api/sessions/S/messages` call as the composer. A
message sent while a turn is running is queued by the server, exactly as in the
Workbench. The pet shows "Queued" and offers no steering control in v1.

### Interaction

- **Hotkey tap with the pet collapsed or hidden.** Show the pet, expand it,
  focus it, and start listening. If voice is unavailable, focus the text input
  instead.
- **Hotkey tap while listening.** Stop and send the transcript.
- **Esc.** Cancel listening, or collapse the panel if not listening.
- **Click into the transcript while listening.** Stop listening and keep the
  text for editing; `Enter` sends.
- **Click the pet.** Toggle the panel.
- **Panel blur.** Collapse, unless listening or the input has unsent text.

### Voice

Reuse the composer's pipeline unchanged:

- `getUserMedia` feeding `VoiceRecordingPipeline`, with `claimVoiceCapture`
  for single ownership;
- `VoiceRealtimeSession` for the live preview (`preview.text + preview.stash`)
  and final text;
- the existing HTTP transcription fallback.

The capture and transcribe logic in `Composer.tsx` is extracted into a shared
hook used by both the composer and the pet. That is the second and third
consumer, together with Show Page dictation, so extraction is justified now.

Voice is available only when `GET /api/asr/status` reports `available`, which
needs Avibe Cloud. Otherwise the hotkey opens text input and the panel shows
a short hint that voice needs Avibe Cloud. That is a new i18n string; the
composer just hides its mic button.

The microphone usage string and audio-input entitlement already ship (#2293).

### Rendering and idle cost

- **v1 art.** One built-in sprite sheet with one animation row per state,
  animated with CSS `steps()`. No canvas, WebGL, or JS animation loop.
- **Pausing.** Animation pauses when the window is hidden or the document is
  not visible. Idle plays a slow loop that stops after a short time.
  `prefers-reduced-motion` shows still frames.
- **Design.** The art and the panel frames are designed in
  `../avibe-docs/design.pen` before the UI implementation. All copy goes
  through `ui/src/i18n/en.json` and `zh.json`.

## Delivery

1. **Shell and state.**
   - `pet` window and its lifecycle invariants;
   - IPC surface;
   - global shortcut, tray toggle and presets, `pet.json`;
   - `/pet` route with binding, `derivePetState`, collapsed and expanded panel;
   - text send, latest exchange, activity strip;
   - "Show in pet".
2. **Voice.** Shared dictation hook extracted from the composer; listening
   flow in the pet.
3. **Art.** Final sprite and design-matched panel. The pet is enabled by
   default.

## Tests

- **Rust** (`desktop/src-tauri/tests/shell_boundaries.rs`):
  - `pet` gets no bootstrap command;
  - pet capabilities grant only the listed command, event, and permission;
  - navigation outside `/pet` is denied;
  - destroying `pet` leaves the Runtime running and `main` intact;
  - disabling unregisters the shortcut, and every wake path is a no-op while
    disabled.
- **Vitest:**
  - `derivePetState` covers every row and the priority order;
  - session binding through `localStorage` and the `storage` event;
  - hotkey summon starts voice only when ASR is available.
- **Manual sanity on a signed macOS build and on Windows:**
  - transparency;
  - always on top over full-screen apps;
  - drag and position restore;
  - first-use mic prompt in the pet window;
  - shortcut conflict reporting.

## Risks to verify during implementation

- **`localStorage` sharing.** Confirm that `main` and `pet` share it on both
  WKWebView and WebView2. If not, move the binding to the Runtime.
- **Mic prompts.** WKWebView may prompt for the mic separately in the second
  webview. Confirm that the prompt appears once and is remembered.
- **Full-screen Spaces.** On macOS, appearing over full-screen apps may need
  the `FullScreenAuxiliary` collection behavior, beyond
  `visible_on_all_workspaces`.
- **Hidden windows.** Confirm that a hidden Tauri window reports the document
  as hidden, so animation and polling pause.
- **Focus.** Summoning focuses the pet so that typing works. Confirm that the
  previously active app gets focus back when the panel collapses.

## Follow-ups

- Bind to the main-agent session when the session type lands, and remove the
  picker.
- Custom pets: a sprite format and an importer.
- A hotkey recorder in Settings.
- Hold-to-talk using shortcut press and release states.
- Approvals inside the pet.

## Todo

- [x] Update G8 and G10 in `desktop-product-gaps.md` to point here.
- [ ] Pet window, lifecycle invariants, capability file, and IPC.
- [ ] Global shortcut, tray toggle and presets, `pet.json`.
- [ ] `/pet` route, session binding, "Show in pet".
- [ ] `derivePetState` with Vitest coverage.
- [ ] Panel: latest exchange, activity strip, needs input, text send.
- [ ] Shared dictation hook; pet listening flow.
- [ ] Sprite and panel design in `design.pen`; i18n strings.
- [ ] Rust boundary tests; manual checks on macOS and Windows.
- [ ] User docs: `desktop/README.md` pet section.
