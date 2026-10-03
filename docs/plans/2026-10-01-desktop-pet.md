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
- The user moves it by dragging the pet (`start-dragging`).
- **Position is the pet anchor, not the window frame.** The window's origin
  moves whenever the panel opens to the left or above the pet. A raw window
  position saved while expanded would restore the collapsed pet displaced by
  the panel's size. So the persisted value is the pet anchor: the pet image's
  top-left in logical screen coordinates, plus its monitor.
  - The shell owns it, because the shell lays out both sizes and knows the
    pet's offset inside the window. It is recomputed from the window origin
    and that offset when a drag ends, whether collapsed or expanded.
  - It is stored as `anchor` in `pet.json`. On restore it is clamped into a
    connected monitor's work area.
  - The window-state plugin stays filtered to `main`. Its per-label frame
    (`desktop-window-state.md`) fits windows whose frame is the user's
    choice; the pet's frame is derived from the anchor, so the anchor is what
    is persisted. `pet.json` already exists for pet preferences, so this adds
    no new store.

### G8 lifecycle invariants

These are the multi-window invariants that G8 said must ship with the first
extra window:

- Only `main` bootstraps. `pet` is remote from its first frame. It never loads
  the bootstrap page and is granted no bootstrap capability; existing
  capability files stay keyed to `main`.
- `pet` exists exactly when the pet is enabled and a Runtime is ready. One
  shell function, `pet_reconcile()`, owns that invariant: it creates the
  window if it should exist and is missing, and destroys it if it should not
  exist. It runs on Runtime ready and stop, on a preference change, and at the
  start of every wake. No other path creates or destroys `pet`. A Runtime
  origin change (a changed `ui.setup_port`) returns the shell to bootstrap,
  so the pet is destroyed and then recreated at the new origin by the same
  function.
- An OS close request on `pet` (for example `Alt+F4` on Windows) is prevented
  and hides the pet, as `main` already does for itself. Turning the pet off
  goes through the tray switch, not the window's close. If the window is lost
  anyway, the next wake recreates it through `pet_reconcile()`.
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

- command `pet_ready() -> {binding, summon_pending: null | {intent}}`, which
  the route calls once `/pet` has mounted and its listeners are installed. It
  returns the current binding and consumes any summon that arrived before the
  page was ready, so a summon that recreated the window is never lost;
- event `pet:summon {intent: 'listen' | 'show'}`, sent from the shell to the
  pet when a wake happens and the page is ready. Before `pet_ready()`, the
  shell records the summon as pending instead of emitting it, because Tauri
  events have no replay; a later summon replaces a pending one;
- command `pet_set_expanded(expanded: bool) -> PetLayout`, to resize and
  anchor the window. The shell is the only owner of placement, because only
  it knows the monitor work area and the anchor. It returns the layout it
  chose, `{panel_side: left | right, panel_edge: top | bottom, pet_offset}`,
  and the route renders the pet and panel from that value. The native frame
  and the DOM therefore share one placement decision;
- command `pet_bind(session_id) -> {shown}` and event `pet:bound`, which set
  and carry the session binding (see Session binding). The pet uses
  `pet_bind` for its switcher; a call from `main` also wakes the pet;
- command `pet_unbind(session_id)`, a compare-and-clear: it clears the binding
  only if it is still `session_id`, so a late result about an old session can
  never clear a newer one;
- command `pet_open(link)`, which takes any link the existing deep-link
  parser (`desktop-deep-links.md`) accepts: session, Show Page, settings, and
  vault request. It focuses `main` through the same path a clicked deep link
  uses. The pet gets no new routing logic; anything the parser rejects is
  dropped;
- permission `start-dragging`.

`main` gets one command, `pet_bind`, through its own new capability file for
the remote loopback origin. It is the "Show in pet" action. No other `main`
capability changes.

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
- **One wake function.** Every wake path (hotkey, tray item, "Show in pet",
  any future one) calls `pet_wake(intent)`. It checks the enabled flag first,
  runs `pet_reconcile()` so a missing window is recreated, then shows the pet
  and delivers the summon with its intent: emitted at once if the page is
  ready, otherwise recorded as pending for `pet_ready()`.
  - `listen` comes only from the hotkey, the one explicit request to talk.
  - `show` comes from every display or navigation path (tray item,
    "Show in pet"). It shows and expands the pet on the bound session and
    never starts capture.
- **Off switch.** A tray `CheckMenuItem` "Show pet", next to "Notifications".
  Off destroys the window and unregisters the shortcut, so nothing can wake it.
- **Preferences.** `pet.json` in `app_local_data_dir`, the same pattern as
  `notifications.json`: `{version: 1, enabled?, shortcut?, anchor?, binding?}`.
  All durable pet state lives here.
  - `enabled` is written only by the tray switch. An absent `enabled` means
    "use this build's default", so writes of the anchor or binding never
    freeze the default.
  - The default is off in the shell-and-state build and on from the voice
    build. A user who never touched the switch therefore gets the pet when
    voice ships, and a user who turned it off stays off.
  - Only an absent file gets the build default. A file that is present but
    unreadable, or has an unknown `version` (for example after a downgrade),
    fails closed: the pet is disabled for this run with a warning, and the
    shell does not rewrite the file, so a newer build's state and a user's
    opt-out both survive. Toggling the tray switch is the explicit recovery: it
    writes a fresh version-1 file. The app never fails to start because of
    `pet.json`.
  - Load fixtures cover an absent file (build default), a file without
    `enabled` (build default), an explicit `false`, an unreadable file
    (disabled, file untouched), and an unknown version (disabled, file
    untouched).

### Pet route (web UI)

`/pet` is a new route in the same SPA.

- It sits inside `AuthGuard` and the existing providers (Api, Inbox), but
  outside `AppShell`, so it has no sidebar or chrome.
- `/pet` is exempt from `AuthGuard`'s setup redirect. Before setup is
  complete, it renders its own setup-pending state ("Finish setting up in
  Avibe", which calls `pet_open("avibe://settings")`; `main`'s own guard then
  shows setup) and stays on `/pet`. So a default-on pet on a
  fresh install never becomes a pet-sized setup wizard.
- The pet leaves setup-pending by re-reading the authoritative setup check
  (the same `getConfig` result `AuthGuard` uses), not by waiting for a
  notification: finishing the wizard in `main` gives the pet no route change
  and no cross-window event. While setup-pending, the pet re-reads on every
  `pet:summon`, on window focus, and on becoming visible. Every way the user
  returns to the pet (hotkey, tray, clicking it) is one of these, so the
  first interaction after setup finds the completed setup and mounts the
  normal pet, with no restart.
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
- A server-derived projection is not filtered by session on the client. When
  an event's payload cannot say whether it touches `S`, the pet re-reads on
  every event of that family, coalesced to one in-flight read plus one
  trailing read. `AgentsPage` and `AgentGraphTab` already refresh this way on
  `runs.updated`.

| Input | Initial read | Live trigger | Gap fallback |
|---|---|---|---|
| Message tail (latest exchange, `quick_replies`) | `listSessionMessages(S, {tail: true, cache: false})` | `message.new` for `S` (merged, deduped by id) | `onConnected`; window becomes visible |
| `quick_reply_chosen` | in the tail rows | `message.updated` for `S`, replacing the row by id, as the chat page already does. The Runtime starts publishing it for this field (see note) | as above |
| Turn state (`foreground`, `in_flight`, `background_activities`) | `GET /api/sessions/S/turn-state` | `turn.start`, `turn.end`, `queue.updated` for `S`; every `runs.updated` and every `definitions.updated`, unfiltered and coalesced | `onConnected`; visible; the chat page's interval reconcile while work is present and the window is visible |
| Pending vault requests | `usePendingVaultRequests` | `vaults.updated` (`useVaultRequestRefresh`) | expiry timer; `onConnected` |
| Session row (`status`, `agent_status`) | `getSession(S, {cache: false})` | `session.status` for `S`, applied to the pet's own copy; any `session.activity` for `S` re-reads the row | `onConnected`; visible: re-read `getSession(S)` |
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
  re-implemented. The pet adds two unfiltered triggers. `background_activities`
  is a projection built from two sources: watch and task definitions bound to
  `S`, and delegated runs whose `callback_session_id` is `S`. Neither event
  names that ownership. `definitions.updated` is instance-scoped, and
  `runs.updated` carries the executor's `session_id`, which for a delegated
  run is by design not `S`. So the pet re-reads turn-state on every event of
  both families, and a first delegated run, watch, or task appears in the
  strip at once. The interval runs only while work is present and the window is
  visible, so an idle pet does no network work.

**Session binding.** The binding is durable pet state, so it lives where the
rest of the pet's durable state lives: `binding` in the shell-owned
`pet.json`. Browser storage is scoped to the Runtime origin, which changes
when the user changes the UI port, so it cannot own anything durable.

- The Workbench chat page's "Show in pet" action calls `pet_bind(S)`. It is
  offered only where the chat page's composer is: it is hidden whenever
  `isSessionReadOnly(session)` (`sessionArchived.ts`) is true.
- A bind from `main` also shows the pet: after persisting, the shell runs
  `pet_wake('show')`, the same single wake path as the hotkey with a
  display-only intent, so a pet hidden by an OS close reappears without
  opening the microphone. `pet_bind` returns `{shown}`; when the pet is off,
  the binding is still saved and `main` shows a toast pointing to the tray's
  "Show pet" switch. A bind from the pet's own switcher does not wake, since
  the pet is already visible.
- The panel's compact recent-session switcher calls `pet_bind` the same way.
  Its rows come from the global `listSessions({status: 'active', limit: 20})`
  (`GET /api/sessions` with no project, newest first), filtered by the same
  `isSessionReadOnly` predicate. It is read when the switcher opens, and
  re-read on `session.activity` and `onConnected` while it is open, so a new
  session with no reply yet is listed.
- The shell writes `pet.json` and emits `pet:bound` to the pet. On load, the
  pet reads the binding from `pet_ready()`.
- **A binding is valid only while `S` is a writable session**, by the chat
  page's own predicate: `getSession(S)` succeeds and
  `isSessionReadOnly(session)` is false. That rejects archived sessions and
  Runtime-owned system sessions such as the workspace-notices session, whose
  messages endpoint always refuses a send. Every read of `getSession(S)`
  checks this, on bind, on gaps, and on any `session.activity` for `S`
  (archive, move, and the rest are all published there). When the read
  returns a read-only session or `404`, the route calls `pet_unbind(S)` and
  shows the empty state with the switcher, so the pet never accepts input it
  cannot send. The rule is checked on the read, not tied to a particular
  event kind.
- **Every asynchronous read is fenced by binding and by order.** Each source
  (session, tail, turn state, vault requests, and the switcher's session
  list) keeps a generation counter.
  Starting a read, and merging a live event into that source, both bump it. A
  read carries the session id and the generation it started at, and its
  result is applied only if the session is still the binding and no newer
  read or live merge has happened on that source since; otherwise it is
  dropped. A live merge that invalidates an in-flight read schedules one
  trailing read, so the source still converges on the server's state.
  - Across bindings, a switch from `A` to `B` never shows `A`'s state, and a
    late `404` for `A` cannot clear `B`: the route drops it, and
    `pet_unbind(A)` would be a no-op in the shell anyway.
  - Within one binding, a tail read started before a quick-reply
    `message.updated` cannot land after it and restore Needs input.
  - The switcher list is not tied to a binding, so it is fenced by
    generation only: an older `listSessions` response cannot overwrite a
    newer refresh that already lists a new session.
- When the main-agent session type lands, the route resolves the main-agent
  session instead, and the switcher is removed.

### Pet state

One pure function, `derivePetState(inputs)`, unit-tested in Vitest. Each input
comes from an API the Workbench already uses:

| State | Condition | Source |
|---|---|---|
| Needs input | pending vault request for `S`, or the latest agent result has unanswered `quick_replies` (see note below) | `GET /api/vault/requests?status=pending&session=S` (`usePendingVaultRequests`, refreshed on `vaults.updated` and expiry); message `content.quick_replies` and `quick_reply_chosen` |
| Blocked | `agent_status = failed` | `getSession(S)` and `session.status` (see Input freshness) |
| Ready | foreground idle and `unread_count > 0` | `inbox.unread.changed` (`unread_by_session`) |
| Running | `turn_state.foreground = running` or `in_flight` | `turn.start`/`turn.end` with `GET /api/sessions/S/turn-state`, as the chat page does |
| Idle | none of the above | |

- **Priority.** Needs input > Blocked > Ready > Running > Idle, as in Codex.
- **Background work.** Delegated runs, watches, and tasks
  (`turn_state.background_activities`) do not change the state. The panel
  shows them as an activity strip, and the collapsed pet shows a count. The pet
  is "Running" only when the agent itself is working.
- **Reading.** The pet marks read only what it rendered. The expanded panel
  renders every unread agent result in the loaded tail, oldest first, in a
  scrollable list, rather than only the latest one.
  - It then calls the existing `POST /api/sessions/S/mark-read` with
    `until_message_id` set to the last row it rendered
    (`markSessionRead(S, untilMessageId)`). Ready clears in the pet and in the
    Workbench together.
  - A result that lands after rendering, for example from a queued turn,
    stays unread and keeps the badge.
  - If the unread rows reach past the loaded tail, the panel does not mark
    read at all. It shows "More in Avibe" (`pet_open` with the session link)
    and leaves reading to the Workbench.

**Only the latest agent result's quick replies mean Needs input.** This is a
deliberate difference from the Workbench, which keeps every unanswered group
clickable regardless of age (`workbench-quick-replies.md`). Clickability is
about not blocking a user who wants an earlier option; the pet's state is
about what the agent is waiting for now. Once a newer agent result exists, the
agent has moved on, often because the user answered in free text without
pressing a button. An older unanswered group is therefore not an open
question, and treating it as one would hold the pet in Needs input
indefinitely with no action that clears it. Older groups stay clickable in the
Workbench, reached with "Open in Avibe". This also keeps the state computable
from the bounded tail, with no history scan.

Tool approvals and `AskUserQuestion`-style waits do not exist today. Claude
runs with permissions bypassed and Codex auto-approves, so vault requests and
quick replies are the complete "needs input" set for now. A future backend that
asks the user must add its source to `derivePetState`.

### Panel

Expanded, top to bottom:

1. **Live transcript** while listening, then an editable input that is always
   available for typing.
2. **Latest exchange.** The last user message and the unread agent results (or
   the last one, when none are unread), from
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

- **Hotkey tap with the pet collapsed or hidden** (`listen` intent). Show the
  pet, expand it, focus it, and start listening. If voice is unavailable, focus the text input
  instead.
- **No valid binding** (none chosen yet, or cleared after an archive). The
  hotkey opens the panel on the session switcher and does not start listening
  or accept input, so nothing is ever captured without a destination.
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
with the Web Locks API, which already gives one total order across them, so no
home-made protocol has to resolve simultaneous claims:

- Claiming requests the lock `avibe.voice-capture` with `steal: true` and
  holds it for the life of the capture.
- The lock manager grants steals in order, so the newest claim always holds
  the lock, even when two realms claim within the same instant.
- A realm whose lock is stolen sees its request reject with `AbortError`. It
  finishes its local owner through the same `finish()` callback a local
  replacement uses, so captured speech is preserved, exactly as today.
- `release` releases the lock. Local semantics (`isCurrent`, `release`) are
  unchanged.
- **The pet never captures without the cross-window guarantee.** If
  `navigator.locks` is missing in the pet's webview, pet voice is turned off:
  the hotkey still summons the pet and focuses the text input, and the mic
  control is hidden. A browser tab keeps today's local-only behavior, since
  there is no second desktop realm there. Single ownership across `main` and
  `pet` is therefore never silently dropped.

This lives in `claimVoiceCapture` itself, so every caller (composer, Show Page
dictation, pet) inherits it, and two Workbench browser tabs gain the same
guarantee. The shell is not involved, so the pet capability file stays as
listed. Lock sharing between the two webviews depends on their sharing one
origin data store, which is verified during implementation (see Risks).

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
    disabled;
  - an OS close request on `pet` hides it, and a wake after the window is
    lost recreates it through `pet_reconcile()`;
  - the saved anchor is the pet image position whichever side the panel
    opened on, and a restore onto a missing monitor is clamped on screen;
  - `pet_set_expanded` returns the layout it applied, near each screen edge;
  - `pet_bind` persists to `pet.json` and emits `pet:bound`, and the binding
    survives a Runtime origin change;
  - `main` gains only `pet_bind`, and `pet` is granted exactly `pet_ready`,
    `pet_set_expanded`, `pet_bind`, `pet_unbind`, `pet_open`, the two events,
    and `start-dragging`; `pet_unbind(A)` is a no-op when the binding is `B`;
  - a wake that recreates the window delivers the summon through
    `pet_ready()`, and a summon to a ready page is emitted once;
  - the hotkey wakes with `listen`, and the tray item and "Show in pet" wake
    with `show`; a pending summon keeps its intent through `pet_ready()`;
  - `pet.json` load fixtures: absent file, no `enabled`, explicit `false`,
    unreadable file; an absent `enabled` follows the build default and an
    explicit `false` survives the default change.
- **Vitest:**
  - `derivePetState` covers every row and the priority order;
  - session binding through `pet_ready()` and `pet:bound`; the switcher goes
    through `pet_bind` and an invalid binding through `pet_unbind`; switching
    from `A` to `B` while `A`'s reads are in flight, with `A`'s results
    (including a `404`) arriving after the switch, leaves `B` bound and shows
    only `B`'s state;
  - a tail read started before a `message.updated` and resolved after it is
    dropped, the chosen row stays, and one trailing read follows;
  - "Show in pet" on a hidden, enabled pet binds and shows it, and on a
    disabled pet saves the binding and shows the tray toast;
  - an older switcher `listSessions` response that resolves after a newer
    refresh is dropped;
  - "Show in pet" is hidden for archived and system sessions, a persisted
    binding to the workspace-notices session is cleared on load, and the
    switcher lists a new active session that has no reply yet and omits
    read-only ones;
  - an older unanswered quick-reply group under a newer agent result does not
    derive Needs input; the hotkey with
    no valid binding opens the switcher and never starts capture;
  - before setup is complete, `/pet` renders its setup-pending state and is
    not redirected to `/setup`;
  - with setup completed in another window, the next summon, focus, or
    visibility change re-reads setup and leaves setup-pending;
- **Setup scenario:** the shell-and-state PR adds a scenario to
  `tests/scenarios/auth_setup/catalog.yaml` with a closed-loop case in
  `test_auth_setup_scenarios.py`: on a fresh install the pet is
  setup-pending, setup is completed through the wizard, and the next summon
  shows the normal pet without a restart. The scenario ID goes in that PR's
  description;
  - the route renders the pet and panel from each `PetLayout` orientation;
  - with several unread results, all are rendered and mark-read passes the
    last rendered row; a result that arrives after rendering stays unread;
    unread rows past the loaded tail are not marked;
  - each row of the input-freshness table: the initial read on bind, each
    live trigger, and each gap fallback re-read the authoritative API, and
    live rows merge without duplicates;
  - a `message.updated` row carrying `quick_reply_chosen` clears Needs input;
  - a freshly bound session that is already failed derives Blocked from
    `getSession(S)`, with no provider row present;
  - any `runs.updated`, including one whose `session_id` is the executor and
    not `S`, and any `definitions.updated` re-read turn-state, and a burst
    coalesces to at most one in-flight and one trailing read;
  - the extracted `useSessionTurnState` keeps the chat page's existing
    turn-state tests passing;
  - a stolen voice lock finishes the local owner and preserves its speech;
  - without `navigator.locks`, the pet hides the mic and the hotkey focuses
    text input instead of starting capture;
    two claims made at the same time leave exactly one owner, the later
    one; `release` frees the lock;
  - an archived or missing bound session clears the binding, from the
    initial read and after `session.activity`;
  - hotkey summon starts voice only when ASR is available;
  - a `show` summon, including from "Show in pet", never starts capture.
- **Manual sanity on a signed macOS build and on Windows:**
  - transparency;
  - always on top over full-screen apps;
  - drag and position restore: drag while expanded with the panel flipped
    left or up, quit, relaunch, and the collapsed pet is where it was seen;
  - first-use mic prompt in the pet window;
  - starting dictation in the pet while the composer dictates stops the
    composer's recorder;
  - shortcut conflict reporting.

## Risks to verify during implementation

- **Shared Web Locks.** Confirm that `main` and `pet` share one origin data
  store, and therefore Web Locks, on both WKWebView and WebView2. Until this
  is confirmed for a platform, pet voice stays off there (the gate above). If
  a platform does not share them, the shell arbitrates voice claims in the
  order it receives them before pet voice is enabled there.
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
