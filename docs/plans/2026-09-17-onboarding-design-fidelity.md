# Onboarding design fidelity repair — issue #2017

PR #2015 merged successfully but did not faithfully reproduce the approved Welcome and assistant-setup design. The owner rejected the visual fidelity on 2026-09-17 and explicitly identified the existing animated Show Page as the implementation reference. At 16:55 the owner additionally required the designed widths and adaptation to different screens.

## Authority and scope

- Static appearance, copy, dimensions, and state presentation: `avibe-docs/design_desktop.pen`, read only through Pencil. The latest design wins if the older prototype differs.
- Welcome animation reference: https://max-app.avibe.bot/p/SM2vv4wGlM4/ (existing source is available locally; reuse its authored sequence and visual details rather than approximating it).
- Fix forward from current `master` after #2015. This is the visual correction of #2010. Keep existing logo assets, shared React Web/Desktop ownership, and existing setup/auth/config behavior.
- The separate #2011 authentication/readiness and #2012 Workbench/General issues retain their behavior scope. #2013 native desktop integration remains later. No new auth model, fabricated connected state, native window controls in Web, service restart, or production-state testing.

## Confirmed gaps in the merged source

- Document and code content were flattened into generic bars; document paragraphs/footer and code indentation/multiple segments are missing. Reveal, completion, and check-mark timing differ from the reference.
- The return wire loses its rounded route, ports, glow, and traveling pulse treatment. Return caption does not change with the cycle. PM content is reset at the summary phase.
- Theme fills/borders/shadows and internal spacing were approximated. A 240x228 outer card alone does not establish design fidelity.
- Narrow screens switch the collaborative diagram into a long vertical stack and hide the electric feedback loop, unlike the provided responsive prototype.
- Page/container widths, header composition, access-tile geometry, and setup-row alignment require an explicit measured comparison, including their interaction states.

## Acceptance contract

1. Every shared surface delivered by #2010 is compared to its corresponding final design state at the same effective content viewport. Record native-titlebar exclusion explicitly, retaining the designed Web content geometry. No arbitrary max-width, global zoom, or screenshot-only CSS may stand in for the designed layout.
2. At the desktop reference, collaboration is 880x272, each card 240x228, access area 640 wide, primary Welcome action 144x44, and assistant rows 880x88. **Superseded on 2026-09-17 by the owner's accepted 1104 cap — see "Accepted width" below; the frames' 880 numbers remain the source proportion.** Read precise text, spacing, radii, borders, fills and effects from Pencil, including Light values. Larger viewports preserve designed component widths and center the content. Smaller/shorter screens adapt without clipping, horizontal overflow, overlapping labels, disconnected wires, or unreachable controls; keep the collaborative sequence legible.
3. Welcome reproduces the authored 8.9-second loop: PM 0–1.5s; first handoff 1.5–2.25s; Codex 2.25–3.75s; second handoff 3.75–4.5s; tests 4.5–6s; return 6–7s; summary 7–8.9s. Source remains active in transit; destination activates on arrival. Document/code reveal, sequential test ticks, 850ms progress circle, 300ms hinge check, 250ms caption fade, wire ports/glow, and return caption follow the design/reference. Completed PM work stays complete during summary.
4. ~~Pause/replay synchronize all animated layers~~ — **superseded on 2026-09-17 18:26 by the owner: the design has no playback controls, so none ship.** Hidden/offscreen content suspends nonessential work; reduced motion gives a complete static narrative. Access-tile interaction and emphasis follow the reference and preserve keyboard use. See "Playback controls removed" below.
5. Dark/Light and Chinese/English use the same faithful component geometry. There is no separate Light English frame: combine the approved English copy and Light visual specification. Existing logos remain unchanged. Existing configuration, detection, installation, synchronization, navigation, and recovery invariants stay intact.
6. Evidence includes design exports, actual product screenshots, a same-size side-by-side or overlay comparison, measured geometry, and captured motion phases. Inspect desktop, intermediate, wide, narrow and short viewports; the representative matrix includes 1200x800, 1440x900, 1024x768, 768x1024, 390x844 and 320x568. A test count or a no-overflow assertion alone is not visual acceptance.
7. Before PR delivery, PM independently checks rendered fidelity and one consuming regression test. Run focused behavior/motion/i18n tests, UI build/lint, theme/catalog checks and the scoped Impeccable detector. Changes stay hermetic with mocked APIs; never claim live OAuth or real installation coverage from mocks.

## Reference frames

Welcome: `bi8Au`, `J0qaZ`, `P8iLm0`.
Setup: `i3vw9`, `lm7mN`, `ktRgC`; hover `mK2JX`, `ZaXoW`, `X7N2Z`; awaiting connection `ICidc`, `S3mLYc`, `HR3ps`; upgrade `I9Q8VD`, `pHiuw`, `QPdWU`; connected `F8RLgR`, `r1IQ0B`, `xNmhX`.

Installed/connection controls that depend on #2011 must be explicitly recorded as that issue's outstanding behavior, never presented as visually accepted by this correction. This issue restores the reachable #2010 surface and existing presentation props without inventing readiness.

## Delivery

One isolated task worktree and a non-draft PR to `master`; owner visual acceptance is required before declaring design fidelity complete. Exact-head Codex pass, all required CI green, zero unresolved threads, and review-loop circuit breaker remain mandatory. **Do not manually post `@codex review`: cyhhao triggers automatically; report any verified automation gap.** No automatic merge; the prior authorization applied to #2015 only.


## Execution contract

Owner session: `sestqz5wvu5ty`. Branch: `fix/onboarding-design-fidelity`.
Baseline: `0d3e44de245e109b3e5e2dd98ce85c9b5e9a21f4` (latest origin/master at dispatch).
This plan supersedes the visual acceptance claim for merged #2010, not the deferred behavior boundaries of #2011–#2013. Implementation is authorized by the owner's correction; no repeat permission request is needed.

Source evidence (read-only):
- `/Users/max/workspace/ai/avibe/avibe-docs/design_desktop.pen`
- `/Users/max/.avibe/show/ses36vg559de2/src/pages/index.tsx`
- `/Users/max/.avibe/show/ses36vg559de2/src/welcome.css`
- `/tmp/onboarding-visual-repair/frames.json` and `design/` — freshly exported by Pencil on 2026-09-17 around 17:00. Check live source if a conflicting change is observed.
- `/tmp/issue2010-pencil.py` — working read-only stdio helper; initialize, notification, then 2s client registration delay. `execute` may use only Get/Print/Export/TakeScreenshot for this task. Do not parse or save the .pen raw file.

The prototype is an animation reference, not authority for stale product layout: the latest Pencil Welcome has no centered extra brand row, and its access area differs from the prototype's horizontal padding. Preserve existing logo assets wherever used; do not introduce the prototype Radio placeholder as a new Avibe logo. Shared Web omits simulated macOS window controls; compare the shared content rectangle explicitly instead of shifting all measurements silently.

### Animation origin session and precedence

On 2026-09-17 17:10 the owner named Session `ses36vg559de2` as the animation origin
("引导页动画实现在 ... 这个会话，你去参照"). Its local source at
`/Users/max/.avibe/show/ses36vg559de2/src/` is that Session's implementation and is
already the animation reference in this plan. Precedence, highest first:

1. Latest `design_desktop.pen` frames — static geometry, copy, fills, borders, radii, state presentation.
2. Session `ses36vg559de2` source — motion sequence, skeleton shapes, glyph and pulse behavior, calibrated glow.
3. Older prototype layout — superseded wherever 1 or 2 disagree.

Owner decisions carried from that Session's history (`/tmp/onboarding-visual-repair/reference-owner-decisions.json`,
96 messages; motion refinements in `reference-session.json`):

- Horizontal three-card PM/developer/tester story with authored document and code skeletons,
  progressive line reveal, and ordered test checks.
- Final durations: each work step <=1.5s, horizontal pulse 0.75s, return 1s, whole loop 8.9s.
  Earlier 12s / 2s / uniform-1s versions are superseded.
- Each handoff pulse travels once and disappears on arrival; no static arrows. The destination
  highlights and begins internal work only after the pulse lands; the source stays active in transit.
- The working glyph is a drawn green ring, not the earlier solid dot with a bubbling halo. The same
  ring stays mounted through completion, closes, then the check grows elastically from its hinge
  toward both ends. The ring is about 12px inside a 14px glyph. The status caption fades over 250ms
  without interrupting the ring.
- The active card carries a visible exterior green glow; an inward or too-subtle shadow was rejected.
  Use the browser-calibrated glow, not a naive transfer of Pencil's shadow spread. The Light pulse
  stays light mint rather than turning dark.
- Six access tiles use the actual product icons with Avibe App first. Hover scales the icon to 1.18
  and fills the tile at about half the border's opacity; idle randomized emphasis pauses on pointer
  entry and keyboard use stays intact. No outer wrapper border. The CTA itself stays still and only
  its arrow moves 4px right.
- The 2026-09-17 design removes the header and centered brand logos from the guidance screens
  ("这些引导页的logo都去掉") and widens the six access tiles so their outer edges align with the
  left and right vertical legs of the return wire ("左右侧和左右的这2条线对齐"). Existing product
  logo assets stay wherever a logo still belongs.
- The setup heading matches the Welcome heading in size, style, and position; setup content matches
  Welcome's width; row hover uses the Welcome glow.
- In Light, green-button text and icons are both white.

Unrelated logo exploration, desktop-shell implementation, and historical manual review triggers from
that Session are out of scope. The owner's no-manual-`@codex` rule still applies.

### Width and screen adaptation

Owner on 2026-09-17 17:14: "你要考虑不同大小屏幕的适配，是固定宽度还是？". The answer is a
capped fluid layout with a responsive internal arrangement — not a globally fixed 1200px page and
not a transform/zoom scale-down.

- 1200x800 is the design reference viewport, not a forced window size. Wider monitors gain
  surrounding whitespace; the cards never stretch to fill them.
- Below the cap, containers are `width:100%` with that max-width plus explicit page
  gutters (40px desktop, 24px tablet, 16px phone: 390 -> 358 usable, 320 -> 288 usable). No
  page-level min-width, so there is no horizontal overflow. The card grid uses percentage tracks
  (27.272727% with space-between), so card width, handoff gaps, and the stretched wire viewBox stay
  proportional at every container width without a separate rule per size.
- Intermediate widths adjust gaps and spacing before shrinking content. At phone widths the
  collaboration keeps its compact horizontal narrative, with wires and ports still attached to real
  card geometry. Card-internal labels wrap or stack and the diagram grows taller rather than
  shrinking text to an unreadable size; the caption stays at or above 11px instead of the
  prototype's 7px.
- Access tiles drop from three columns to two on narrow screens with comfortable targets. Assistant
  setup rows reflow status and actions onto another line, and the desktop 88px row height becomes
  content-driven on small screens.
- Short windows scroll naturally and keep the primary action reachable; no `overflow:hidden`
  clipping and no global scale-down. Content taller than the viewport is never cut off above the
  fold. **Corrected 2026-09-17 18:36: the welcome is vertically centred and the setup is
  top-anchored, because the frames anchor them differently — see "Vertical rhythm" below.**

If a full label cannot stay readable at the minimum width, report the concrete constraint and the
smallest layout adaptation rather than hiding information or treating no-overflow as a pass.

### Accepted width: 1104

Owner on 2026-09-17 17:38: "用1104". This accepts PM's recommendation and supersedes both the
frames' 880 and the unaccepted 1240 candidate. The frames stay the source of proportion; 1104 is
the cap the whole composition is drawn at.

- One shared `--ob-content-w: 1104px` caps the header, the Welcome story and the assistant rows,
  so header and body can never drift apart.
- Everything else is that cap times its authored ratio to 880, never a re-typed number: access area
  `640/880` -> 802.9, cards `27.272727%` -> 301.1 with 100.4 of handoff space, diagram
  `880:272` -> 1104x341.2, cards `228/272` of the diagram -> 286.0.
- The diagram is sized from its own width (`aspect-ratio`), not from the cap, so the proportion
  holds at every width between the gutters and the cap and the stretched `880x272` viewBox keeps
  every port on its card edge. Widening the cap alone would have detached the wires.
- Card interiors are drawn in design units (`--ob-u: 100cqw / 880`): padding, skeleton rhythm, bar
  and check sizes grow with the card, while text, icons and controls keep their authored sizes. A
  card a quarter taller must not read as a zoomed screenshot, and must not leave a hole under its
  skeleton either.
- Assistant rows widen to 1104; their 88 height stays because the row's contents fit it, and it is
  a `min-height` on the row itself (border included), not a multiplication. Everything inside the
  row keeps its authored size and offset, so the extra width lands in the identity column only.
- Below 960 the bands keep their hand-chosen diagram height and the authored interior rhythm.

### Vertical rhythm

The design's 1200x800 frames include the native 44px titlebar, so the web content box they describe
is 1200x756.

**Corrected 2026-09-17 18:24 (PM, from the live file).** Both frames show the heading block 65px
below the content edge, but only one of them *specifies* it. `JuwcG` and `KIxnF` centre the welcome
inside their frame, so its 65 is a RESULT of that frame's height; `IWQi6` tops the setup out at its
own 64.5 padding, so that 65 is the position itself. The product reproduces the setup's number as
20px shell padding plus the 45px language bar, and centres the welcome in what is left. A test that
pinned the welcome's y to 65 was asserting the one window height where the two happen to agree, and
has been replaced by a centring assertion plus the setup's own fixed top.

Centring is `align-self: center` inside a `flex: 1 0 auto` content box, which is what keeps a
composition taller than the window from being pushed off the top: the box grows to its content, the
slack to share becomes zero, and the shell scrolls from the content edge.

Measured from the frames rather than the exports, both screens run one rhythm from that edge:

- Welcome (`bi8Au`): heading 79, 24, collaboration 880x272, 24, access 640x160, 24, action 144x44,
  then 64 of slack. The heading is a 34/1.35 title, a 10 gap and a 14/1.57 subtitle; a browser
  stacks the same type to 77.9 because the design rounds each text box up on its own.
- Setup (`i3vw9`): the same heading block, 24, a 29 tall section bar, 12, then 88 tall rows on a
  100 pitch, 24, and a footer that spaces action, hint and Back by 10. Inside a row: a 44 well 20
  from the outer edge, identity 16 further in, status and action against the opposite 20.
- The setup frame's OUTER container starts at x=48 of the 1200 frame and is 1104 wide, but the
  rows and content inside it are drawn at 880 like the welcome's. 1104 is therefore the design's
  own outer container, not its content width; the content reaches it only by the owner's amendment,
  and 48 is the gutter at which that cap is met.

The amendment changes exactly one term of that rhythm: the collaboration is 341.2 rather than 272,
so the welcome needs 781 of content height where the design needed 692. A 1200x800 desktop window
has a 756 content box, so something has to give.

**Corrected 2026-09-17 18:36 (PM).** It is not the spacing. There used to be a `max-height: 760px`
band that took the 24 gaps to 16 and halved the access block's padding; it bought about 30px and
spent the design's own composition to do it, at the reference's own frame size, where nothing was
wrong. That band is deleted. Whitespace IS the composition, so a short window scrolls: the type,
the icons, the buttons, the 24px gaps, the 88px assistant rows and their 12px spacing are identical
at every height, and only width adapts.

### 2026-09-17 18:23-18:36 repair disposition

PM held the first repair before push and issued a bounded correction list; the owner overruled one
part of it directly. This section is the source-backed disposition and replaces any earlier claim in
this plan that contradicts it.

#### Playback controls removed (owner, 18:26)

"不需要暂停和播放按钮，设计图里都没有呀". Every user-facing pause / play / resume / replay control
is gone, together with its reserved layout space, its state and handlers, and its copy and styles.
No substitute control replaces it. The loop starts with the screen and owns itself.

What stays is lifecycle only, because the browser — not the user — asks for it:

- `useOnboardingMotion()` returns `running` from a reduced-motion preference, document visibility
  and whether the composition is on screen. It has no setter and no UI.
- A hidden tab stops both halves of the motion together: the React clock the cards are read from,
  and the CSS animations the shimmer and the test ticks run on. Stopping only the first would leave
  the second playing on unseen and reappearing mid-sweep.
- Being scrolled out of sight stops the same two halves, because a visible document is only half
  the question: on a short window the diagram sits entirely above the viewport while the user reads
  the button below it. The hook observes the element each consumer attaches its ref to, so the story
  and the access tiles suspend independently: both are normally on screen together at desktop sizes,
  while a short or narrow window can give them different intersection. `threshold: 0` is the
  reading the incumbent observer in `vault-chat-requests` takes, so any intersecting pixel counts
  and a composition at the seam stays live instead of stuttering.
- No restart machinery was built for the removed button. Resuming continues the same sweep.

Manual pause/replay UI tests are deleted; the suite now asserts the *absence* of playback controls
on both steps, and captures deterministic phases through test-owned time control rather than any
shipped UI.

**Defect found by the rendered-state assertion.** The `data-motion="paused"` rule lost on
specificity to `.onboarding-collaboration-card[data-state="working"] .onboarding-skeleton::after`,
whose `animation` shorthand resets play state to running — so the longest-running effect on the
screen was the one the suspension never reached. Asserting the attribute would have passed. The rule
now carries `!important`, for the same reason the reduced-motion block does: no authored rule should
be able to out-specify "the tab is not being presented".

#### The access block follows the actual diagram width

The six tiles are drawn 640 wide on the diagram's own 880 box, so their outer edges land on the
vertical legs of the return wire (the ports at x=120 and x=760). `.onboarding-access` is therefore
`calc(100% * 640 / 880)` of the same box the diagram is sized from, not a number that happens to
agree at the cap. It is asserted at 1440 / 1200 / 1100 / 1024 / 900 / 768 against the rendered port
centres, because only a width derived from the real diagram holds at all of them.

Documented exception, below 600: two columns of 640/880 of a phone would be about 200px of tiles
inside a 360px screen, so the block takes the full content width and the tiles stay a readable
target. This deliberately leaves the wire relation rather than following it into an unusable size.

#### The active-card halo is one managed semantic token, themed through variables

The reference's dark halo is a spread-less 28px mint at `#5BFFA060`; Light has its own tighter
16/-4 at `#10B98170` over a `#10B98160` border. Both are now carried by one managed token,
`--shadow-glow-onboarding-mint`, instead of approximated by stacking two rungs of the sized scale.

Its per-theme parts are named runtime variables (`--onboarding-glow-blur`, `-spread`, `-alpha`)
re-anchored beside the rest of each light palette. That shape is not decoration: `src/index.css`
declares the token inside `@theme inline`, so Tailwind substitutes its VALUE into the utilities at
build time and a later re-declaration of the token name would compile to nothing while looking
correct in the file. `--brand-glow-blur` already uses this shape, and `assertInlineThemeTokensAreNotRedeclared`
exists precisely to fail the other one. The hue needs no variable: `color-mix(in srgb, var(--mint) …)`
already follows whichever mint the theme declares.

The auditor was strengthened rather than widened. `glowScale.test.mjs` now resolves each rung
through its own theme's declared numbers before asserting, so a themed blur is a number by
assertion time — which let the CTA move from "themed blur, therefore unasserted" to pinned at 16
(dark) and 20 (light), and let the new onboarding role be pinned in both themes. Four red probes
confirm it bites: light CTA blur, light onboarding blur, light onboarding alpha, dark onboarding
spread. No idle card or assistant row gained a drop shadow; the suite asserts idle cards carry
`box-shadow: none` in both themes.

#### Type, mobile identity, and button chrome

- The live design's headline is 34/1.35/600 at **-1.6** tracking (`XKQOx`, `Igagh`, `hv9DA` agree),
  shared by both steps and asserted from computed style.
- Narrow adaptations are deliberate and recorded in the stylesheet where they are made: 30/-1.4 at
  960 (the same -0.047em ratio), and 25/-0.8 at 600 — phones loosen rather than follow the ratio,
  because tight tracking is exactly what costs legibility at that size.
- Below 600 the three card footers use one explicit grid (logo, name, role, each on its own row)
  with the name's box holding the two lines the longest of them needs. Free flex wrapping produced
  a different arrangement per name length and left the three separators up to 21px apart. Nothing is
  truncated, scaled globally, or dropped to the prototype's 7px; both lines of a wrapped name show.
  Asserted at 320 and 390 in EN and ZH and in the 768 band, from real bounding boxes: equal status
  y/height, equal identity y/height, no clipped or ellipsised label, role type at or above 10px.
- Re-scan is outlined (`n4ATm` / `H8gj7`); install and configure keep a visible themed border and
  surface; primary actions rest flat on the designed theme fill with its designed text colour. No
  global button recipe was rewritten and no darker global primary palette was adopted.

#### Skeletons are audited against native coordinates

All three are written from their own nodes and the numbers are recorded beside the rules:
`h5qtNn` (17 above the heading, then 12 / 8 / 14 / 7 / 7 between six lines, 14 below the last),
`u3bGM` (six 11-tall rows on a 16 pitch, 14 below the box top), `xyNG1` (four 12-tall rows on a 23
pitch, 17 below the box top — fewer rows, so this skeleton ends higher; the design's own asymmetry,
not slack). The extra card height the 1104 cap creates is allocated by reserving each end's authored
share in design units (17.56 status + 149.3 skeleton + 18.8 + 55.2 identity = 240.9 of content), so
the three separators align by construction and the surplus cannot pool under the last bar.
Typography and icons keep their authored sizes. No document footer or test-result strip was added.

#### Source-approved, left alone

The dark browser shadow stays as drawn (the native -17 spread is not copied), the return height is
correct, the 2s access emphasis is authored interaction and is not erased to match a static still,
existing logo assets and `PlatformIcon` are untouched, an idle Codex card is authored, the shimmer
is authored, and dual-card glow in a settled still is a comparison artefact of holding every effect
at one time rather than leakage.

Two observations from the 21:40 read-only visual pass are recorded as non-blocking rather than
fixed: the English testing caption takes three lines at 320 — the footer and card baselines stay
aligned and every word is readable, so wrapping is preferred to shrinking it — and the optical
top/bottom difference at 768 is explained by the native header's own space, so no redesign follows.

#### Evidence claims

- Settled stills (every CSS effect held past its end) are the only images compared against a static
  frame; they are labelled as such. The normal-speed evidence is a separate recorded run with no
  clock control at all, sampling what the page actually draws across a full loop and across a
  hide/show.
- `ThemeProvider` is checked for real: System mode sets no `data-theme` and follows an emulated OS
  switch live, and a stored explicit preference survives the OS moving in either direction and a
  reload.
- The fixture stays hermetic: a deny-by-default route records every non-GET or off-origin request
  and each test asserts that list is empty. No local service, no Incus runner, and no OAuth or
  install was ever performed — the assistants render an installed, permitted runtime because the
  fixture answers those reads, which is a rendering claim and not a working-integration one.

**Capture clock ownership, corrected 2026-09-17 21:52.** `page.clock.install()` replaces the timer
functions but leaves the clock advancing with real time — measured directly: 38 ticks of a 16ms
interval in 600ms of wall time. Only `pauseAt` holds it. Every "settled" still therefore landed
wherever wall-clock time had carried the story, which is how a dark and a light capture of the same
size ended up drawing different cards. `openOnboarding` now pauses at a fixed instant, after which
only `freezeAt` moves it; each still names its phase in its filename and the capture asserts the
rendered states are that phase's. This was found because the new offscreen test failed on it, not to
improve a claim.

Two limitations stand in the batch as it exists:

- `welcome-design-frame-1200x756-codex-working.png` is a VIEWPORT CROP, not a whole composition: at
  756 the primary action is below the fold. It proves matching in-view geometry only. Scrolling is
  the accepted outcome and the button is reachable; the companion `…-cta-…` still shows it scrolled
  to, and a second companion at 390x300 comes from the offscreen test.
- Images produced before the clock fix are not an exact-phase cross-theme pair, whatever their
  filename said. No deterministic same-phase A/B may be claimed for that batch. The batch shipped
  with this change is regenerated under the paused clock, where the phase is exact by construction.

#### The 360 slot is the assistant block (PM, 21:38)

Item 3's "minimum 360px slot" is `.onboarding-assistants`, the region native `sNs1V` names, and it
already carries `min-height: 360px`. An earlier note in this plan added up 252 and 348 from the
blocks *around* it and concluded the number did not resolve; those sums never measured the slot's
own minimum and are withdrawn. No implementation change followed — the geometry case now asserts the
slot directly (computed `min-height` 360, rendered height at least 360), together with the 24 below
it and the 52 from the last row to the button. Measuring both gaps is what shows the 52 comes from
the slot's spare height plus the 24, rather than from a margin tuned to match it.

### Follow-on boundary (not this patch)

The owner's 17:49 desktop request (no top navigation, System-default theme switching) belongs to
issues #2012 and #2013. This patch preserves the existing `ThemeProvider` semantics (System default,
stored manual preference, shared setter) and the Web language entry in the onboarding header, and
adds no desktop detection.

### Ownership

Single UI implementation lane owns `ui/src/components/onboarding/*`, `ui/src/components/steps/Welcome.tsx`, presentation-only edits in `steps/AgentDetection.tsx` and `Wizard.tsx`, directly related tests, only relevant `onboarding.*` keys in `ui/src/i18n/en.json` and `zh.json`, and this plan plus a short status correction to the existing shared plan. Keep detection/install/provider reconciliation, Back/Continue completion flow, and auth semantics intact. Reuse existing primitives/icons; component-scoped theme variables may encode exact approved design values without changing the global theme. Broader tokens/primitive or behavior changes require PM scope diagnosis before editing.

No-touch: primary checkout, old shared-onboarding worktree, other task branches/worktrees, design file, existing Show Page, native desktop, Python/API/persistent schema, credentials, runtime service, unrelated i18n, and global Workbench/Settings. PM may edit this plan before dispatch; after dispatch the lane is the sole writer until hand-back.

### Implementation and evidence

- Audit incumbent at this baseline against complete exported state set. Record residual #2011 controls accurately. Fix all in-scope visual drift, not only the obvious animation.
- Port the authored sequence and skeleton/line/check shapes into the existing React components. Adapt imports/i18n/semantic theme ownership; avoid importing the Show runtime or introducing parallel controllers/dependencies.
- Desktop width is constrained by design, not by available monitor width. On narrow screens preserve the pulse's source/destination connections through actual component sizing; do not solve overflow by discarding the narrative.
- Build a hermetic browser fixture for the actual product entry with mocked network and isolated browser state. Explicitly deny unmatched write-capable requests. Never connect to the running local service or call the Incus runner. No real auth/install claim.
- Capture design, before and after at the same content viewport and freeze the same timeline phase (reference is PM complete, Codex working, tests waiting); record crop/native-titlebar rules. Capture each phase and a representative full loop, plus hidden/reduced motion — ~~pause/replay~~ superseded 18:26, there are no playback controls to capture. Automated bounding rectangles supplement screenshots, not replace them.
- Test responsive invariants over wide/intermediate/narrow/short sizes; test Chinese/English and light/dark, preserve System mode. At 1200x800 do not inherit the prototype's preview-stage breakpoint that shrinks the cards merely because height <=850; the actual design has 240x228 cards there.
- Before opening a PR, deliver a concrete diff, local verification and screenshot/motion evidence for PM spot-check. No new product-direction approval is required. PM checks fidelity and consumer regressions before the first push.
- After the reviewed patch is ready, create PR with `Closes #2017`, base master, include scenario IDs and evidence/residual checks. Immediately notify PM with PR/head so an independent PM Watch can be armed; keep one durable lane combined PR/CI Watch. Never manually trigger Codex. Review/CI follow-up uses the normal circuit breaker.

### Tasks

- [x] Issue created and title/body read back.
- [x] Read existing Show Page source and latest Pencil frames.
- [x] Isolated worktree created from latest master.
- [x] Complete source-to-render difference table and fix shared visuals.
- [x] Complete same-viewport screenshot, motion and responsive evidence.
- [x] Bounded repair of the 18:23-18:36 disposition, recorded above.
- [x] Offscreen suspension, the 360 slot assertion, and the capture clock fix (PM 21:38/21:40).
- [ ] PM independent pre-push spot-check.
- [ ] PR, exact-head review, zero unresolved threads, all required CI green.
- [ ] Owner visual acceptance; merge requires a fresh explicit instruction.
