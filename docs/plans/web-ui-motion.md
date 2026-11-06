# Web UI Motion · Plan

> **Status**: Approved by the owner 2026-11-05 (decisions in section 11).
> **Owner**: cyhhao
> **Design**: `ui/design/frames/motion/` (status `approved`), `ui/design: motion/*`

## 1. Background

The owner wants the Web UI to feel like a native app, and much of that feel is
motion. The Web UI now renders from local caches first
(`docs/plans/web-ui-local-cache.md`), optimistic send and optimistic working
state are being built, and the iOS app will show this Web UI inside a native
`WKWebView` shell (`docs/plans/ios-app.md`), so "close to native iOS" is the bar.

The rule that governs the cache work governs motion too: **nothing pops in from
nothing; state may change, but smoothly; render the best known state first,
then let live correct it.**

Owner constraints carried from earlier design rounds:

- **Restrained.** Motion is added to the existing visual design. It does not
  restyle, relayout, or remove anything (a full visual redesign was rejected,
  including a variant without chat bubbles). Bubbles, cards, colors, and
  spacing stay exactly as shipped.
- **Quality, not spectacle.** The reference is the voice meter: mint, smooth
  interpolation, a stable frame rate, little CPU, and a full fallback when
  "Reduce motion" is on.
- **Chosen by comparison.** Each main moment was drawn in three strengths
  (restrained, standard, pronounced) beside today's behavior, on the same data
  and size; the owner chose standard for phone and desktop alike (section 9).
- **Budget for the iPhone Home Screen app and `WKWebView`,** not for desktop
  Chromium.

## 2. What exists today (inventory, `master` at c2214a537)

**Keyframes in `ui/src/index.css`**

| Name | Use | Timing | Reduced motion |
| --- | --- | --- | --- |
| `slide-in` | Every toast, from the right edge | 200 ms `ease-out`, `translateX(100%)` | none |
| `appwindow-in` / `-out` | App windows (desktop) | 140 / 130 ms | none |
| `vr-typing` | The agent working dots (`ThinkingBubble`) | 1.2 s loop | none |
| `msg-highlight` | Search jump target | 3 s background fade (paints) | none |
| `project-pickup` | Project drag preview | 140 ms | yes |

**tw-animate-css** (`animate-in` / `animate-out` on Radix state): `Dialog`
(overlay fade 150 ms; content fade + zoom 95% on desktop, slide from the bottom
on phones, 300 ms in and 200 ms out on `ease`), `ResponsiveMenu` (the same
sheet), `Popover` (fade + zoom 95% + 2 px slide), and the settings panel on
phones (`slide-in-from-right-4`, 200 ms, `motion-safe` only). None of these
respects reduced motion except the settings panel.

**framer-motion** is used only for drag reordering (`Reorder` in the Dock, the
Library, the Model Hub source order and catalog) and `useReducedMotion`
(project and model order lists, onboarding). There is no `motion.*`,
`AnimatePresence`, or `layout` anywhere.

**Transitions**: 227 bare `transition` (Tailwind's 150 ms on every animatable
property), 139 `transition-colors`, and about 20 different literal durations
(80 to 650 ms) and five curves across components and CSS. The desktop pet
already uses the iOS settle curve `cubic-bezier(.32,.72,0,1)` as `--settle`.
`AppWindow` animates `left/top/width/height` (layout), and `ProgressBar` and
`ModeSelection` use `transition-all`.

**Reduced motion**: about 20 call sites use `motion-reduce:` or `motion-safe:`,
the voice meter has its own static bar, and the onboarding diagram stops
itself. There is no shared policy.

**Voice**: the meter is drawn from refs every animation frame (no React render
per frame), breathes when quiet, goes flat and red when armed, sweeps while
settling, and is one static bar under reduced motion. A finger opens the
microphone at 200 ms with the ring from 100 ms; the desktop composer at 350 ms
with the ring from 100 ms.

**What pops today** (appears or changes in one frame): a sent message (it
lands when the POST answers, after the working dots), the working dots, Send
and Stop, a reply replacing the dots, new rows from the event stream or from a
live answer replacing the cached view, Inbox reorders and unread badges, every
route change, the mobile Apps drawer (no animation at all), the inline error
banner (which also pushes the transcript down), and toasts leaving.

**Taps on iOS** show WebKit's grey tap highlight (`-webkit-tap-highlight-color`
is unset outside PDF links), and no tappable surface has a pressed state.

## 3. Principles

1. **Purposeful.** Motion explains a change: where a thing came from, where it
   went, what replaced what. Decoration and idle motion are out, except the
   shimmer and the working dots, which say "still loading" and "still working".
2. **Fast.** Feedback within one frame of input; most moments finish in
   160–360 ms; nothing over 500 ms except a spring's settling tail.
3. **Interruptible.** A new input or a new state retargets a running animation
   from where it is; it never queues behind it or snaps back first.
4. **Spatially consistent.** A thing leaves the way it came. A message rises
   from the composer and is withdrawn into it; a page pushed from the right
   goes back to the right.
5. **Never blocking input.** State and focus change at once; the motion plays
   over a UI that already accepts input. No moment disables a control while it
   animates.
6. **Render the best known state first.** Cached, optimistic, or skeleton
   first; live corrects it with the same motions as any other change.
7. **Reduced motion respected.** Every moment has a no-motion or crossfade
   fallback (section 7).
8. **Additive.** Motion never changes a resting layout, color, or style.

## 4. Tokens (`ui/src/index.css`, this PR)

Plain custom properties in `:root` (Tailwind emits only the theme variables a
utility uses, and the design kit and future CSS read these directly), with the
curves also exposed as `ease-*` utilities through `@theme inline`.

| Token | Value | Use |
| --- | --- | --- |
| `--motion-duration-instant` | 100 ms | Press feedback; the two halves of a hand-over in a control |
| `--motion-duration-fast` | 160 ms | Small things (dots, badges, icons); every exit; reduced-motion crossfades |
| `--motion-duration-base` | 240 ms | An element within a surface: a message, a row, a popover |
| `--motion-duration-slow` | 360 ms | A surface: a page, a sheet; list moves without a spring |
| `--motion-stagger` | 24 ms | Between rows entering together; at most five rows |
| `--motion-ease-enter` (`ease-enter`) | `cubic-bezier(0.22, 1, 0.36, 1)` | Entering: decelerates into place |
| `--motion-ease-exit` (`ease-exit`) | `cubic-bezier(0.4, 0, 1, 1)` | Leaving: accelerates away, on a shorter duration |
| `--motion-ease-move` (`ease-move`) | `cubic-bezier(0.32, 0.72, 0, 1)` | Something on screen moving to a new place (the existing `--settle`) |
| `--motion-spring-{snappy,smooth,sheet}` (`ease-spring-*`) | `linear()` curves | The springs below, for CSS |
| `--motion-spring-*-duration` | 290 / 440 / 440 ms | The time each spring needs to settle |
| `--motion-nav-push-duration` / `--motion-nav-pop-duration` / `--motion-nav-ease` | 260 / 220 ms / enter | A page pushed over another on a phone, and popped off it by the app's Back (M5) |
| `--motion-beat` | 120 ms | A follower trails its leader (the working bubble after the message) |
| `--motion-offset-sm` / `--motion-offset` / `--motion-offset-lg` | 4 / 8 / 16 px | How far a thing travels as it enters |
| `--motion-drift` | 30% | How far the page beneath drifts the other way on a push |
| `--motion-scale-enter-sm` / `--motion-scale-enter` | 0.98 / 0.96 | The scale a large surface (a card) or an element enters from |
| `--motion-scale-press` / `--motion-scale-press-control` | 0.97 / 0.94 | A pressed surface (card, row); a pressed control (button, chip) |
| `--motion-scale-morph` | 0.6 | An icon trading places with another (Send ↔ Stop, a badge leaving) |
| `--motion-handover-duration` / `--motion-handover-ease` | base / enter | Skeleton or cached view to content (section 6) |
| `--motion-shimmer-duration` (`animate-skeleton-shimmer`) | 1600 ms | A skeleton's highlight sweep (section 6) |

**Springs.** One set of names for framer-motion and CSS. The framer presets
(added in Phase 1 as `ui/src/lib/motion.ts`) are framer's own
`visualDuration` / `bounce` form; the CSS tokens are the same springs sampled
into `linear()` (overshoot included) with the duration each needs to settle
within 0.4%. Safari before 17.2 has no `linear()` and falls back to the nearest
cubic-bezier through `@supports`.

| Preset | framer-motion | stiffness / damping | CSS | Use |
| --- | --- | --- | --- | --- |
| `snappy` | `{ type: 'spring', visualDuration: 0.2, bounce: 0 }` | 685 / 52 | 290 ms | Controls: press release, Send ↔ Stop, badges |
| `smooth` | `{ visualDuration: 0.3, bounce: 0 }` | 305 / 35 | 440 ms | Content: a message rising, rows moving (FLIP) |
| `sheet` | `{ visualDuration: 0.38, bounce: 0.08 }` | 190 / 25 | 440 ms | Surfaces: sheets, drawers |

Existing literal durations move onto these tokens as each phase touches their
component; nothing is migrated in bulk.

## 5. Performance rules and budget

The target device is a mid-range iPhone running the Home Screen app or the iOS
shell's `WKWebView`, where #3256 measured Inbox renders of 100 ms or more under
live traffic before its fix.

- **Transform and opacity only.** They run on the compositor, so they keep
  their frame rate while the main thread renders a live update. Height, width,
  top, left, margin, `clip-path`, filters, and shadows never animate. Color and
  shadow may change on a single small element with a `fast` transition (a
  badge, a pressed card), never across a list.
- **FLIP for anything that moves in a list or transcript.** Measure once
  (all reads, then all writes), apply the inverse transform, and play it with
  the Web Animations API (`element.animate`) so it runs off the main thread.
  Not framer-motion `layout`, which writes a transform every frame from
  JavaScript. Inside a scroller, apply the inverse only after the scroll
  position has been pinned, and never re-pin during the glide: a positive
  transform extends the scrollable area, and `ChatPage` re-pins after every
  commit (`useLayoutEffect(restoreScrollPosition)`), so the pin must ignore
  the running offset until the glide ends.
- **Only what is on screen moves.** Rows outside the viewport plus one screen
  snap. At most 8 elements animate in one moment, and at most 5 rows stagger.
- **Bursts coalesce.** A second update during a move retargets the running
  animation (`effect.updateTiming` or a new FLIP from the current position);
  it never queues. During a burst of live updates (more than 3 in 1 s) list
  moves fall back to the crossfade.
- **No React render per frame.** Continuous motion (the voice meter, a drag)
  writes styles from refs in `requestAnimationFrame`, as the meter does.
- **Layers are temporary.** `will-change` only while an animation runs; never
  on list rows at rest (each layer costs memory in WebKit).
- **No backdrop blur on what moves.** A scrim with `backdrop-filter` may fade
  (opacity), but no element with a backdrop filter translates or scales; on
  phones, the sheet scrim's blur is kept static while it fades.
- **Start after commit.** A moment starts in the frame after React commits
  (`useLayoutEffect` measuring, then `animate`), never inside a render, and
  heavy work it triggers (Markdown, IndexedDB writes) is scheduled after the
  first frame of the motion.
- **Budget per moment**, measured in Chromium with 4× CPU throttling as the
  proxy until a device lab run: no dropped compositor frames, no main-thread
  task over 16 ms during the motion caused by the motion itself, and zero
  style recalculation or layout per frame from the animation. Each
  implementation PR records this, plus one run on an iPhone with Safari Web
  Inspector's timeline (the `WKWebView` is inspectable in development builds).

**Limitation of this design round:** the preview video and the checks behind
it ran in desktop Chromium (Playwright), with frames stepped deterministically,
so the video shows the intended timing exactly but says nothing about an
iPhone's frame rate. The choreography only uses compositor properties, which
is what makes it cheap on WebKit too, but the device numbers come with each
implementation PR.

## 6. Loading and hand-over

The owner's rule for loading (2026-11-05): main surfaces never show "a spinner
first, then content". The skeleton's look is designed by the lists lane in its
own PR (frames `system/skeleton/*`, `inbox/inbox-page/*-loading`,
`chat/skeleton/*`, `system/boot/*`, and the `Skeleton` primitive); this section
owns its motion and the tokens that lane already uses by name.

- **Cache first.** A surface with a snapshot renders it at once, fully
  usable, and refreshes quietly. It shows no skeleton and no loading
  indicator.
- **Skeleton when there is nothing.** Blocks the size and radius of the rows
  they stand for (`bg-foreground/[0.06]`), inside the real card's border, so
  the content lands without moving anything.
- **Shimmer.** One highlight per card at the owner's "light" strength
  (`via-foreground/[0.03]`, decided 2026-11-05), not one per block, crossing it with `animate-skeleton-shimmer`: about 60% of each
  1600 ms period sweeping (`ease-in-out`, transform only), the rest resting.
  Under reduced motion it is still (`motion-reduce:animate-none`): the blocks
  remain, the sweep does not. The highlight rests off its block
  (`[transform:translateX(-100%)]`), so a still one shows nothing; not a
  `translate-x-*` utility, whose separate `translate` property adds to the
  keyframes' `transform` and shifts the sweep.
- **Skeleton to content.** The content fades in where the skeleton was, on
  `--motion-handover-duration` and `--motion-handover-ease` (240 ms, enter),
  and the skeleton goes as it arrives. Geometry does not change, so nothing
  moves; a row count that differs is absorbed by the rows below gliding (FLIP).
  Under reduced motion the content replaces the skeleton in one frame, which
  is safe because nothing moves.
- **Cache to live.** Rows with the same identity stay where they are and do
  not animate. A row that is new enters as a reply does (section 8, M3); a row
  removed leaves as an archived row does (M7); a changed order uses the Inbox
  move (M7). When the live answer differs too much to describe as row changes
  (more than 5 rows, or the scroll anchor is gone), the whole list cross-fades
  on the hand-over tokens. Chrome never changes (`web-ui-local-cache.md`
  invariant 4).
- **Loading more.** No spinner. At the end of a list, one or two skeleton rows
  in the list's own row shape, with the shimmer, replaced by the rows on the
  hand-over. Older messages at the top of a conversation keep the existing
  32 px slot (`h-8`) and show a slim skeleton bar there instead of `Loader2`,
  so the anchor never shifts.
- **Retry.** A failed load shows its message in the same slot the skeleton
  held (no layout change), and the retry starts the skeleton again.

## 7. Reduced motion

`prefers-reduced-motion: reduce` gets each moment's fallback, never a global
kill switch (the dots, the shimmer, and the voice meter have their own still
forms, and a progress indicator may still turn).

- Anything that travels (translate, scale, a push, a sheet) becomes a
  crossfade on `fast`, or no animation where a crossfade would only flash.
- Press feedback keeps its color change and drops its scale.
- The working dots hold still at mid opacity; the shimmer stops; the voice
  meter keeps its static bar.
- Implementation reads one hook (`useMotionPreference()` in Phase 1, built on
  framer's `useReducedMotion`) for JavaScript-driven motion, and
  `motion-reduce:` for CSS.

Every moment's reduced form is drawn in at least one frame (`*-reduced`).

## 8. Catalog

Priority is perceived impact on a phone: P1 is the conversation (the most
frequent action), P2 how the app moves between places and answers a touch, P3
lists and notices. Each moment is the approved standard strength, with its
reduced-motion form.

### M1 · Optimistic send (P1)

- **Trigger**: Send or Enter accepted locally; the row is appended before the
  POST (optimistic send lane).
- **Motion**: the Send button sinks to `--motion-scale-press-control` under the finger (`instant`).
  The transcript, already pinned to its new bottom, glides up off its FLIP
  offset on `spring-smooth`, so the new message emerges from behind the
  composer's top edge; the message itself grows from `--motion-scale-enter` at its
  bottom-right corner and rises the last `--motion-offset`, fading in on `fast`. The
  composer hands over in two beats so the texts never overlap: the typed text
  and Send leave (`instant`, exit), then the empty composer and Stop arrive
  (`fast`, enter).
- **Failure**: the working bubble and Stop leave first (`instant`); the
  message sinks back under the composer as the transcript glides down (`base`,
  move; the message fades on `base`, exit); the composer shows the text and
  Send again. The existing inline error fades in on `fast` (it still inserts
  above the transcript; changing that is a layout decision outside this plan).
- **Not pinned** (the reader is up in the history): no glide; the
  jump-to-latest control appears (`fast`).
- **Reduced**: the transcript cross-fades to its new state on `fast`.
- **Components**: `ChatPage` (`Transcript`, `MessageRow`), `Composer`, the
  optimistic row.
- **Frames**: `motion/send/phone`, `phone-reduced`, `phone-failed`, `desktop`.

### M2 · Send ↔ Stop, and the working bubble (P1)

- **Send → Stop**: Send scales to `--motion-scale-morph` and fades (`instant`,
  exit), then Stop grows from it on `spring-snappy` while it fades in (`fast`). The queue-send
  button that appears beside Stop when there is text enters the same way.
  Stop → Send is the mirror.
- **Working bubble**: enters one beat (`--motion-beat`) after the message, rising
  `--motion-offset-sm` and fading in on `fast`; it leaves by fading where the
  reply grows over it (M3).
- **Reduced**: crossfade on `fast`; the dots hold still.
- **Components**: `Composer`, `ThinkingBubble`.

### M3 · A reply arriving, and streamed rows (P1)

- **Motion**: the working bubble fades out (`instant`) while the reply grows
  in its place from its top-left corner (`--motion-scale-enter`, `spring-smooth`, fading on
  `base`), and the transcript glides up by what the reply adds. Stop hands back
  to Send (M2). Interim rows and activity rows enter the same way, one glide
  per 100 ms at most; a row that grows while it streams is not animated (its
  height change is followed by the pinned glide).
- **Reduced**: crossfade on `fast`.
- **Frames**: `motion/arrival/phone`, `phone-reduced`.

### M4 · Cache → live hand-over (P1)

Section 6. Chat: M3's arrival for new tail rows, M7's leave for removed rows.
Inbox: M7's move. Frames: `motion/arrival/*`, `motion/inbox/*`.

### M5 · Navigation (P2)

- **Phone, hierarchy** (Inbox, Projects, Search → a conversation): the tapped
  card sinks to `--motion-scale-press`; the conversation slides in from the
  right edge while the page beneath drifts `--motion-drift` the other way, as a
  native push does, and the app's own Back reverses it. The pace is a native
  navigation's: `--motion-nav-push-duration` (260 ms) and
  `--motion-nav-pop-duration` (220 ms) on `--motion-nav-ease` (the `enter`
  curve), one decelerating curve with no settle. Built on same-document View
  Transitions (`document.startViewTransition`, Safari 18 and Chrome 111+),
  named per page; where they are missing (iOS 17), navigation stays a cut
  rather than a JavaScript slide.
  - Input is never held: any pointer or key input during a transition calls
    `skipTransition()`, and the transition is at most 260 ms.
  - **A pop animates only when the app's own Back started it** (the header's
    Back, or another in-app back affordance calling `navigate(-1)`). Every other
    history step (the system edge swipe, the browser's Back or Forward, a
    keyboard shortcut, `history.back()` from anywhere else) is a cut with no
    View Transition at all, whatever `hasUAVisualTransition` says, and no
    transition starts for a moment after it unless the reader acts. The app
    marks its own Back right before it navigates; nothing is inferred from
    event properties.
- **Phone, tabs** (the tab bar): no slide, as on iOS; at most a `fast`
  crossfade of the content.
- **Desktop**: no slide. Switching conversations cross-fades the chat column
  on `fast` with the composer in place; the sidebar's selection moves (FLIP,
  `spring-snappy`) instead of jumping.
- **Reduced**: crossfade on `fast`.
- **Frames**: `motion/navigation/phone-push`, `phone-push-reduced`.

### M6 · Sheets, drawers, popovers, menus (P2)

- **Phone sheets** (`Dialog` and `ResponsiveMenu` on phones, the new-session
  sheet, the Apps drawer, the Bot switcher): rise from the bottom edge on
  `spring-sheet`; the scrim fades on `base`. They leave faster: `base`, exit.
  The Apps drawer, which has no motion today, joins them.
- **Drag to dismiss**: from the handle or the sheet header (or anywhere once
  its content is scrolled to the top). The sheet follows the finger 1:1, with
  resistance above its resting place; on release it dismisses when pulled past
  30% of its height or moving down faster than 500 px/s, carrying the finger's
  speed out, and otherwise springs back on `spring-snappy`.
- **Desktop popovers and menus**: fade and grow from `--motion-scale-enter` at the trigger
  (Radix's `--radix-*-transform-origin`) on `fast`, enter; leave with a fade on
  `fast`, exit. Today's 150 ms zoom moves onto the tokens.
- **Desktop dialogs**: fade and grow from `--motion-scale-enter` on `base`; the scrim on `fast`.
- **Reduced**: fade only, on `fast`.
- **Frames**: `motion/sheets/phone`, `phone-drag`, `phone-reduced`.

### M7 · Inbox rows (P3)

- **Move to the top** (a new reply): the card leaves its old slot (fades on
  `instant`), the cards above glide down into the room (`spring-smooth`), and
  the card comes in at the top with the new reply, from `--motion-offset`
  above and `--motion-scale-enter-sm` (`spring-smooth`, fade on `base`). Inbox cards are tinted, not
  opaque, so a card never travels across the others.
- **Insert** (a new conversation): the rows below glide down; the card comes in
  at its slot as above.
- **Unread cleared**: the count badge shrinks to `--motion-scale-morph` and fades (`fast`,
  exit); the card's mint wash eases to the read style with a `fast` color
  transition (one card).
- **Archive or removal**: the card fades and shrinks to `--motion-scale-enter-sm` (`fast`, exit),
  then the rows below glide up (`spring-smooth`). Height never animates.
- **Reduced**: crossfade on `fast`.
- **Frames**: `motion/inbox/phone-move`, `phone-move-reduced`.

### M8 · Press feedback and quick replies (P2)

- **Every tappable surface** (buttons, cards, rows, tab bar items, chips) gets
  a pressed state on `:active`: cards and rows sink to
  `--motion-scale-press` and buttons and chips to
  `--motion-scale-press-control`, each with a color step (a pressed fill one
  step darker, or the surface's hover fill).
  Press is `instant`; release is `spring-snappy`. On touch the pressed state
  waits 60 ms inside a scroller so a scroll does not flash it. WebKit's grey
  tap highlight is turned off where a pressed state exists.
- **Quick reply**: the chip presses as above; on choice it fills (its shipped
  chosen style, `fast` color) while the others fade to their locked state, and
  the user message rises as in M1.
- **Reduced**: the color step stays; the scale goes.

### M9 · Toasts (P3)

Toasts rise from below their stack (phone: above the tab bar) by
`--motion-offset-lg` on `spring-smooth`, fading on `fast`; the stack moves up
with FLIP; a toast leaves with a fade and a `--motion-offset` drop on `fast`,
exit, and can be swiped sideways on a phone. Today they slide in from beyond
the right edge and vanish on dismissal. Reduced: fade.

### M10 · Voice composer (P3, alignment)

The shipped voice design stays (meter, thresholds, reduced-motion bar). The
hold card enters from `--motion-scale-enter` on `spring-snappy` with its halo fading on `base`,
the cancel target changes color on `fast`, and its existing literals move to
the tokens. No new motion.

### Smaller moments

The jump-to-latest control (fade and `--motion-scale-enter` → 1, `fast`), queue rows (enter like a
message, leave like an archived row), Harness group expansion (content fades in
on `fast`; no height animation), the search jump highlight (kept), and the
error banner (fade on `fast`).

## 9. Strength

The owner compared three strengths side by side on the same data (the preview
video and frames of 2026-11-05) and chose **standard**, the same on phone and
desktop: iOS-like springs without visible overshoot, full-distance pushes and
sheets, small scales. Restrained (fades and short travel only) and pronounced
(longer travel, a 2% overshoot spring, receding pages) were not chosen; their
frames and the tokens only they used were removed.

## 10. Phases

Each phase is one PR, with its frames moved to `shipped`, its reduced-motion
test, the Chromium 4× measurements in section 5, and a place for the device
numbers. Delivery order (2026-11-05), to stay clear of other work in flight:
phase 2 and phase 3 first; phase 4 after the list skeleton PR (it also changes
the Inbox and the sidebar); phase 1 after the chat skeleton PR and optimistic
send (they change `ChatPage`).

1. **Conversation (P1)**: `ui/src/lib/motion.ts` (springs, `useMotionPreference`,
   a small WAAPI FLIP helper), then M1–M4 in `ChatPage` and `Composer`. Lands
   on top of the optimistic send lane.
2. **Navigation and touch (P2)**: View Transitions push and pop on phones, the
   desktop column crossfade, M8 press feedback (a shared pressed-state class on
   the primitives), the tap highlight.
3. **Sheets and overlays (P2)**: M6 for `Dialog`, `ResponsiveMenu`, the Apps
   drawer and the Bot switcher, drag to dismiss, popover and menu tokens, M9
   toasts.
4. **Lists (P3)**: M7 in the Inbox page, the sidebar Inbox popover and the
   project tree, and the loading hand-over (section 6) where the skeleton
   lanes have not already adopted the tokens.

## 11. Decisions (owner, 2026-11-05)

1. **Strength**: standard for every moment, on phone and desktop alike.
2. **Phone push**: View Transitions on iOS 18 and later; on iOS 17 navigation is
   a cut, never a JavaScript slide.
3. **Drag to dismiss**: phone sheets support it (M6).
4. **Pressed state**: cards and rows sink to 0.97 (`--motion-scale-press`) and
   buttons and chips to 0.94 (`--motion-scale-press-control`), each with a
   color step (M8).
5. **Native back swipe**: the system edge swipe always wins. Our pop animates
   only from the app's own Back; a system traversal (an edge swipe in Safari,
   the Home Screen app or the iOS shell, the browser's Back) is a cut with no
   web transition (M5; owner feedback 2026-11-06).

## 12. Known by design

- Frames are static; each moment is a looping CSS animation over the shipped
  page in each state. The moments are kit components (`ui/design/kit/motion/`);
  the frames own the data and pass it to them. Every proposed motion value
  (duration, curve, offset, scale, opacity, beat) is read from the tokens. A
  frame states three kinds of number that are not motion design: when in its
  loop a moment happens, the FLIP offsets (the row heights the shipped page
  draws for that data, which FLIP measures at run time), and a demo gesture's
  finger path.
- The chat frames stack two or three shipped pages; the phone chat is an
  opaque sheet, so a stacked page leaves its sheet background off
  (`seeThrough`) to let the composer cross-fade. Nothing in the product changes
  for this.
- Moments that need interaction state the frames cannot hold (toasts, the
  pressed state on every primitive, desktop popovers) are specified here and
  drawn when their phase lands.

## 13. Delivery notes

### Phase 1 (conversation)

- **The glide leaves the scroller alone.** The transcript still pins its
  bottom and holds the reader's anchor exactly as before (#3286). The motion
  (`src/lib/transcriptMotion.ts`) measures how far the last row that stays
  moved on screen, from just before React changes the document
  (`getSnapshotBeforeUpdate`) to after the pin, and moves the content column
  off that offset and back (FLIP with the Web Animations API, transform
  only). It glides only while the reader follows the tail. What changes above
  that row (a saved page replaced by the server's, a row that grows) does not
  move it on screen, so it glides nothing; the pin's own scroll delta would
  have counted it. The column sits in a
  box that clips it (`overflow-y: clip`, which took over the transcript's
  vertical padding), so a glide's offset never adds to what the transcript can
  scroll: the anchor and follow-tail see the layout they would see with no
  motion, and nothing in the motion writes a scroll position.
- **What enters** is what is new at the tail, compared by element rather than
  id: the server's row that takes a sending row's place, and a saved window
  the live answer replaces, keep their elements and do not enter again.
- **What leaves.** React has removed a row before an effect can see it, so a
  withdrawn send and the working bubble are drawn for their exit by an inert,
  hidden copy of their last frame, removed when the exit ends.
- **The reply takes the bubble's place.** The working bubble and a reply
  draw the same head (the avatar and the name) in the same place. When a
  reply replaces the bubble, the head stays still: only the bubble's dots
  fade, and only the reply's body grows in. Crossfading both whole rows drew
  the head twice, slightly apart, for the length of the fade.
- **Limits.** More than five rows at once is a hand-over (M4): they fade on
  the hand-over tokens, with no glide. After three glides in one second (a
  burst of live rows, a fast stream), further rows land without one, and a
  move longer than the viewport is never a glide. A row that grows while it
  streams is followed by the same glide, under the same limit.
- **Not pinned.** When the reader is up in the history nothing glides. Sending
  from there still takes the reader to the new message, as optimistic send
  (#3261) shipped it; the message's entrance plays where it lands.
- **The composer.** When Send and Stop trade places, the control that arrives
  grows in where the other was, and the one that leaves goes at once (it is
  unmounted, and the two share a place, so the trade reads as one control
  changing). Each control is keyed as its own element; unkeyed, React turned
  Stop's element into Send, which kept its finished animation and only
  shifted colours. Nothing morphs as the composer first draws; the queue-send button
  beside Stop always morphs in.
  The typed text clears at once instead of in the two beats M1 describes: the
  message rises from behind the composer's top edge, so the two never overlap.
- **The queue strip** fades in, and a message queued from this tab rises into
  it; the server's queued row keeps that entrance. The inline error fades in;
  the jump-to-latest control grows in.
- **Not animated yet.** Rows a live refresh removes (other than the two exits
  above) still go at once; an activity card's own expansion is unchanged.
- **Frames.** `motion/send/*` and `motion/arrival/*` render the shipped page;
  a frame's `fixtures.stream` delivers event-stream events on a clock (the
  reply arriving).
- **Tests.** `e2e/chat-scroll/motion.spec.ts` (in `test:chat-scroll`) holds
  each moment to the scroller's guarantees: one pin write, the tail still
  followed, and travel spread over many frames rather than one.
- **Cost.** The motion adds 2–3 ms of script (Chromium, 4× CPU, a 350-row
  conversation) to the commit that starts it. It reads every token it needs
  before it writes anything, so that commit recalculates style once, not once
  per token. While any animation runs, Chromium recalculates style about every
  other frame even for compositor animations (a lone opacity fade does too);
  over a whole moment that is 3–4 ms at 4×.

### Phase 2 (navigation and touch)

- **Where a push happens.** On a phone, a push or pop runs between a
  conversation and the lists it is opened from: the tab pages (Inbox,
  Projects, the capability pages), Search, and the home. A conversation and
  another full-screen surface (a Show Page, an app, Settings) stay a cut, as do
  tab switches; one conversation replacing another cross-fades on any screen.
  The decision lives in one place, the router (`src/lib/navigationMotion.ts`),
  so every caller that opens a conversation gets it.
- **The tapped card.** An Inbox card is not itself a tap target; its Open
  button is, and it presses as a control (0.94) before the push.
- **The system back swipe.** The first cut trusted `hasUAVisualTransition` to
  tell a system swipe from our Back. On the owner's iPhone, in the Home Screen
  app, the conversation flashed back after an edge swipe: WebKit sets the flag
  only while its swipe animation is still running, and the router replays a
  View Transition on every POP whose push had one, deferring the new page so
  the snapshot it takes is the conversation the reader just left. Now the app
  marks its own Back, every other history step is a cut that skips the View
  Transition entirely (the router renders the list in the same task), and no
  transition starts for a moment after it. The pace also changed from the
  sheet spring (440 ms with a long settle) to `--motion-nav-*` (260 / 220 ms,
  `enter`), the fastest of three paces recorded side by side, chosen by the
  owner. The iOS shell's bridge exposes no gesture capability. Its primary
  runtime now enables `allowsBackForwardNavigationGestures` only when the
  immediate back item belongs to the selected instance; the Web rule above
  already treats that system traversal as a cut, so no second motion path is
  needed.
- **A relaunch** that restores the last page (the Home Screen app after iOS
  evicted it) lands without a push.
- **Press.** `press` (controls) and `press-surface` (rows and cards) in
  `src/index.css`, on `Button`, the segmented control, the tab bar, the Inbox
  card's buttons, Projects rows, and Search results. Other tappable surfaces
  adopt the same two classes as they are touched. On a touch screen only
  surfaces wait `--motion-press-delay` before showing pressed; a control
  answers at once. A control that blurs what is behind it does not scale
  (`[--press-scale:1]`, section 5).
- **Deferred to phase 4**: the desktop sidebar's selection moving between
  conversations (FLIP), since that phase changes the sidebar.

### Phase 3 (sheets and overlays)

- **One set of classes.** `motion-sheet` (a phone sheet), `motion-dialog`
  (that sheet on a phone, a dialog that fades and grows on a desktop),
  `motion-scrim`, and `motion-popover` in `src/index.css`, keyed on the
  `data-state` Radix sets; Radix waits for the closing animation before it
  unmounts. `Dialog` (with it the Bot switcher and the new-session sheet),
  `ResponsiveMenu`, and `Popover` use them, and so does the Apps drawer,
  which keeps itself mounted through its closing animation.
- **Drag to dismiss** (`src/lib/sheetDrag.ts`) on the `Dialog` sheet, the
  `ResponsiveMenu` sheet, and the Apps drawer. A touch claims the sheet from
  its first downward move, where nothing under it scrolls (the handle and the
  header) or from content scrolled to its top; fields keep their own touch.
  Dismissing closes the sheet as its close button does; an owner that keeps
  it open gets it back on `spring-snappy`.
- **The Apps drawer** moves inside a frame that ends at the tab bar's edge, so
  it rises from behind the bar and sinks back behind it. Its two inner sheets
  (a tile's actions, the footer's overflow) rise in; they leave with the cut
  they had, since each unmounts with the choice that closes it.
- **Toasts** (`ToastProvider`) move with the Web Animations API. A toast that
  joins a stack waits one `--motion-beat` while the stack lifts, so the tinted
  cards never cross. A finger swipes one away sideways. Voice toasts are a
  separate component and are unchanged.
- **Not migrated**: the Model Hub menu drawer, the search palette, and the
  phone Settings panel keep their own animations until they are next touched.
- **Measuring.** A probe that runs a `requestAnimationFrame` loop makes
  Chromium recalculate style on every frame while any animation runs; count
  style and layout without one. Counted that way, the phase 2 push and pop
  run 3–4 style passes over 420 ms and no layout.
