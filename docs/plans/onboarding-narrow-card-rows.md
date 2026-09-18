# Shared onboarding card rows on narrow screens

## Contract

At supported narrow widths, the three collaboration cards share their status,
skeleton and identity row boundaries. Every English/Chinese status and identity
remains readable through the authored animation phases, without truncation,
smaller type or changes to the copy. Desktop geometry, wire anchors, animation
timing and backend/setup behavior stay unchanged.

## Cause and boundary

The cards currently use independent flex columns. At 320px in Linux Chromium,
the OpenCode status wraps to four 13px lines. Its icon row plus gap and caption
requires 70px, whereas the other cards stop at their 62px minimum. A minimum
height is not a shared row-size contract; raising that constant would still
depend on font metrics and translated content.

Let the existing card grid own three shared rows below the existing 768px
desktop boundary. Each card consumes those rows through CSS subgrid. Explicit
max-content status and identity tracks follow the largest content requirement;
the existing skeleton absorbs the remaining height. Preserve the existing
design minimums and all desktop rules. No JavaScript measurement, new state,
dependency or text change.

Keep the card's inner column shrinkable and allow an overlong status word to
wrap within it. The later "Implementation ready" phase exposed why checking
only each label's own scroll size is insufficient: an intrinsically sized row
can extend beyond the card and be clipped by the card instead. The consumer
test must check row containment inside the card as well as label containment.

Run the same narrow contract in WebKit too. Its automatic track sizing honored
the identity's 44px minimum but let the stacked labels extend beyond that row;
explicit content-sized outer tracks express the intended sizing in both
engines. Keep this browser project limited to the narrow geometry contract.

## Validation-owner scope decision

Full-suite verification also exposed a pre-existing race in the same browser
harness's CSS-animation sampler, including at the unchanged 1200px desktop
layout. Two runs observed one-frame drift in the strict pause comparison.
A read-only Linux Chromium probe captured `playState="paused"` while
`pending=true`: the shimmer moved from 216.584ms to 233.284ms when `ready`
resolved, then remained stable. The other two probe samples had already
settled and did not move.

The bounded correction belongs to the test sampler: await each selected
animation's existing `ready` promise before reading its timeline. Keep the
strict paused/running, elapsed-time and resume assertions unchanged. Do not
change production animation behavior, add sleeps/retries, or relax tolerances.

## Verification

- Reproduce the unchanged 320px English case in Linux Chromium.
- Extend the existing real-Wizard browser geometry test across all authored
  phases, both languages, and the narrow breakpoint.
- Check shared status/identity boundaries, complete labels and containment.
- Run the full onboarding browser suite, focused component tests, UI lint,
  fixture typecheck and production build.
- Use a task-owned temporary test directory in the existing local Incus
  instance. The full worktree runner requires platform credentials, which this
  hermetic UI change does not need. Do not change the persistent master source,
  service, configuration, or real platform/provider connections.

## Known by design

- This does not redesign the approved desktop diagram or change its animation.
- This is a rendered layout contract, not a new authentication/setup scenario.
- Browser evidence must name the engine/platform actually exercised. Do not
  claim arbitrary translations or unbounded text fit inside a fixed diagram.
- Merge and deployment require their own authorization.

## Results

- Unchanged base `a8df6b5bb` reproduced the 320px English failure in isolated
  Linux Chromium: status heights differed by 8px.
- Final Linux Chromium onboarding suite: **82 passed**, including desktop
  geometry, motion, themes, connections and the expanded narrow contract.
- Final macOS WebKit narrow matrix: **10 passed**. The matrix covers 320,
  375, 390, 767 and 768px, English/Chinese, and all seven authored phases.
- Focused component/timeline unit tests: **16 passed**. UI lint baseline,
  changed-file ESLint, fixture typecheck, theme/catalog validation, production
  build and diff checks passed.
- The initial row-containment and WebKit failures were retained as diagnostic
  evidence and fixed before opening the PR. No assertions were removed or
  relaxed; no real provider/platform calls were made.
- The four browser/product input files in the Linux temporary directory were
  SHA-256-equal to the corresponding task-worktree files.
- Exact-head Codex review and the complete GitHub lint workflow remain required
  before delivery as merge-ready. No merge, master deployment or restart is
  included in this change.
