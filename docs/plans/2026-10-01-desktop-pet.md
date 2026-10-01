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
   panel shows the running indicator until the agent's reply lands.
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
- **Two sizes.** Collapsed, the window is only as big as the pet image, so
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
  until voice ships, then on.

### Pet route (web UI)

`/pet` is a new route in the same SPA.

- It sits inside `AuthGuard` and the existing providers (Api, Inbox), but
  outside `AppShell`, so it has no sidebar or chrome.
- It reuses the provider's single `EventSource('/api/events')`. That is one
  connection per window. The server has no per-session filter, so the route
  filters on `session_id` on the client.

**Input freshness.** The SSE broker is an in-memory fan-out with no replay,
and some state changes publish no event at all. So every input the pet derives
from needs three things: an initial read, a live trigger, and a gap fallback.
The table is the contract. Its rules:

- Any trigger re-reads the authoritative API; events are hints, never state.
- The pet reads its own session `S` directly. It does not rely on a provider
  having `S` cached: a fresh pet window's providers only hold what their own
  bootstrap fetched, which is not the session `main` picked.
- A trigger counts only if it is published after the state it signals is
  persisted. Each row below was checked against the publishing code for that
  ordering.

| Input | Initial read | Live trigger | Gap fallback |
|---|---|---|---|
| Message tail (latest exchange, `quick_replies`) | `listSessionMessages(S, {tail: true, cache: false})` | `message.new` for `S` (merged, deduped by id) | `onConnected`; window becomes visible |
| `quick_reply_chosen` | in the tail rows | `message.updated` for `S`, replacing the row by id, as the chat page already does. The Runtime starts publishing it for this field (see note) | as above |
| Turn state (`foreground`, `in_flight`, `background_activities`) | `GET /api/sessions/S/turn-state` | `turn.start`, `turn.end`, `queue.updated` for `S`; `runs.updated` for `S` or with no `session_id` | `onConnected`; visible; the chat page's interval reconcile while work is present and the window is visible |
| Pending vault requests | `usePendingVaultRequests` | `vaults.updated` (`useVaultRequestRefresh`) | expiry timer; `onConnected` |
| `agent_status` | `getSession(S, {cache: false})` | `session.status` for `S`, applied to the pet's own copy | `onConnected`; visible: re-read `getSession(S)` |
| Unread count | Inbox provider `unread_by_session`, which its bootstrap reads for every session | `inbox.unread.changed` | provider's own `onConnected` |

Two notes on the table:

- **Quick replies.** Today the server records `quick_reply_chosen` on the
  agent row only after the dispatch returns, and publishes no row update. The
  user message's `message.new` and `queue.updated` are emitted during dispatch
  (`_publish_materialized_delivery`), so they can arrive before the choice is
  persisted, and a re-read on them can race. The fix is at the Runtime layer:
  when `set_quick_reply_chosen` newly records a choice, the messages endpoint
  publishes `message.updated` with the updated agent row after the write
  commits. Every window then clears Needs input from one ordered event, and the
  Workbench gains the same cross-tab lock for free. The window that clicked
  also locks optimistically, as `QuickReplies` does today. This is a small
  server change in the shell-and-state PR, with a pytest that the event follows
  the write.
- **Turn state.** The chat page already owns the subtle part: a grace window
  after a local send, and an interval reconcile (60 s while working, 10 s while
  background work is present) that recovers a dropped `turn.end` without ever
  clearing a live turn on a timer. That logic is extracted from `ChatPage.tsx`
  into a shared `useSessionTurnState(S)` hook used by both, rather than
  re-implemented. The pet adds the `runs.updated` trigger, so a delegated run
  that starts or finishes while the pet is visible updates the activity strip
  at once. The interval runs only while work is present and the window is
  visible, so an idle pet does no network work.

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
| Blocked | `agent_status = failed` | `getSession(S)` and `session.status` (see Input freshness) |
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
2. **Latest exchange.** The last user message and the last agent result, from
   the durable tail plus `message.new` for `S`, rendered with the existing
   message renderer in a compact variant. Replies are not streamed: the
   Runtime persists an agent reply atomically as one result row, and there is
   no reply-delta event. While the turn runs, the panel shows the running
   indicator and the activity strip; the reply appears when its row lands.
   Token streaming would be a new Runtime transport and is a follow-up.
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
  for single ownership (extended across windows, below);
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

**Cross-window mic ownership.** `claimVoiceCapture` keeps its owner in a
module variable, so it arbitrates only inside one JavaScript realm. `main` and
`pet` are separate realms: a hotkey summon while the composer is dictating
would start a second recorder. The claim is extended across same-origin realms
with a `BroadcastChannel` (`avibe.voice-capture`):

- Claiming posts a message with a per-realm id and token.
- A realm that receives a claim from another realm finishes its local owner
  through the same `finish()` callback a local replacement uses. Captured
  speech is preserved, exactly as today.
- Local semantics (`isCurrent`, `release`) are unchanged, and a realm without
  `BroadcastChannel` falls back to local-only ownership.

This lives in `claimVoiceCapture` itself, so every caller (composer, Show Page
dictation, pet) inherits it, and two Workbench browser tabs gain the same
guarantee. The shell is not involved, so the pet capability file stays as
listed. Delivery between the two webviews depends on the same shared data
store as the `localStorage` binding and is verified with it (see Risks).

The microphone usage string and audio-input entitlement already ship (#2293).

### Rendering and idle cost

- **v1 art is the existing mascot, Vibey (云团子).** `assets/mascot/` already
  holds one pose per state, each a 1024 px PNG with a transparent background,
  so no new art is drawn:

  | Pet state | Pose |
  |---|---|
  | Idle | `cloud-tuanzi.png`; `cloud-tuanzi-sleep.png` after a few idle minutes |
  | Listening | `cloud-tuanzi-focus.png` |
  | Running | `cloud-tuanzi-thinking.png` |
  | Ready | `cloud-tuanzi-celebrate.png`, with the unread badge |
  | Needs input | `cloud-tuanzi-message.png` |
  | Blocked | `cloud-tuanzi-error.png` |

  The UI bundles downscaled copies (2x the collapsed display size) under
  `ui/src/assets/pet/`; the 1024 px sources stay the masters.
- **Motion.** One pose per state, brought to life with CSS only: a slow
  breathing float, and a short cross-fade with a small squash on a state
  change. No sprite sheet, canvas, WebGL, or JS animation loop. A frame-based
  sprite sheet is a later upgrade if the stills feel flat.
- **Pausing.** Animation pauses when the window is hidden or the document is
  not visible. Idle breathing stops after a short time.
  `prefers-reduced-motion` shows the still pose and swaps it without motion.
- **Design.** The collapsed pet and the panel frames are designed in
  `../avibe-docs/design.pen` with the existing tokens before the UI
  implementation. All copy goes through `ui/src/i18n/en.json` and `zh.json`.

## Delivery

1. **Shell and state.**
   - Runtime: publish `message.updated` when a quick-reply choice is recorded;
   - `pet` window and its lifecycle invariants;
   - IPC surface;
   - global shortcut, tray toggle and presets, `pet.json`;
   - `/pet` route with binding, `derivePetState`, collapsed and expanded panel;
   - text send, latest exchange, activity strip;
   - "Show in pet";
   - Vibey poses and CSS motion, and panel frames in `design.pen`, so the
     first PR already looks finished.
2. **Voice.** Shared dictation hook extracted from the composer; listening
   flow in the pet. The pet is enabled by default from this PR on.

## Tests

- **Pytest:** recording a quick-reply choice publishes `message.updated` for
  the agent row after the write commits, and only when the choice is newly
  recorded.
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
  - each row of the input-freshness table: the initial read on bind, each
    live trigger, and each gap fallback re-read the authoritative API, and
    live rows merge without duplicates;
  - a `message.updated` row carrying `quick_reply_chosen` clears Needs input;
  - a freshly bound session that is already failed derives Blocked from
    `getSession(S)`, with no provider row present;
  - `runs.updated` for `S`, or without `session_id`, refreshes the activity
    strip;
  - the extracted `useSessionTurnState` keeps the chat page's existing
    turn-state tests passing;
  - a voice claim from another realm (simulated `BroadcastChannel` message)
    finishes the local owner, and a realm's own claim does not;
  - hotkey summon starts voice only when ASR is available.
- **Manual sanity on a signed macOS build and on Windows:**
  - transparency;
  - always on top over full-screen apps;
  - drag and position restore;
  - first-use mic prompt in the pet window;
  - starting dictation in the pet while the composer dictates stops the
    composer's recorder;
  - shortcut conflict reporting.

## Risks to verify during implementation

- **Shared origin storage.** Confirm that `main` and `pet` share
  `localStorage` and `BroadcastChannel` delivery on both WKWebView and
  WebView2. If not, move the binding to the Runtime, and relay voice claims
  through a shell event to every window.
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
- Custom pets: an image or sprite format and an importer.
- Frame-based sprite animation for Vibey.
- A hotkey recorder in Settings.
- Hold-to-talk using shortcut press and release states.
- Streaming agent replies, once the Runtime has a reply-delta transport.
- Approvals inside the pet.

## Todo

- [x] Update G8 and G10 in `desktop-product-gaps.md` to point here.
- [ ] Pet window, lifecycle invariants, capability file, and IPC.
- [ ] Global shortcut, tray toggle and presets, `pet.json`.
- [ ] `/pet` route, session binding, "Show in pet".
- [ ] `derivePetState` with Vitest coverage.
- [ ] Panel: latest exchange, activity strip, needs input, text send.
- [ ] Input-freshness table for the pet route; extract `useSessionTurnState` from the chat page.
- [ ] Shared dictation hook; cross-window voice claim; pet listening flow.
- [ ] Vibey pose assets, CSS motion, and panel design in `design.pen`; i18n strings.
- [ ] Rust boundary tests; manual checks on macOS and Windows.
- [ ] User docs: `desktop/README.md` pet section.
