# Shared chat-scroll observation follow-up

## Contract and boundary

This is a browser-test-only follow-up to the shared-base failures observed on
November 10, 2026. No dependency, product, scroll-restore, or deployment changes.
The branch starts from GitHub master `e13468888f206fb29d764ad113b85e3be6500e86`.

- A chip/panel handoff always draws exactly one shared icon and label. The label
  travels through a real intermediate position instead of jumping to an endpoint.
- The curtain matrix measures overlapping disclosures with their bodies already
  loaded. When both grow, the next row does not overshoot its resting place.
- Keep the existing geometry tolerances and real-time disclosure/scroll checks.

## Findings and changes

### Label interpolation: observation false negative

PR #3482, lint run `42806307146`, WebKit's execution-process disclosure test:
closing moved the label from `[49, 327.625]` to `[47, 321.875]`. The sampled path
went from the start to only about 0.68px from the endpoint, with a 78ms gap. A
maximum read gap below 110ms cannot guarantee a point more than 1px from both
ends of a short, eased subinterval.

`motion.spec.ts` now owns interpolation in a separate deterministic browser test.
It captures the real disclosure animations in the click's microtask checkpoint,
pauses and seeks them on a common animation clock, reads rendered geometry and
copy visibility, then restores their original times. Opening and closing must
both travel more than 2px, and include a point more than 1px from either end.
The real-time tests still check every observed icon/label count, coverage, row
geometry, interruption, reduced motion, and scroll-write behavior. There is no
sample-gap exemption in the interpolation assertion anymore.

### Curtain monotonicity: unloaded source fixture

PR #3483, lint run `42806333918`, WebKit row/above/opening/grows:
the row overshot by 5.022px. The same cell failed on master run `42807861392`.
The fixture preloaded a chip when it was the host, but not a chip that was the
source above/below it. That source first opens its skeleton, then shrinks to an
empty panel. Thus the supposedly "both grow" cell also contains a shrink.

With the existing `activityDelay=150` fixture parameter, the original setup
fails the same monotonicity assertion on Chromium (7.529px) and WebKit (7.543px).
Preloading both participating chips and checking that their busy body is gone
passes both engines with the same delay. The committed fixture keeps its normal
zero delay. Loading/reveal motion remains covered by `motion.spec.ts`.

### Return-to-latest glide: unresolved shared-base failure

PR #3483's Chromium-touch trace recorded scrollTop 8596 throughout, against
bottom 9592: this was not simply a glide landing a little short. The trace shows
a completed Playwright click, but does not expose the product's rest-state refs.
The same failure occurred on master run `42807861392`. Both browser projects
pass the unchanged focused test locally.

The product waits for rest before starting the glide, with a 1500ms lost
`scrollend` fallback, while the test records 700ms after clicking. This is a
candidate explanation, not an established cause of that CI trace. Leave
`scroll.spec.ts` and the product unchanged; retain shared-browser ownership
rather than attributing this to a lock update or declaring a product regression.

## Validation

Normal parallelism only (`workers: 1`), no load generation or Incus operations:

- Original-master focused baseline: all three reported cases on both engines,
  six tests passed.
- Final focused checks: 16 tests passed across both engines, covering the label
  probe, real-time chip motion, interrupted close, reduced motion, curtain hosts
  group/row/chip and above/below, and the unchanged return-to-latest glide.
- Chat-scroll TypeScript check, `npm run build`, and `npm run lint` passed.
- Controlled activity delay: original fixture failed both engines; preloaded
  fixture passed both with the same delay.
- Temporary product mutations (all restored before commit):
  - `steps(1, end)` label easing failed the unchanged interior-position
    assertion on both engines.
  - Keeping the summary twin opaque failed the one-copy assertion (2 vs 1)
    on both engines.
  - An overshooting disclosure easing failed the unchanged curtain
    monotonicity assertion (about 21px vs <=1px) on both engines, even after
    the fixture preload fix.

PR review and the full CI backstop remain the delivery gates. This lane does not
merge or deploy.
