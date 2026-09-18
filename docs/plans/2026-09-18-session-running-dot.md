# Session dot motion and selected-row alignment

## Outcome and authority

The owner requested an issue and an implementation agent for the green dot before
the name of a running session: it should keep blinking while the session runs.
The owner also reported that selecting a session shifts its dot to the right;
selected and unselected rows must share the same dot and name alignment.
The supplied screenshot establishes the approved dot, row, position, and color.
This is a narrow motion refinement of that supplied design, not a new UI surface.
Owner merge and release authorization has not been given.

## Evidence

At origin/master `557186174`, both `WorkbenchSidebar.tsx` and `ProjectsPage.tsx`
render static mint dots for `WorkbenchSession.agent_status === 'running'`.
The API type and the existing `session.status` event already expose the live
`idle | running | failed` state; a new backend status or polling mechanism is
unnecessary. Both surfaces render the dot in their normal and rename paths.
The desktop row currently combines unconditional `pl-[26px]` with selected-only
`border-l-2` and `pl-[24px]` using `clsx`. Conflicting padding utilities may leave
the intended border compensation ineffective in generated CSS. This is a
testable hypothesis, not a measured diagnosis yet; inspect browser computed
padding and bounding boxes before choosing the smallest alignment fix.

## Change contract

- Producer: existing Workbench session state, including its existing live updates.
- Consumer: the status dot immediately before each session name in the desktop
  sidebar and mobile Projects list, including inline rename.
- Motion applies exactly while `agent_status === 'running'` and the user has not
  requested reduced motion. Route selection, unread count, hover, and focus do
  not determine whether a session is running.
- Use a gentle repeating opacity pulse (existing Tailwind pulse, approximately
  two seconds per cycle). Keep the dot visible through the cycle and preserve
  its current green token, glow, size, position, and surrounding layout.
- Dot center X and name left edge must remain identical across selected and
  unselected rows at the same tree depth, including after navigation. Preserve
  the current selected-row background and left accent without letting their
  border geometry shift content. Prefer one unambiguous spacing rule over
  competing utility classes or another compensating offset.
- A transition out of running removes the animation immediately through the
  existing state render. Preserve the incumbent idle and failure appearance.
- Under `prefers-reduced-motion: reduce`, show the static status color. Preserve
  existing localized status descriptions.
- No new package, timer, fetch, persisted field, or runtime lifecycle change.
  Keep Agent graph indicators and other unrelated animations outside this task.

Motion thesis: the only moving element is the running dot; it tells users which
session is currently working even when another chat is open. CSS opacity gives
continuous feedback without geometry changes or a JavaScript animation loop.

## Scope and delivery

One implementation lane owns `ui/src/components/workbench/WorkbenchSidebar.tsx`,
`ui/src/components/workbench/ProjectsPage.tsx`, this plan, and directly relevant
existing row tests. A small shared status presentation helper or component may
be used if it reduces the repeated paths without changing other state semantics.
The existing `sessionRowLayout.ts` and a focused browser regression in the current
UI test framework are also in scope if the alignment fix needs them.
Touch `ui/src/index.css` only if the existing motion utility is insufficient.
All backend, settings, authorization, regression-runner, and other feature files
are outside scope. Never edit the primary checkout's unrelated dirty work.

The lane must follow `AGENTS.md` and `pr-delivery-loop`, open a non-draft PR linked
to the issue, drive exact-head Codex review and CI, and hand back without merging.
The orchestrator is Avibe Session `sesdztacubwq4`.

## Acceptance and evidence

1. A running session's dot visibly cycles for multiple periods independently of
   selected-row state, on both desktop and mobile session-list surfaces.
2. Changing the same row's runtime status starts or stops the animation with no
   reload; the static status appearance remains correct outside running.
3. Reduced-motion rendering remains static and communicates the same status.
4. Row geometry, text readability, rename, and existing actions remain usable.
5. Browser bounding-box measurements show equal dot centers and name left edges
   between selected and unselected sibling rows, before and after selection is
   switched. Compare both editable and read-only rows without changing action
   permissions or the selected-row visual language.

Run the relevant existing UI tests, scoped lint, and `npm run build` before push.
Do not add implementation-mirroring test scaffolding for a CSS utility change.
Verify actual browser computed animation and visible motion with test-owned data
and isolated browser state; use the local Incus runner for product-level probes.
Capture desktop/mobile and reduced-motion evidence in the PR. If product-level
verification is unavailable, state the remaining check rather than claiming it.
Never restart the user's local Avibe service or mutate real sessions for tests.

## Implementation result

The alignment hypothesis was confirmed by measurement, not by reading the JSX.
In the baseline bundle `.pl-[24px]` is emitted before `.pl-[26px]`. Utilities of
equal specificity are resolved by their order in the generated stylesheet, not by
the order of the class string, so the selected row's `pl-[24px]` compensation for
its `border-l-2` never applied: selected content sat at 2px + 26px while its
siblings sat at 26px. That is the 2px rightward shift the owner reported.

The fix moves both invariants into `sessionRowLayout.ts`, which already owns this
row's shared geometry. Every desktop row now carries `border-l-2 pl-[24px]`
unconditionally and selection changes only the border *colour*, so a row can
never emit two competing `pl-*` utilities and no compensating offset exists to go
stale. The selected background, the left accent, and the absolutely positioned
action rail are unchanged; the rail is positioned against the padding box, so the
always-present border does not move it.

Motion is the stock Tailwind `animate-pulse`
(`pulse 2s cubic-bezier(.4,0,.6,1) infinite`, `@keyframes pulse{50%{opacity:.5}}`),
which is already the contract's gentle two-second cycle that never fully
disappears, so `index.css` needed no new keyframes. `motion-reduce:animate-none`
is emitted later, inside the reduced-motion media query, and therefore wins.
Both surfaces import the one motion constant so they cannot drift apart.

Evidence: `ui/e2e/session-dot/` drives the real sidebar, the real mobile list and
the real provider against test-owned mocked data in an isolated browser profile.
It measures rather than mirrors classes — it pauses the live CSS animation, walks
`currentTime` across four cycles and reads computed opacity (1 / 0.5 / 1 / 0.5 / 1
at 0/1000/2000/3000/4000 ms, never below 0.5), observes an untouched wall-clock
change, and compares bounding boxes of selected and unselected sibling rows. It
flips status through the product's own `session.status` event. Reverting the two
product class changes fails 10 of its 11 cases, the alignment case reporting the
exact 2px difference.

## Checklist

- [x] Inspect current code and freeze the change contract.
- [x] Create and read back [issue #2035](https://github.com/avibe-bot/avibe/issues/2035).
- [x] Implement in the assigned isolated worktree.
- [x] Verify status transitions, reduced motion, and the UI build.
- [ ] Open the PR and complete Codex review plus required CI.
- [ ] Deliver the PR and validation evidence to the orchestrator; do not merge.
