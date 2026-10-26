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

> P1 amendment (2026-10-15): §§4.2, 4.3, 5.2, 8 and 10 supersede the v2 main-card
> and background-card descriptions elsewhere in this document. Layout, shell,
> voice, dock, motion and timing remain v2. References to main-card detail now
> mean the reply bubble's expandable detail. §10 now uses the shipped owner-only
> main endpoint; the binding switcher and Workbench binding action are removed.

## 1. What changes, in one walk-through

1. Vibey (歪比) sits at the bottom of the mini. Above it is a **collapsed
   stack**. The front card is the **main agent card**: the user's last message
   to the main agent, and below it what the agent is doing ("Thinking").
   Thin layers under it show that more cards exist.
2. The user clicks the stack. The stack **expands upward** into a
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
  in the window. Near the top of the display it opens **downward** instead
  (owner decision, 2026-10-23, §9.1): Vibey and the dock sit at the top of the
  window and the whole column is mirrored below them.
- **Column width:** `min(440px, 94vw)`. Every card, the marquee and the input
  bubble are **the same width**: the column minus 18px on each side.
  - Expanded: 6px panel padding plus 12px scroll padding.
  - Collapsed: 18px stack padding.
- A card's width never depends on its content or on the number of cards.
  Vibey's reply bubble is the one exception, and only while it shows a status
  word alone (§4.2); the activity cards always keep the column width.
- **Long text never widens anything.** Every flex/grid item on the path from
  the list to a text node needs `min-width: 0`, and grid rows need
  `grid-template-columns: minmax(0, 1fr)`. (A grid column sized by a long
  nowrap title was the bug we hit.)

### 3.2 Expanded box

- **Shared expansion (owner decision, 2026-10-15).** The card list and Vibey's
  recent-turn history share the expanded space; they never expand together.
  History retains the collapsed stack above it and uses up to the list's 292px
  height, bounded by the measured stack, bubble header and fixed window. On the
  460×600 frame this leaves about 240px with four cards. Only overflowing history
  scrolls; ordinary replies and their Open in Avibe/approval/quick-reply actions
  fit without scrolling. Vibey and the dock keep their fixed anchors.

- **No backing panel on the desktop (owner decision, 2026-10-12).** The
  expanded list sits in a transparent wrapper with 6px padding (kept so the
  widths in §3.1 hold). Cards float directly over the desktop, separated by
  their own border. The gaps between cards show the wallpaper and
  pass clicks through (§9.2). The prototype keeps a page-coloured plate only
  to hide the web page's own text behind it (§12).
- **No shadows on any mini surface (owner decision, 2026-10-14).** Cards, the
  switcher, the dock, the marquee and the input bubble separate from the
  desktop by their border alone. A shadow overflows its box, and the scroll
  box, the speech slot and the stack/list swap all clip it, so it showed as a
  grey rectangle behind the switcher and the input and flashed as a square
  during the swap.
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

A stack needs at least **two** visible cards. **Exactly one** visible card is
never collapsed: it is always drawn in its full expanded form (stop or
authorize, Open in Avibe, × on hover, click to reference) at its own height,
whatever the panel state. A Vibey click (§5.1) opens or closes the history
beside it and never changes the card. All of the card's controls are hit regions in
every panel state. Opening the history keeps the card in full beside it, and
the history's height bound uses the card's measured height. Going between one
and two cards follows the data with the ordinary swap transition.

- **Front card:** the **first** card (the main agent card when it exists),
  showing its title and subtitle only: no ×, no stop button.
- **Layers:** at most **three** behind the front card, regardless of the
  number of remaining cards. For layer `i` (0-based, nearest first):
  - **the same height as the front card**, offset `(i + 1) × 9px` below it,
    so its bottom edge peeks out `(i + 1) × 9px` under the front card's
    bottom whatever the front card's height. Do not use a fixed layer height:
    a fixed 52px is hidden entirely behind a taller front card (the main card
    with its eyebrow);
  - horizontal inset `18 + 6 + 3i` px from each column edge;
  - radius 24px, background `#1a1a1e`, border `white / 8%`;
  - **the same opacity as the front card** (no per-depth fade);
  - nearer layers stack above farther ones.
- The stack's height is the front card plus up to `3 × 9px`.
- When there are at least two cards, the front card's eyebrow row shows a
  muted count chip: `N items` in English or `共 N 项` in Chinese. Counts above
  99 display as `99+`.
- The whole stack is one button: a click expands it. It is the only way to
  expand the cards; Vibey collapses an expanded list but never expands one
  (§5.1). The expanded list and the open history never show together: opening
  either closes the other.

## 4. Cards

### 4.1 Common anatomy

| Part | Spec |
|---|---|
| Container | Radius 28px; padding 20px × 14px; background `#2a2a2e`; border 1px `white / 8%`; no shadow (§3.2). |
| Title | 15px, semibold, tight tracking, white, one line, ellipsis. The full text is the tooltip. |
| Subtitle | 13px, `white / 45%`, 2px below the title, one line, ellipsis, with the full text as the tooltip. While running it has a 6px pulsing dot (`emerald-400`) before the text. |
| × (dismiss) | Expanded activity cards only (a lone card always counts as expanded, §3.3). 20px circle at the top-left corner, offset −6px/−6px. Hidden until the card is hovered or receives focus within; always visible when hover is unavailable. Background `#3a3a3e`, glyph 11px `white / 70%`; on hover `#4a4a50` with a white glyph. Sits above the card content. |
| Stop | Expanded list or a lone card only (§3.3), running sub-tasks only (§6). A 32px circle with background `white / 10%` and a filled rounded-square SVG icon at `white / 80%`. Its right gap and top gap to the card edge are both 16px. |

- There is **no reply/return button** and **no Follow up** (owner decisions).
- **Icons are SVG**, never Unicode glyphs. iOS renders `↩` as a blue emoji.

### 4.2 Personal Agent reply bubble

There is no main-agent card. Vibey's own progress and reply occupy the speech
slot above the avatar: thinking, delegated-to (only when provenance is known),
then the latest reply. The bubble names Vibey in its existing localized spelling.
It remains available with either the collapsed or expanded stack.

**When it shows.** While Vibey's conversation is active: the turn is running
or thinking, or a send is pending; Vibey needs the user (a pending Vault
handoff, a failure notice, an uncertain-send notice, or a queued notice); or
the history is open. Composing and dictating take the slot as before (§5.2).
Then, when Vibey's turn ends, the bubble shows the reply summary for
**10 seconds of on-screen time** before it hides with the speech slot's
transition (instant under reduced motion), taking no space and no clicks.

- **The reply of a turn this epoch watched end.** One thing lingers: that
  reply, pinned by its id, and everything about the linger is derived from it
  while rendering. The turn end is the main session's server turn state going
  from running to done while the current epoch (binding and authorization)
  watches; the local send grace (which shows "thinking" before the server
  registers a turn) is not part of it. The reply is the newest non-empty agent
  result after the input of that turn that is not a background run's detached
  completion; nothing before that input counts, so a promoted answer to an
  earlier input (#2815) keeps the badge but does not linger. A stopped
  turn (which ends with an empty result) or a turn with no reply does not
  linger; a reply that lands after its turn end lingers once it arrives. The
  summary shows the pinned reply for as long as it lingers, even if a later
  result (a background completion) arrives. The render that ends the turn is
  the render that starts the linger, so the bubble never closes in between;
  the only state is which replies have expired or been seen, per epoch. A turn
  already over when the epoch first looks is history, so on first load an old,
  finished exchange starts hidden. A reply that appears without a watched turn
  end (a background or detached result, a turn that starts and ends while
  authorization is still resolving, or a turn so quick the pet never saw the
  server report it running) does not linger; Vibey's badge still marks it, and
  a Vibey click opens it. A turn that ends in failure keeps the bubble shown
  through its failure notice (needs-you).
- **On-screen time.** The 10s count only while the reply itself is on
  screen: the document is visible, the slot is drawing this bubble (not the
  input, the dictation marquee, or the loading and unavailable notices), its
  summary row is the reply (not "thinking" while the turn still reads as
  running, nor a failure notice), and the pointer is not over the bubble.
  The pointer reading counts only while the bubble is drawn; when the input or
  the marquee replaces it, or it hides, the reading is dropped, since no leave
  event may come.
  Typing or dictating pauses it, and the rest plays when the bubble is back.
  Leaving the bubble with the pointer leaves at least 3s. One timer, rescheduled
  whenever these change.
- **Keyboard focus** is read when the time runs out, not tracked: focus on a
  focus-visible control inside the bubble keeps the reply and is looked at
  again a second later. A pointer click never pins it. If the bubble goes
  while focus is inside, whether it folds away or is replaced (by the input,
  the dictation marquee, or a loading notice), focus moves to Vibey's button.
- **Looked at means done.** A reply present while the history is open (its
  expansion confirmed by the shell) counts as seen and does not linger after,
  whether the history was opened from the lingering reply (a Vibey click or its
  toggle row) or the reply arrived while it was already open; closing the
  history hides the bubble. A reply that arrives only after the history has
  closed was not seen there, so it lingers. A history the user asked for that
  the shell has not confirmed yet holds the bubble and pauses the linger; if
  the shell refuses, the request is dropped and the linger resumes. A new running turn holds the bubble again, and its
  own result lingers anew.

Unread replies only light Vibey's badge and never hold the bubble; quick
replies stay in the history. A new running turn, a new needs-you state or a
Vibey click that opens the history shows the bubble at once.

Click the bubble to expand recent turns from the loaded tail. Preserve all
previous main detail behavior:

- Markdown for the latest exchange and recent user/result rows;
- mark-read only for the current, fully loaded unread exchange when its result
  is visible and the page has focus; incomplete unread tails keep More in Avibe;
- latest quick replies, pending Vault authorization handoffs, queued and
  uncertain-send notices, and Open in Avibe;
- the document-wide send lock and authorization/session epoch fencing.

The detail opens **above** the toggle row (speaker row and summary line) and
grows upward. The toggle row stays at the bubble's bottom and keeps its screen
position in both states, so collapsing needs no mouse travel; the bubble's
bottom edge and tail never move. The toggle stays first in the DOM, so focus
order is unchanged; CSS places the detail above it, and the toggle's
`aria-controls` names the detail. The divider sits between the history and the
toggle row. On open the history scrolls to the very bottom, chat-style: the end
of the newest result and the quick replies, Vault handoffs and notices that
follow it sit right above the toggle row, with the §7.5 edge fades (no bottom
fade, since the end is reached). While it is open, new content, or the box
shrinking because the input below it grows (§5.2), keeps a reader who is at the
bottom (within 8px) pinned there and leaves one who has scrolled up where they
are. The pin's own scroll is never read as the reader scrolling up. Mark-read needs any part of the latest result in view. Collapsed shows an up chevron and expanded a down chevron.

Open in Avibe is an icon-only button (the card's `ExternalLink` open button,
28px, on a solid surface) pinned to the top-right corner of the open history,
8px from the bubble's outer edge on both the top and the right, which centres
it on the 22px corner arc,
outside the scroll box: it stays put while the history scrolls, is never faded
by the edge mask, and exists only while the history is open. Its accessible name is
"Open in Avibe", and instead of a native `title` it shows a pet-styled tooltip
with that text: after about 350ms of hover, at once on keyboard focus, gone on
leave, blur or Escape (which it takes before the pet's own Escape). The tooltip
sits below the icon, right-aligned; the activity cards' open button uses the
same button and tooltip, placed to its left so the card scroll box cannot clip
it. Both are portalled to the pet stage. Hover only reaches the page while the
pet window receives hover (§5.1), which it does once opening the history or the
cards has made it key. The history's top padding reserves that corner,
so at the top of the scroll it covers no text. An incomplete unread tail keeps
its "More replies in Avibe" notice as plain text; the pinned button is the way
there. The history's scrollbar is hidden like the card list's (§3.2); the edge
fades show there is more.

Detail retains the 280ms grid / 200ms opacity transition and closes on stack
collapse. Its own scroll region fits the existing window; opening it never
resizes the avatar or the 292px activity scroll box. Input and dictation hide
the bubble, quick replies included, unless its history is open: an open history
stays above them (§5.2).

**Width (owner decision, 2026-10-23).** While the summary row is a status word
alone (thinking while the turn runs, or waiting with no reply) and the history
is closed, the bubble is sized to its content, at least 160px, and centred on
Vibey like the column, so its tail and Vibey stay put. When the reply (or a
failure notice) takes the summary row, or the history opens, it widens to the
column width from the centre over 300ms settle; a new running turn narrows it
the same way. Reduced motion switches at once. The width animates through an
explicit pixel width that is handed back to CSS when it lands, since CSS
cannot interpolate between a content size and a percentage. The activity
cards keep their width.

### 4.3 Visible activity cards

Use `api.getRunningAgents()` as the activity source, shared by the card and
team-pose derivation. Exclude the bound main session, `ses_main`, missing session
identities, and rows without explicit foreground visibility and chat access.
Deduplicate by session; an active row wins over an idle row. A process going
idle or disappearing is not evidence of a completed reply.

The existing Inbox supplies unread evidence. Foreground unread sessions in its
loaded feed remain eligible after their process disappears, with visibility
revalidated by `getSessionResult`. No separate unread counter is created.
The card says Replied while it has unread evidence and no running process.
Callbacks to the main session are reported in Vibey's bubble.

- Heading: project · agent; for IM, platform · project · agent.
- Title: session title, with the existing localized fallback.
- Priority: needs authorization, running, then replied. Preserve relative order
  within a priority group while expanded; a newly actionable card moves first.
- Clicking the card body appends the Web marker `#<session-id>` to the main
  draft. It appears as a removable title chip; sending uses the main binding and
  the existing session-reference sidecar. Cards never send replies directly.
- Actions: dismiss, authorization handoff, Open in Avibe, and supported stop.
  A foreground Web session exposes stop only after its live turn-state confirms
  an in-flight dispatch turn. Use `cancelSession`, with §6's two presses, 3s
  confirmation, 2.5s success linger and §7's exit. Runtime teardown is not stop.
  Agent runs and IM rows without this cancellation evidence hide the control.
  A process snapshot does not identify a Watch/Task definition; never infer a
  definition ID or pause a schedule from a runtime identifier.

Known data limitations for P1:

- `trigger_source` defaults to human and does not prove caller lineage. Omit
  the delegated badge and delegated-to summary until a reliable read-only
  relation exists; do not fabricate attribution or suppress a visible result.
- Background/system/unknown visibility is excluded. Ordinary questions lack a
  reliable attention field; only pending Vault requests supply this signal.
- Replied fallback is bounded by the existing loaded Inbox feed, which covers
  Web Inbox data. A process-free session absent from that feed cannot be
  discovered here, including a Vault-only session with no unread Inbox row.

### 4.4 Empty

- With no main agent card and no sub-tasks, show nothing above Vibey. If the
  main session has history, there is always a main agent card.

## 5. Input: voice first, no always-on text box

### 5.1 Vibey and the dock

- **Click Vibey** (a pointer click without hold or drag, or keyboard
  activation): one step about Vibey's own conversation, in order:
  - an expanded card list collapses, and nothing else happens;
  - else an open history closes, and the bubble hides with it unless something
    else keeps it shown (§4.2);
  - else, if there is any exchange, the bubble shows with the history open,
    scrolled to the bottom;
  - with no messages yet, the click does nothing.

  Typing and dictating do not stop a click (owner decision, 2026-10-23): the
  history opens and closes above the input or the marquee, which keep their
  draft, focus, caret and capture. A click on Vibey is not leaving the input:
  the input keeps the focus (Vibey's press does not take it).
- **Double-click Vibey** (owner decision, 2026-10-26) opens Vibey's
  conversation in the Avibe main window (`avibe://session/<main>`, routed to
  `/chat/<id>`). A pointer click waits for the double-click window before it
  toggles Vibey's history, so the first press of a double-click has no visible
  effect and cannot collapse the card list. A second press inside the
  double-click window cancels that pending single click and opens the window,
  leaving history and cards exactly as they were before the gesture. The page
  pairs the platform click count with the elapsed time between presses, and
  also honours a native `dblclick` event without opening the session twice.
  This v1 uses a 450ms page interval; matching the native macOS
  `NSEvent.doubleClickInterval` remains a known gap. Keyboard activation
  (`event.detail === 0`) toggles immediately. A hold or a drag is never a
  click, and a pointer cancel or leaving Vibey before release cancels any
  pending click.

  Opening the session, or losing the pet window's key while neither dictating
  nor typing, clears the hover state and blurs Vibey. The page suppresses the
  dock while the shell still reports the pointer over Vibey; the next
  `hover: false` arms it again, and a later `hover: true` shows it. If the
  shell already reported `hover: false` before suppression, the first new
  `hover: true` restores the dock without requiring another leave. CSS
  `:hover` and focus cannot bypass this suppression. The dock remains visible
  while dictation, finishing, or typing is active. A double-click's first
  press does not open history, so it does not mark a lingering reply as seen;
  “looked at means done” still requires a history the user actually opened.

  Every branch leaves the shell's panel state matching what is drawn, so
  nothing stays expanded out of sight. Vibey's button is labelled "Recent
  turns", with `aria-expanded` reflecting the open history. It has no tooltip:
  hovering Vibey already shows the dock, and a tooltip on every hover of the
  pet would be noise.
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
  - Background `#2a2a2e`, padding 4px, border 1px `white / 8%`, no shadow.
  - Two 32px buttons with a 4px gap and a 1px × 16px `white / 10%` hairline
    between them:
    - **mic** starts or stops dictation; while dictating, it uses the same
      background as the button hover state with a stop square icon;
    - **keyboard** opens or closes the input bubble; while the bubble is open,
      it uses the same background as the button hover state. Hover, dictation,
      and the open keyboard state share this background and the normal button
      icon color.
  - Hidden by default. It fades and rises in (opacity 180ms ease, transform
    220ms settle, from `translateY(-6px) scale(.92)`) on hover over Vibey or
    the dock, or on keyboard focus.
  - Hover comes from the shell: on macOS a webview gets no hover while its
    window is not key, and the pet never takes focus by itself. The page marks
    the rects of Vibey and the dock `hover: true` in `pet_hit_regions`; the
    cursor poller (§9.2) sends `avibe:pet-hover { hover }` when the cursor
    enters or leaves them, and the page shows the dock while it is set. CSS
    `:hover` still applies where the webview does see hover, except while the
    idle dock is suppressed after opening Avibe or losing key. In that case
    it stays hidden until the shell reports a leave followed by a new enter
    (or just a new enter if the pointer was already outside),
    even while the pet window remains unfocused.
  - It stays visible while dictating or typing, and is always visible on
    devices without hover (`@media (hover: none)`).

### 5.2 Speech slot

- **One slot** sits at the bottom of the column, between the cards and
  Vibey. By priority it holds the marquee while dictating, otherwise the
  input bubble when it is open, otherwise Vibey's reply bubble when it has
  progress, history, a notice, quick replies or a pending authorization.
  Only the highest-priority content is interactive or eligible for mark-read,
  except an open history.
- **History above the input (owner decision, 2026-10-23).** Vibey's history,
  opened while the marquee or the input holds the slot, is drawn above them in
  the same slot, 14px apart (room for the bubble's tail); both are
  interactive. Opening the input keeps an open history, and closing the
  history leaves the input as it is. The history's height also leaves room for
  the measured input below it, which grows to its four lines inside the fixed
  window; nothing clips.
- **Mutually exclusive expansion.** Opening "Recent turns" collapses the cards
  into their layered stack. Opening the card stack expands the cards and closes
  history. Closing history returns to the collapsed mini.
  Both transitions reuse the v2 height/content motion (§7.1, §7.4); reduced
  motion switches directly. There is no separate 48–120px history cap.
- **No margin of its own.** The last card's row spacing plus the panel's 6px
  padding separate it from the cards (about 14px).
- **Motion:**
  - opening grows the slot's height (`grid-template-rows 0fr → 1fr`, 300ms
    settle), so the stack above slides up with it;
  - the content fades and rises (opacity 180ms; `translateY(8px)
    scale(.97)` → rest over 300ms settle; 60ms delay on open);
  - closing reverses both;
  - the slot clips while it moves, then unclips once open;
  - a closed slot stays mounted but `inert`.

### 5.3 Marquee (live transcript)

- **Look:** one line, the same width as a card.
  - Pill: background `#2a2a2e`, padding 8px vertical, 12px left, 16px right,
    no shadow.
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
  border `white / 10%`, padding 6px (14px left), no shadow.
- **Textarea:** one line by default, auto-grows to 4 lines (96px), 13px,
  placeholder "和歪比说…".
- **Buttons, right side:**
  - a 32px mic (`white / 10%`) that dictates more and appends;
  - a 32px send button: white with a black up-arrow SVG; `white / 10%` and
    disabled when empty.
- **Keys:**
  - Enter sends; Shift+Enter is a newline; Enter during IME composition does
    not send;
  - Esc closes the bubble and discards the draft. Esc while dictating cancels
    dictation first. An open history above the input or the marquee closes
    before either. Esc with nothing open collapses the stack.
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
- A dismissed card stays closed through activity refreshes in the same document.
  Reloading or reopening the mini starts fresh (§11.3).

### 7.4 No reflow jumps

- Spacing lives inside the collapsing row. A list `gap` does not animate: it
  stays during the collapse and vanishes on removal, which makes an ~8px jump
  at the end.

### 7.5 Scroll edge fades

- Mask the scroll box: `linear-gradient(to bottom, transparent 0, #000
  {top}px, #000 calc(100% − {bottom}px), transparent 100%)`.
  - `top = min(band, scrollTop)`: no fade at the top until the user scrolls.
  - `bottom = min(band, scrollHeight − clientHeight − scrollTop)`: no fade once
    the end is reached.
- The card list uses a 32px band with that linear ramp.
- The reply history (§4.2) uses a taller, darker 48px band. Its ramp fades the
  outer part of the band strongly: from the edge, `transparent 0`,
  `rgba(0,0,0,.12)` at 35% of the band, `rgba(0,0,0,.55)` at 70%, and `#000` at
  100%, mirrored at the bottom.
- Recompute on scroll, and whenever the box or its content resizes
  (ResizeObserver on both).

### 7.6 Poses

- Keep `ui/src/pet/pet.css`, including:
  - the 320ms squash on a pose change;
  - breathing on working poses;
  - the 30s breathing settle on resting poses, which keeps idle cost near
    zero.
- Use the layered Vibey art in `ui/src/pet/VibeyArt.tsx` for the accessory
  motion. `needs_input` reveals three dots from bottom to top before the
  envelope pops; `blocked` lets the sweat drop flow down; `running` lights its
  trail dots and rocks the question mark; `team` uses the same body with a
  turning wrench in the thought bubble; `ready` bursts confetti outward and
  down; and `sleeping` sends a small `z` followed by a large `Z` upward.
  `idle` and `listening` render body-only art with the existing breathing.
- The layered loops animate `transform` and `opacity` only. They pause while
  the window is hidden (`document.visibilityState` is not `visible`); focus
  does not matter, because the pet window is visible without ever taking
  focus. Reduced motion disables every animation and leaves the
  full static composition visible. Sleeping deliberately keeps its `z`/`Z`
  loop while the window is visible, even though idle still settles after 30s.
- Vibey on the mini stage is **115px** (the owner tuned it on the prototype:
  2×, then −10%). Verify the size on a real desktop with the owner.

### 7.7 Reduced motion

With `prefers-reduced-motion: reduce`, every transition and animation in this
spec is off. States switch instantly, a dismissed card disappears at once, and
the marquee text still updates.

## 8. Pose priority

Use the first matching condition, following the approved prototype's `POSES`:

1. Listening: active dictation.
2. Needs input: pending authorization in the main session or any eligible card.
3. Blocked: the main session failed.
4. Running: the main session is running.
5. Ready: main unread results or a nondismissed replied card.
6. Team working: an eligible activity is active. This uses the `team` pose,
   with the wrench bubble, while Vibey's own turn keeps the `running` pose.
7. Idle; sleeping after five idle minutes.

Cards and team activity use the same filtered snapshot. Background definitions
alone do not create cards or a busy pose. Quick-reply suggestions do not count
as needs input. Preserve reduced-motion behavior.
The lower-left green activity-count badge is removed; the stack and team pose
already communicate activity. Keep the upper-right red unread badge.

## 9. Shell (Tauri) work

### 9.1 Window

- **One width, and no resize for any interaction state.** The window is
  460 logical px wide. Collapsing, expanding, the speech slot and the cards
  never resize it; click-through (§9.2) makes the empty area harmless. Its
  height is decided only where the pet is placed (below), at most **600**.
  - This replaces the collapsed 88 × 88 / expanded side-panel geometry.
- **Placement:** the stored anchor is Vibey's image top-left. Dragging Vibey
  drags the window.
- **Which way the column opens (owner decision, 2026-10-23).** On a display
  with room for the full 600px window above Vibey's image (449px), the column
  opens upward, as before: Vibey sits at the bottom of the window. Otherwise
  the column opens toward the side with more room: below the dock when there
  is more room below than above (Vibey and the dock at the top of the window,
  the image 12px from its top), else upward. Ties stay upward.
  - The window reaches from Vibey toward the column only as far as the work
    area goes, up to 600px, so no part of it is ever off the display. macOS
    never lets this window's top rise above the menu bar (verified: even a
    programmatic frame is pushed back down at its floating level), so a frame
    that did would move Vibey instead.
  - The page draws the layout the shell reports (`panel_edge: top | bottom`)
    and mirrors the whole column for `top`: the speech slot stays nearest
    Vibey, the stack's layers peek toward Vibey, the history opens on the far
    side of its toggle row with Open in Avibe on the far corner, the bubble's
    tail points at Vibey, and content enters moving toward Vibey. Focus order
    is unchanged.
- **The shell owns the frame and the layout (decision 2026-10-23, after the
  #2945 breaker).** One state machine in the shell (`FrameState`) holds the
  window frame and the layout snapshot `{revision, layout}`, the page's only
  source for which edge to draw. A frame change that keeps the layout applies
  at once. One that changes it, and every drop, gets a new revision and waits.
  The shell publishes the snapshot to the document showing now
  (`avibe:pet-layout`), whichever document started the change; a document
  that loads reads the same snapshot from `pet_ready`. The page draws only a
  newer revision, then acknowledges it (`pet_settle(revision)`), and the
  waiting frame comes back only on its own revision. Stale acknowledgements
  change nothing; after 1s without one the frame applies anyway. That 1s
  belongs to one publication: when the machine publishes the held snapshot
  again (a document loaded meanwhile), the earlier timer releases nothing and
  the new one starts over. No reply
  carries a layout to current pages (`pet_set_expanded` still answers one for
  older pages). The page always acknowledges the snapshot in its `pet_ready`
  reply, even a revision it already drew from an event.
  - The machine also knows what the document draws: *loading* (from the
    moment a document starts loading until its `pet_ready`), *layouts*, or
    *legacy*. While it is loading nothing held is released, expiry included;
    when it is ready, a page that draws layouts gets the held snapshot again,
    and an older page gets the upward window at once, in place of a held frame
    or a current one in the top layout (mid-drag, in place of the frame a
    failed drag returns to).
  - Every drag ends idle or holding: one that does not end with a placement
    (the native drag did not start, the window could not be read on release,
    the window was replaced, the release could not be handled) goes back to
    the frame and anchor it started from, held like any other change of layout
    if that layout is no longer the one the page draws.
- **Dragging.** A 600px window cannot take Vibey near the menu bar, so the
  drag goes through the shell (`pet_drag`): it shrinks the window to the
  **drag strip** around Vibey and the dock (12px above the image, 36px below
  it: 163px at full width), keeping Vibey under the pointer, and starts the
  native drag. Both layouts draw Vibey at the same place in the strip, and the
  page hides the column in any window that short (a sliver otherwise). If the
  native drag cannot start, the window goes back as it was.
  - The shell says it can drag the pet before any page script runs: an
    initialization script defines `window.__AVIBE_PET_DRAG__`. Shells without
    the marker keep the page's old `start_dragging` path. The shell drags
    only a document whose `pet_ready` has told it the document draws layouts,
    and refuses `pet_drag` from one it still takes for loading (or legacy).
    The page drags through the shell only once its `pet_ready` has succeeded;
    before that a press drags the window natively, which the shell treats
    like any other move (below). A failed `pet_ready` is asked again after
    250ms, doubling up to 8s, until it succeeds.
  - On release (the shell reads the buttons: no button down in
    `pressedMouseButtons` on macOS, whose bits are physical and whose primary
    side can be the right one; the primary button in `GetAsyncKeyState` on
    Windows; where it cannot, a drag ends once the window has not moved for
    400ms), the shell keeps the pet whole on a display (as on restore),
    chooses the window for that spot, saves the anchor and publishes the drop
    as above.
  - A page that predates the top layout (the shell can adopt an older running
    Runtime) does not declare `layouts` in `pet_ready`; it only ever gets the
    upward window at the same spot, as tall as the room above allows, so
    Vibey stays put and nothing is mis-drawn.
- **Nothing is drawn before the layout is known.** Under a shell with the
  marker, the page keeps its stage invisible (and so reports no hit regions)
  until it has the shell's first snapshot from `pet_ready`, so a pet restored
  in the top layout never shows a frame in the other one as it loads. If
  `pet_ready` fails or has not answered after 4s, the page draws the default
  layout anyway (and keeps asking, as above). The window itself is created and shown as before: a hidden
  macOS window would pause the page's animation frames, and with them its
  first acknowledgement. Older pages, which do not read the marker, draw at
  once as before.
- **Every other move gets the window for its spot.** When the platform moves
  the window (a display is removed, a work area or scale changes) or an older
  page drags it natively, the shell, once the move settles for 400ms, keeps
  the pet whole on a display and applies the window for that spot through the
  same state machine. An older page still cannot drag Vibey above the 600px
  window's reach: only the shell's drag shrinks the window.
- **What must stay on a display.** Restore on launch, a dropped pet and a
  display or scale change keep the drag strip inside a connected work area:
  at the window's width on a display that fits the full 460 × 600 window,
  else at Vibey's width. The window for that spot then follows the rule above.
  - **Which display (decision 2026-10-12, from the #2508 breaker):** the one
    Vibey overlaps most, else its saved display if it is connected, else the
    primary. A pet the user parked on a small laptop screen stays there, even
    when a large external display is connected. With no display at all, keep
    today's behaviour. The saved display for a drop or a move is the one
    saved before it (a drag saves nothing on its way), so a pet dropped off
    every display, as Windows allows just above a secondary one, returns to
    the display it came from.
  - A short display gets a shorter window, never a window past its edge. The
    expanded list keeps its fixed box, so on such a display its far end can
    still be cut at the window's edge, as before.

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

## 10. Main agent session (implemented; main-only)

`GET /api/sessions/main` shipped in #2637. The mini uses this endpoint through
`api.getMainSession()` as its only conversation selector:

```
GET /api/sessions/main
200 { "session": WorkbenchSession, "capabilities": { "can_chat": true } }
```

- **Owner-only in this release.** The instance owner uses the stable `ses_main`
  session. Non-owner 403 is an accepted unavailable state; per-member main
  sessions and permission expansion are outside P1.
- **Lifecycle and recovery.** Resolve main on startup, reconnect and authorization
  change, and retry on page reactivation after transient failures. Use the existing
  authorization epoch and `FencedSource` request ordering. No new polling loop.
  Transcript/runtime reads wait for main's explicit chat grant. Authorization
  changes immediately invalidate pending reads and action callbacks before resolving
  again. Retain the user's draft and document send ledger across transient failures.
- **No switcher.** The bubble's switch button, the entire recent-session picker,
  its "Talk to Vibey" option and the Workbench "Show in pet" action are removed.
  The only allowed binding write is `pet_bind("ses_main")` after successful main
  resolution, to normalize the legacy binding. No caller writes another
  session or invokes `pet_unbind`. Summons still open/listen
  through the existing shell flow and wait for a usable main.
- **Persisted compatibility.** Existing `pet.json` shapes still load unchanged.
  Legacy `binding` values and bound events never select a conversation, including
  when they arrive after main has resolved. The existing native IPC normalizes empty or other persisted bindings to
  `ses_main`; the file format and native code remain unchanged.
- **Unavailable.** A 403, `409 main_session_invalid`, `409 main_session_archived`,
  `builtin_agent_unavailable`, or a transport failure shows the existing localized
  unavailable message and "Open in Avibe". No arbitrary-session fallback. Pending
  resolution shows the existing loading copy; reconnect/reactivation can recover.
- **Main-only binding normalization.** After main resolves successfully, compare the legacy
  binding from `pet_ready` with `ses_main`. If different, make at most one
  `pet_bind("ses_main")` attempt per surface epoch. An already-main file
  needs no binding write. A failed normalization does not retry in a loop;
  a later epoch may try again. This does not synchronize dismissals.
- **Document-only dismissal.** Manual × closes a card for the current document.
  The last successfully resolved main owns these closes, including new closes
  made during a temporary main lookup failure. Do not read or write native
  `dismissed_ids`; `pet_ready` and binding normalization cannot restore, clear,
  or delay document closes. Reloading or reopening starts with no closed cards.
  Forget an id on an explicit new turn, or after it disappears from eligible
  activities and has no unread Inbox reply. Absence-based forgetting and local
  reappearance detection require an applied whole-account Inbox unread snapshot,
  no initial Inbox feed load in progress, a successful running-agents snapshot,
  and no pending reply-activity validation. Retain validated reply rows during
  revalidation; only the latest read may replace them. A failed read stays
  non-authoritative until a later event retries. No new polling loop or shared
  activity-source lifecycle is introduced.
- **Turn-scoped card presentation.** Confirmation, cancellation, stopped/failed
  notices, exits, leaving animation and retained cards use `sessionId:generation`
  keys. Increment the local generation on `turn.start` or healthy activity
  disappearance followed by reappearance. Retire that key's timers together;
  every timer/cancel callback checks its captured key and authorization epoch.
  Old callbacks cannot alter a new turn. Stop exits never add manual dismissals.
- **Standalone stop eligibility.** A foreground, chat-openable human activity
  with `platform = null` is a Web candidate, as is `platform = avibe`. Both still
  require an explicitly in-flight turn-state read; actual IM platforms stay excluded.
- **Turn failures remain visible.** A foreground terminal failure notice is shown
  in the reply bubble/detail, with "Fix in Avibe" when it names a next step. Detached
  completions and replayed failures do not replace the current exchange. Live step
  labels still wait on §11.1; the mini shows "Thinking" meanwhile.

## 11. Backend gaps (not in this delivery; degrade gracefully)

1. **No "current step" text.**
   - Neither the main card subtitle nor sub-task progress has a server field;
     activity rows are raw (`🔧 Bash {…}`).
   - Until the server emits a localized step phrase, show "Thinking" while
     the main turn runs. Show the mapped status for sub-tasks.
   - **Do not guess from tool names in the client.**
2. **No HTTP cancel for agent runs.** Hide stop on agent runs until it exists
   (§6).
3. **No dismissal persistence in this delivery.** A manual × lasts only for the
   current mini document. Refreshing the page or restarting the mini lets the
   activity sources populate cards again. Existing `pet.json` files still load,
   but their `dismissed_ids` are ignored and no dismissal IPC writes are made.
   Forgetting within the document follows §10. Cross-restart memory and a unified
   readiness model for the three activity sources are deferred.

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
      are at most three layers, with 9px steps, a `18+6+3i` inset and the
      same opacity as the front card. Two or more cards show the count chip,
      capped at `99+`.
- [ ] Clicking the stack expands upward (360ms settle, height plus
      content fade); clicking Vibey again collapses.
- [ ] Expanded: fixed 3.5-card box; cards bottom-aligned; overflow scrolls;
      scrollbar hidden; 10px top padding.
- [ ] No backing plate behind the expanded list; gaps show the desktop and
      pass clicks through.
- [ ] Edge fades: none at the top until the user scrolls, up to 32px, none at
      the end; recomputed on resize.
- [ ] Cards, marquee and bubble share one width. A long title truncates and
      never widens a card.
- [ ] Main card: "Vibey" / "歪比" eyebrow; mint border; `#34343b` background;
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
- [ ] Window: fixed width, never resized by an interaction; Vibey anchored at
      the bottom, or at the top with the column mirrored below when it has
      been dragged there (§9.1), the window never past the work area.
- [ ] Vibey drags all the way to the top of the work area and back; the
      column flips on drop without showing the other layout at full size.
- [ ] Clicks pass through
      everywhere except the reported regions on macOS and Windows (shadows
      included); polling backs off and stops when hidden.
- [ ] With the input or dictation open, a Vibey click opens the history above
      it and a second closes it; draft, focus, caret and capture survive, and
      Esc closes the history first. Everything fits the fixed window.
- [ ] Vibey's bubble is content-sized and centred while it only says
      thinking/waiting, and widens from the centre when the reply arrives.
- [ ] A double-click on Vibey opens its conversation in Avibe and leaves the
      history and cards as they were; pointer single clicks take the
      double-click window to confirm.
- [ ] Reduced motion turns everything above into instant switches.
- [ ] Icons are SVG. All copy is localized in en and zh.
