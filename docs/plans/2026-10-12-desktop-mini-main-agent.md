# Desktop mini (main agent) — interaction and implementation spec

Status: ready for implementation, v2 (2026-10-12). Supersedes v1 of this file.

- **Reference prototype:** Show Page "会话卡片" tab,
  <https://max-app.avibe.bot/show/sesexk2xpr89k/>. The source is
  `src/pages/index.tsx` and `src/styles.css` in the Show Page workspace
  `/Users/max/.avibe/show/sesexk2xpr89k/`. Every size, colour, duration and
  curve below was read from that source.
- **Precedence:** where this spec and the prototype disagree, this spec wins.
  §12 lists the prototype-only parts.
- **Owner decisions:** every behaviour here was decided with the owner in the
  design session. Do not "simplify" a detail away. If a detail cannot be built
  as written, stop and ask.
- **Existing design:** `docs/plans/2026-10-01-desktop-pet.md` (#2384, #2401,
  #2408) stays the base for binding, IPC, the shortcut, `pet.json`, poses and
  mark-read. This spec changes the layout and interaction on top of it.

## 1. What changes, in one walk-through

1. Vibey (歪比) sits at the bottom of the mini. Above it is a **collapsed
   stack**. The front card is the **main agent card**: the user's last message
   to the main agent, and below it what the agent is doing ("Thinking").
   Thin layers under it show that more cards exist.
2. The user clicks Vibey or the stack. The stack **expands upward** into a
   fixed-height, scrollable list. Under the main agent card are the main
   agent's **sub-task cards**: delegated runs, watches and scheduled tasks.
   Vibey does not move.
3. The user **presses and holds Vibey** and speaks. The cards fade aside. A
   one-line **marquee** above Vibey's head shows the live transcript. The
   user releases.
4. The marquee becomes an **input bubble** in the same place, prefilled. The
   user edits and presses Enter. The main agent card now shows that message,
   and its subtitle follows the turn: Thinking → Ran command → "已回复：…".
   If the agent hands work off, a new sub-task card appears below.
5. A sub-task card has a **stop** button. The first press asks "停止？", the
   second stops it. A card's **×** removes it from the stack.

The mini talks **only to the main agent**. To change a sub-task, the user tells
the main agent; there is no Follow up on cards.

## 2. Terms

| Term | Meaning |
|---|---|
| Main agent | The user's built-in `main` session (§10). |
| Main agent card | The pinned first card; it represents the main agent's current or last turn. |
| Sub-task card | One in-flight item of the main agent: a delegated run, a watch or a scheduled task. |
| Collapsed stack | The front card plus one layer per remaining card. |
| Expanded list | Every card at full size, in a fixed-height scroll box. |
| Speech slot | The single place between the cards and Vibey's head that holds the marquee or the input bubble. |
| Dock | The small two-button pill under Vibey's feet. |

## 3. Layout

### 3.1 Column and anchoring

- **Vibey is anchored.** Nothing in this spec moves Vibey on screen:
  expanding, collapsing, the speech slot, dismissing or stopping a card.
- Above Vibey is one **column**: the card stack, then the speech slot. Its
  bottom sits on the top edge of Vibey's image box and overlaps it by 4px
  (the art has its own transparent margin). The column is centred on Vibey.
- The column grows **upward**. It is a floating layer above everything else
  in the window.
- **Column width:** `min(440px, 94vw)`. Every card, the marquee and the input
  bubble are **the same width**: the column minus 18px on each side.
  - Expanded: 6px panel padding plus 12px scroll padding.
  - Collapsed: 18px stack padding.
- A card's width never depends on its content or on the number of cards.
- **Long text never widens anything.** Every flex/grid item on the path from
  the list to a text node needs `min-width: 0`, and grid rows need
  `grid-template-columns: minmax(0, 1fr)`. (A grid column sized by a long
  nowrap title was the bug we hit.)

### 3.2 Expanded box

- **No backing panel on the desktop (owner decision, 2026-10-12).** The
  expanded list sits in a transparent wrapper with 6px padding (kept so the
  widths in §3.1 hold). Cards float directly over the desktop, separated by
  their own border and shadow. The gaps between cards show the wallpaper and
  pass clicks through (§9.2). The prototype keeps a page-coloured plate only
  to hide the web page's own text behind it (§12).
- **Scroll box:** **fixed height** of about 3.5 cards (prototype **292px**),
  horizontal padding 12px. The half card at the bottom signals "more".
  - Hidden scrollbar: `scrollbar-width: none`, plus `::-webkit-scrollbar
    { display: none }`.
- **List padding:** top 10px, so the first card's × is never clipped; bottom
  0.
- **Row spacing:** 8px after each card, and **16px after the main agent
  card**, so it reads as its own group. Spacing is row bottom padding, never a
  flex `gap` (§7.4).
- **Bottom alignment:** cards are bottom-aligned (next to Vibey). Put the list
  in a column flex box and give it `margin-top: auto`; never use
  `justify-content: flex-end`, which makes overflow unreachable.
- **Scroll edge fades:** §7.5.

### 3.3 Collapsed stack

- **Front card:** the **first** card (the main agent card when it exists),
  showing its title and subtitle only: no ×, no stop button.
- **Layers:** **one per remaining card**, behind the front card (6 cards →
  front card + 5 layers). For layer `i` (0-based, nearest first):
  - **the same height as the front card**, offset `(i + 1) × 9px` below it,
    so its bottom edge peeks out `(i + 1) × 9px` under the front card's
    bottom whatever the front card's height. Do not use a fixed layer height:
    a fixed 52px is hidden entirely behind a taller front card (the main card
    with its eyebrow);
  - horizontal inset `18 + 6 + 3i` px from each column edge;
  - radius 24px, background `#1a1a1e`, border `white / 8%`;
  - **the same opacity as the front card** (no per-depth fade);
  - nearer layers stack above farther ones.
- The stack's height is the front card plus `layers × 9px`.
- The whole stack is one button: a click expands it.

## 4. Cards

### 4.1 Common anatomy

| Part | Spec |
|---|---|
| Container | Radius 28px; padding 20px × 14px; background `#2a2a2e`; border 1px `white / 8%`; shadow `0 18px 50px rgba(0,0,0,.45)`. |
| Title | 15px, semibold, tight tracking, white, one line, ellipsis. The full text is the tooltip. |
| Subtitle | 13px, `white / 45%`, 2px below the title, one line, ellipsis, with the full text as the tooltip. While running it has a 6px pulsing dot (`emerald-400`) before the text. |
| × (dismiss) | Expanded list only, never on the main agent card. 20px circle at the top-left corner, offset −6px/−6px. Background `#3a3a3e`, glyph 11px `white / 70%`; on hover `#4a4a50` with a white glyph. Sits above the card content. |
| Stop | Expanded list only, running sub-tasks only (§6). A 32px circle with background `white / 10%` and a filled rounded-square SVG icon at `white / 80%`. Its right gap and top gap to the card edge are both 16px. |

- There is **no reply/return button** and **no Follow up** (owner decisions).
- **Icons are SVG**, never Unicode glyphs. iOS renders `↩` as a blue emoji.

### 4.2 Main agent card

- **Position:** always the first card, collapsed or expanded. It cannot be
  dismissed and has no stop button in this version.
- **Eyebrow:** "MAIN AGENT" above the title, 2px gap.
  - 10px, semibold, uppercase, tracking 0.14em.
  - Light green at 80%: map to the theme's mint token; the prototype used
    `emerald-300`.
  - It names the card type, not the speaker.
- **Title:** the user's last message to the main agent. No avatar or icon:
  these are the user's own words, so nothing may suggest Vibey said them.
- **Subtitle:**
  - while the turn runs, the current step ("Thinking", "Reading file", "Ran
    command", "Delegating", …) with the running dot;
  - after the turn, the start of the reply, "已回复：…", with no dot.
- **Distinct but quiet:**
  - background `#34343b`, one step lighter than sub-task cards;
  - border light green at 45% (the same mint token);
  - 16px of row spacing after it.
- **Click to open the conversation detail.** Clicking the main agent card in
  the expanded list opens an inline detail region inside the card. This keeps
  everything today's pet panel does, so nothing regresses (decided in review):
  - the latest exchange: the last user message and the unread results (or the
    last result), rendered with the existing Markdown renderer. Mark-read
    follows the existing rule: what is rendered and seen is marked read; when
    unread rows reach past the loaded tail, show "More in Avibe";
  - quick replies for the latest result, clickable, using the existing
    `QuickReplies` path (they never change Vibey's pose, #2408);
  - pending vault requests, one "需要授权，在 Avibe 中打开" button each;
  - the uncertain-send notice and the queued notice from the existing panel;
  - an "在 Avibe 中打开" link.

  The detail opens and closes with the `grid-template-rows 0fr ↔ 1fr`
  technique: 280ms settle for the rows, opacity 200ms ease. It grows inside
  the fixed scroll box: the box and Vibey do not move. It closes when the
  stack collapses, and clicking the card again also closes it.
- **Before the first send,** the card shows the main session's last exchange.
  If there is none, it is absent.
- **Pose:** Vibey takes the running pose while this card's turn runs.

### 4.3 Sub-task cards

- **Source:** the main session's turn state, `background_activities`. It
  already unions delegated runs (`item_kind: agent_run`), watches (`watch`)
  and scheduled tasks (`task`); this was verified live.
- **Title:** the item's `label`, falling back to `description`.
- **Subtitle:** "{kind} · {status}". Map the kind and raw status to readable,
  localized text: 委派运行 / 监控 / 定时任务; Running, 排队中, 等待中…
  Never show raw values such as `enabled`.
- **Running** means `status` is `running` or `processing`; it shows the
  running dot and the stop button.
- **Order:** the main agent card first, then running sub-tasks, then the rest.
  A sub-task the current turn just created goes directly below the main agent
  card. Live data must not reshuffle rows under the pointer while the list is
  expanded; apply reorders on collapse.

### 4.4 Empty

- With no main agent card and no sub-tasks, show nothing above Vibey. If the
  main session has history, there is always a main agent card.

## 5. Input: voice first, no always-on text box

### 5.1 Vibey and the dock

- **Click Vibey:** toggle the stack between collapsed and expanded.
- **Press and hold Vibey (≥ 320ms):** start dictation; release to stop.
  - A press shorter than 320ms is a click.
  - Leaving Vibey before the hold fires cancels the hold.
  - Suppress the context menu.
- **The summon hotkey** has the same effect as a hold: press starts
  dictation, release stops it. The global-shortcut handler handles only
  `Pressed` today; add `Released`.
- **While listening:**
  - Vibey takes the `listening` pose (`vibey-focus`);
  - a soft rose glow breathes around it: a radial gradient
    `rgba(251,113,133,.28) → 0`, about 1.2× Vibey's size, scale .92 ↔ 1.12
    with opacity .7 ↔ 1 over 1.6s ease-in-out;
  - the card stack fades out and moves 8px down (opacity 220ms ease,
    transform 260ms settle) and ignores the pointer. It comes back when
    dictation ends.
- **Dock:** a pill under Vibey's feet that overlaps the image by 12px.
  - Background `#2a2a2e`, padding 4px, shadow `0 8px 24px rgba(0,0,0,.45)`.
  - Two 32px buttons with a 4px gap and a 1px × 16px `white / 10%` hairline
    between them:
    - **mic** starts or stops dictation; it turns solid rose with a stop
      square while dictating;
    - **keyboard** opens or closes the input bubble; it is filled white while
      the bubble is open.
  - Hidden by default. It fades and rises in (opacity 180ms ease, transform
    220ms settle, from `translateY(-6px) scale(.92)`) on hover over Vibey or
    the dock, or on keyboard focus.
  - It stays visible while dictating or typing, and is always visible on
    devices without hover (`@media (hover: none)`).

### 5.2 Speech slot

- **One slot** sits at the bottom of the column, between the cards and
  Vibey. By priority it holds the marquee while dictating, otherwise the
  input bubble when it is open, otherwise nothing.
- **No margin of its own.** The last card's row spacing plus the panel's 6px
  padding separate it from the cards (about 14px).
- **Motion:**
  - opening grows the slot's height (`grid-template-rows 0fr → 1fr`, 300ms
    settle), so the stack above slides up with it;
  - the content fades and rises (opacity 180ms; `translateY(8px)
    scale(.97)` → rest over 300ms settle; 60ms delay on open);
  - closing reverses both;
  - the slot clips while it moves, then unclips once open so shadows show;
  - a closed slot stays mounted but `inert`.

### 5.3 Marquee (live transcript)

- **Look:** one line, the same width as a card.
  - Pill: background `#2a2a2e`, padding 8px vertical, 12px left, 16px right,
    shadow `0 12px 32px rgba(0,0,0,.45)`.
  - A 10px pulsing rose dot on the left, then 13px white text.
- **Text:** the realtime preview from the existing pipeline
  (`VoiceRealtimeSession` preview text + stash).
  - The newest words always stay visible: translate the line left by
    `max(0, scrollWidth − clientWidth)` with a 300ms ease-out transition.
  - A 28px mask fades the left edge once the text overflows.
  - Before any text: "正在听…", muted.
- **Ending dictation** (mic again, release, or end of speech) turns the slot
  into the input bubble, prefilled with the final text (appended after a space
  if a draft exists) and focused. **It is not sent.**
- **Esc** cancels dictation and discards the transcript.
- The marquee is `aria-live="polite"`.

### 5.4 Input bubble

- **Opens** after dictation, prefilled; or from the keyboard button, empty.
  Either way it gets focus.
- **Look:** the same width as a card, radius 22px, background `#2a2a2e`,
  border `white / 10%`, padding 6px (14px left), shadow as the marquee.
- **Textarea:** one line by default, auto-grows to 4 lines (96px), 13px,
  placeholder "和 main agent 说…".
- **Buttons, right side:**
  - a 32px mic (`white / 10%`) that dictates more and appends;
  - a 32px send button: white with a black up-arrow SVG; `white / 10%` and
    disabled when empty.
- **Keys:**
  - Enter sends; Shift+Enter is a newline; Enter during IME composition does
    not send;
  - Esc closes the bubble and discards the draft. Esc while dictating cancels
    dictation first. Esc with nothing open collapses the stack.
- **Sending:**
  - goes to the main agent only, with the existing composer rules: one
    message in flight, the uncertain-send lock, server-side queueing;
  - closes the bubble;
  - the main agent card takes over as the feedback: its title becomes the
    message, its subtitle starts at "Thinking", and Vibey takes the running
    pose until the real turn state takes over.

## 6. Stop a sub-task

Stop is the emergency brake: direct and deterministic.

- **Where:** expanded list, running sub-task cards only.
- **Two presses, no dialog.**
  - First press: the circle becomes a rose pill (`rose-500`, height 32px,
    12px horizontal padding, 12px semibold white text) that reads "停止？"
    for runs or "暂停？" for watches and tasks. It pops in over 180ms
    (`scale .85 → 1`, settle).
  - A second press within **3s** confirms. After 3s the pill reverts.
- **By kind:**
  - watch → `setHarnessWatchEnabled(id, false)` (exists);
  - task → `setHarnessTaskEnabled(id, false)` (exists);
  - agent run → **no HTTP cancel exists yet** (only the CLI `vibe runs
    cancel`). Hide the stop button on agent runs until the backend adds the
    endpoint (§11).
- **Pending:** the subtitle reads "正在停止…" / "正在暂停…" and the dot goes
  off. The button shows a 14px spinner and is disabled. Wait for the server's
  answer, not the click.
- **Done:** the subtitle reads "已停止" / "已暂停" in `white / 60%` with a grey
  dot (`white / 40%`), and the button disappears. The card lingers **2.5s**,
  then leaves with the dismiss motion (§7.3).
- **Failed:** restore the card, show "没停下来，稍后再试" in the subtitle, and
  keep the stop button.
- **The main agent is told** through the existing run/harness events; the
  user does not have to tell it.

## 7. Motion

The primary curve is **settle**: `cubic-bezier(0.32, 0.72, 0, 1)`.

### 7.1 Expand / collapse

- The column animates its **height** between the collapsed and expanded
  heights: 360ms, settle, with `overflow: hidden` while it moves.
- The incoming content fades and rises from `opacity 0, translateY(8px),
  scale(.98)`: 360ms, settle.
- Because the column is bottom-anchored, it grows and shrinks upward.
- **Only the collapsed ↔ expanded switch drives the outer height.** Changes
  inside the list (the main card detail, a dismissed card, a stopped card)
  move content inside the fixed box. Never let an outer height transition
  chase an inner one: two animations on different curves read as a jump.

### 7.2 Main agent card detail

- `grid-template-rows 0fr ↔ 1fr` over 280ms settle, with opacity over 200ms
  ease (§4.2).

### 7.3 Dismiss (×), and the end of a stop

Two beats, so the exit reads before the reflow:

1. **Exit, 0–200ms.** The card falls back and fades:
   - `opacity 1 → 0`, `translateZ(0 → −120px)`, `translateY(0 → −4px)`,
     `scale(1 → .88)`, `blur(0 → 2px)`;
   - 200ms, `cubic-bezier(0.4, 0, 1, 1)`, fill forwards;
   - the row has a 900px perspective.
2. **Close the gap, 140–480ms.** The row collapses with
   `grid-template-rows 1fr → 0fr` over 340ms settle, **with a 140ms delay**.
   The cards below slide up.
3. Remove the card from the data at about 500ms.

- The leaving row ignores the pointer.
- A second click on the same × is a no-op.
- A dismissed card does **not** come back on a refresh (§9.3).

### 7.4 No reflow jumps

- Spacing lives inside the collapsing row. A list `gap` does not animate: it
  stays during the collapse and vanishes on removal, which makes an ~8px jump
  at the end.

### 7.5 Scroll edge fades

- Mask the scroll box: `linear-gradient(to bottom, transparent 0, #000
  {top}px, #000 calc(100% − {bottom}px), transparent 100%)`.
  - `top = min(32, scrollTop)`: no fade at the top until the user scrolls.
  - `bottom = min(32, scrollHeight − clientHeight − scrollTop)`: no fade once
    the end is reached.
- Recompute on scroll, and whenever the box or its content resizes
  (ResizeObserver on both).

### 7.6 Poses

- Keep `ui/src/pet/pet.css`, including:
  - the 320ms squash on a pose change;
  - breathing on working poses;
  - the 30s breathing settle on resting poses, which keeps idle cost near
    zero.
- Vibey on the mini stage is **115px** (the owner tuned it on the prototype:
  2×, then −10%). Verify the size on a real desktop with the owner.

### 7.7 Reduced motion

With `prefers-reduced-motion: reduce`, every transition and animation in this
spec is off. States switch instantly, a dismissed card disappears at once, and
the marquee text still updates.

## 8. Busy rule (pose)

Vibey shows the running pose when **any** agent is working in a conversation
the user can see: the main session, built-in role sessions, or ordinary user
sessions.

- This keeps #2401: an `active` foreground session in the running-agents
  snapshot.
- It **adds the main session's own running sub-tasks**: running items in its
  turn state's `background_activities`. They run in background sessions, which
  #2401 excludes.
- The main session's own needs-input, blocked and ready states still win over
  busy (existing priority).

## 9. Shell (Tauri) work

### 9.1 Window

- **One fixed window size** in every state, large enough for the expanded
  column plus the speech slot, Vibey and the dock: about **460 × 600**
  logical px.
  - Collapsing does not resize the window; click-through (§9.2) makes the
    empty area harmless.
  - This replaces the collapsed 88 × 88 / expanded side-panel geometry
    (`window_size`, `PetLayout`).
- **Placement:** Vibey is anchored at the bottom centre of the window.
  - The stored anchor is Vibey's position.
  - The window is clamped into the work area, so Vibey cannot sit higher
    than the window's height above it.
  - Dragging Vibey drags the window (existing `startDragging`).
- **When the frame does not fit (decision 2026-10-12, from the #2508
  breaker).** On a work area shorter or narrower than the frame (for
  example 1366×768 at 125% minus the taskbar), full-frame clamping is
  impossible.
  - **Priority:** Vibey and its dock must always be fully inside a connected
    work area and reachable. The transparent rest of the frame may extend
    past the work area.
  - **How, decided per display:** choose the target from all connected
    displays: the one Vibey overlaps most, else its saved display if it is
    connected, else the primary. Then clamp with the whole frame if *that
    display* fits it, otherwise with Vibey's rect plus the dock. A pet the
    user parked on a small laptop screen stays there, even when a large
    external display is connected. With no display at all, keep today's
    behaviour.
  - **Expanded list on a short display:** its top may fall off-screen. This
    is accepted in v1; do not shrink or flip the column.

### 9.2 Click-through (verified in a spike)

- Only the cards, the speech slot, the dock and Vibey take the mouse.
  Everything else passes clicks to whatever is below.
- **One mechanism on every platform:**
  - The page reports its hit rects (window-local logical px) to the shell
    whenever layout changes: ResizeObserver, plus after state changes.
  - The shell polls the cursor (`AppHandle::cursor_position`), converts it to
    window-local logical px, and toggles `set_ignore_cursor_events(!inside)`
    on the main thread, only when the value changes.
- **Why not native:** on macOS, fully transparent pixels pass clicks through,
  but semi-transparent pixels (card shadows) catch them; this was verified.
  Windows and Linux have no native pass-through.
- **Cost:**
  - poll every 16ms only while the cursor is inside the window bounds;
  - back off to about 100ms when the cursor is outside them;
  - stop while the window is hidden.
- **Spike source:** `spec/spike-passthrough/` in the Show Page workspace.
- The new IPC command, for example `pet_hit_regions(rects)`, is granted to
  the pet page only, like the other pet commands, and must recheck its
  caller.

## 10. Main agent session (dependency; owned by another lane)

- **Model:**
  - sessions get a built-in type (`main`, `memory`, `ops`, …);
  - each user has their **own** `main`;
  - `main` is a writable session with **normal visibility** (the pet's
    validity rule accepts it);
  - it is **built in at install** (upgrades must create it too);
  - it **cannot be archived or deleted**;
  - its **id is stable** across context rotation.
- **Lookup API — not built yet; mock it.** Proposed contract:

  ```
  GET /api/sessions/main
  200 { "session": WorkbenchSession, "capabilities": { "can_chat": bool } }
  ```

  - The pet reads the main session only through one client function,
    `api.getMainSession()`.
  - Until the endpoint exists, the function returns the session the pet is
    bound to today (the user-picked binding from `pet_ready()`), in the same
    shape. Under this mock, keep a way to pick the session (for example in
    the main card's detail header).
  - When the endpoint lands: call it on start, on reconnect and after
    authorization changes. Then remove the switcher, "在桌宠中显示" and the
    binding writes. Older `pet.json` files that contain `binding` must still
    load; ignore the field (persisted-shape rule).

### 10.1 Interim: the mini talks to Vibey (owner decision, 2026-10-13)

Until the `main` type exists, the mini's main agent is a Vibey conversation
(the built-in Agent, `backend: vibey`).

- **One per person, created on first use.** `POST /api/desktop-mini/session`
  (Editor) returns the caller's conversation, creating it the first time in
  their most recently active Project they can chat in. A session needs a
  Project so it has a Workbench sidebar home and "在 Avibe 中打开" lands on it.
  It is an ordinary foreground session, so the Workbench shows and manages it
  like any other. Which session belongs to whom is recorded server-side in
  `state_meta` (`desktop_mini_session:<principal>`, principal `local` or
  `remote:<sub>`), never in session metadata a client can write or a fork can
  copy.
- **Who the caller is.** The principal comes from the request's authorization
  context, never from a second lookup that could fall back: a remote context
  without a subject owns nothing and is refused.
- **Reuse.** The same session is returned while it passes the checks a new one
  would: active, foreground, a Project the caller can chat in, and an Agent the
  caller may use. An archived session, an archived Project or lost
  access gets a new one. With no chat Project at all the answer is
  `409 no_chat_project`, and the mini asks the user to create a Project.
- **Binding.** The mini binds that session whenever nothing is bound. An
  existing binding is kept; the switcher offers "和歪比对话" to move to it.
  A session the mini already bound and then cleared as invalid is not
  re-bound automatically, so a bad answer cannot loop.
- **A turn Vibey cannot run is shown.** When the latest exchange ended in a
  backend failure notice (for example no model source), the main card's
  subtitle reads "没跑起来：<notice>" (the exchange ends at its foreground
  terminal row, by the transcript's own rule, so detached completions and
  replayed failures never decide it), and the detail shows the notice as plain
  text with "在 Avibe 中处理" when it names a next step. That opens the session,
  where the Workbench draws "Open Models" for those who manage the instance.
- **Not in this change.** A live step label ("Ran command", …) for the main
  card still waits on §11.1; the server already computes `status_label` for
  the IM status bubble and can expose it later.
- When `main` lands, this endpoint is replaced by `GET /api/sessions/main`.

## 11. Backend gaps (not in this delivery; degrade gracefully)

1. **No "current step" text.**
   - Neither the main card subtitle nor sub-task progress has a server field;
     activity rows are raw (`🔧 Bash {…}`).
   - Until the server emits a localized step phrase, show "Thinking" while
     the main turn runs. Show the mapped status for sub-tasks.
   - **Do not guess from tool names in the client.**
2. **No HTTP cancel for agent runs.** Hide stop on agent runs until it exists
   (§6).
3. **Dismiss persistence.** There is no server state for dismissed cards.
   Keep the dismissed ids in `pet.json`, so a dismissed card stays gone across
   restarts. Forget an id once its item disappears from the turn state. This
   adds an optional field, so older files keep loading.

## 12. Prototype-only (do not copy)

- The mock cards that pad the list to six.
- The simulated dictation script and its timing (one or two characters every
  100ms).
- The simulated turn steps and the spawned sub-task.
- The simulated stop and its 900ms pending time.
- The 3s polling of turn state; production uses the existing events plus the
  reconcile cadence in `usePetSession`.
- Hard-coded Chinese strings. Production copy goes through `ui/src/i18n`
  (en + zh).
- The page header, the "还差什么" section, and the extra chevron under Vibey
  in earlier versions.
- The page-coloured plate behind the expanded list (it only hides the web
  page's text; the desktop has none, §3.2).

## 13. Acceptance checklist

- [ ] Vibey never moves for any interaction in this spec.
- [ ] Collapsed: the front card is the first card (main when present); there
      is one layer per other card, with 9px steps, a `18+6+3i` inset and the
      same opacity as the front card.
- [ ] Clicking Vibey or the stack expands upward (360ms settle, height plus
      content fade); clicking Vibey again collapses.
- [ ] Expanded: fixed 3.5-card box; cards bottom-aligned; overflow scrolls;
      scrollbar hidden; 10px top padding.
- [ ] No backing plate behind the expanded list; gaps show the desktop and
      pass clicks through.
- [ ] Edge fades: none at the top until the user scrolls, up to 32px, none at
      the end; recomputed on resize.
- [ ] Cards, marquee and bubble share one width. A long title truncates and
      never widens a card.
- [ ] Main card: "MAIN AGENT" eyebrow; mint border; `#34343b` background;
      16px after it; no ×; no stop; no avatar.
- [ ] Clicking the main card opens and closes the detail inside the fixed
      box (280ms). The detail has the latest exchange, mark-read, More in
      Avibe, quick replies, vault buttons, uncertain and queued notices.
- [ ] Sub-tasks come from the main session's `background_activities`, with
      readable localized status and running first.
- [ ] Pressing and holding Vibey for 320ms, or the hotkey, starts dictation;
      release stops it. The listening pose and rose glow show, and the cards
      fade aside.
- [ ] The marquee shows the realtime preview with the newest words visible,
      a 300ms slide and a 28px left fade.
- [ ] Dictation ends in a prefilled, focused, unsent input bubble. The
      keyboard button opens an empty one.
- [ ] The slot opens and closes with height and content motion (300ms); the
      cards slide with it.
- [ ] Enter sends to main (IME-safe). The main card becomes the message and
      starts at "Thinking". Esc behaviour follows §5.4.
- [ ] The dock appears on hover or focus, stays while talking or typing, and
      is always shown without hover.
- [ ] Stop: two presses (3s window), then pending, done (2.5s linger, exit
      motion) or failed (restore). Watches and tasks pause; runs are hidden
      until a cancel API exists.
- [ ] Dismiss: 200ms exit, then a 340ms gap close after a 140ms delay; no end
      jump; the card stays gone after a refresh and a restart.
- [ ] Busy rule follows §8.
- [ ] Window: fixed size; Vibey anchored at the bottom; clicks pass through
      everywhere except the reported regions on macOS and Windows (shadows
      included); polling backs off and stops when hidden.
- [ ] Reduced motion turns everything above into instant switches.
- [ ] Icons are SVG. All copy is localized in en and zh.
