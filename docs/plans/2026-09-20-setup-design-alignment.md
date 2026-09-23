# Setup screen alignment to design_desktop.pen and the show-page reference

> Status: implemented on `fix/setup-design-alignment-20260920`, owner-approved scope
> from the 2026-09-20 acceptance review of `/setup`.
> Supersedes, for the values it names, the continuous card-height and headline rules
> in [2026-09-17-onboarding-design-fidelity](2026-09-17-onboarding-design-fidelity.md)
> and the `--shadow-glow-onboarding-mint` halo described there.

## Background

Owner acceptance of the shipped setup found four gaps against
`avibe-docs/design_desktop.pen` (read through Pencil) and the interactive reference in
Show session `ses36vg559de2`:

1. Large-screen adaptation drifted: a continuous card-height formula
   (`min(30vh, …)`) and a continuous headline clamp reproduce none of the design's
   four authored desktop readings exactly.
2. The top bar used the small square brand chip and the square "中" language button;
   the design draws a 44px logo lockup with a two-line wordmark and a 44px round
   language button with a Languages glyph.
3. The connection step's enable control was a checkbox with a text label; the design
   and the reference draw a switch in the card header's trailing slot.
4. The connection methods sat side by side as chips; the design stacks them as two
   full-width rows, and the card's hover/active shadow is a tight offset drop shadow,
   not a 28px spreadless halo.

## Goal

`/setup` matches `design_desktop.pen`'s `Web setup` frames at their authored
viewports (1366x768, 1440x900, 1920x1080, 390x844, plus the intermediate 1200x800)
and the reference session's interaction shapes, without touching the Settings
surfaces, which keep their own shells.

## Solution

### Discrete tiers instead of one continuous formula

The frames draw the composition at readings, not along a curve. `onboarding.css`
now declares four desktop bands plus the phone band as CSS custom-property tiers on
`.onboarding-shell`: card height 232 / 262 / 300 / 330 (258 stacked), and with each
its header height, card padding and interior rhythm, identity header, name size,
method-row and lifecycle-row heights, headline size/line-height/gap, heading-block
reserve and its distance to the stage, access block width, and action size. The
content column keeps the ratified continuous cap `clamp(976px, 62.5vw, 1200px)`;
only the vertical rhythm is authored per tier. The header is full-bleed at the
window's own gutter while the composition keeps its capped column.

### Header and language control

`Wizard` draws its own `SetupHeader`: the logo asset (which already carries its
white tile) at 44px beside "Avibe / Agent OS", and a 44px round Languages button with
the switcher's own dropdown menu. The settings-shell `LanguageSwitcher` and
`BrandLogo` are untouched, per the owner's decision that only the setup page changes.

### Connection card internals

- The enable control is a `role="switch"` button in the identity header's trailing
  slot, drawn per theme by the reference (dark: lit border on the on-state; light:
  no border, white thumb).
- The identity header rules itself off from the body with a 1px divider in both
  steps, as both frames draw.
- The lifecycle pill keeps the state row's left; the action the pill offers —
  update now, or its in-flight state — is drawn opposite it on that row by the card,
  while `BackendLifecycleChip` still owns the probe and the write and reports its
  derived visual up through a new `onVisual` callback (with `refreshKey` so a
  card-driven upgrade re-probes the chip). Install stays opposite the not-installed
  pill, as before.
- "Add subscription / sign in" and "Add API Key" are two full-width rows, stacked;
  the connected state wears the same row with a mint check and stays a button, so a
  settled connection can still be reopened.
- The card's active/hover shadow is the design's own `0 2px 12px #5BFFA038` (dark) /
  `0 2px 16px -4px #10B98124` (light). Because those carry a y offset they are not
  members of the centred accent glow scale, so the managed token
  `--shadow-glow-onboarding-mint` is retained but re-declared in that offset shape,
  with its per-theme blur, spread and alpha named beside each palette the way
  `--brand-glow-blur` is. `glowScale.test.mjs` keeps the role as an excused member:
  its blur is asserted from the role map, its offset geometry and alpha are pinned
  per theme, and the scale's blur assertion reads the shadow's third length so an
  offset is not mistaken for an absence of blur. `onboarding.css` consumes the
  token rather than holding literals.
- The wire box is sized `card * 350/300` from the card, which keeps the handoffs on
  the cards' midline at every tier; the return-loop caption rides y=338 of that box.

### Access block and action

The access block is 740 wide (830 at the large tier, full-width stacked on phones)
with the six circles kept as a centred 560 grid inside it; the primary action and
the way back take the tier's 52/56/54 height, 12 radius and 17px label with a 19px
arrow. The import capsule's review button takes the design's 6-radius 28px shape.

## Validation

- `npm run build`, `validate:theme`, `typecheck:tests`, `lint` green.
- Onboarding unit suites and `glowScale.test.mjs` green, including the retained
  offset-shadow role asserted per theme.
- `ui/e2e/onboarding-fidelity` updated to assert the tiers it now ships — card
  height per window, heading reserve and gap per tier, wire-box ratio, access
  block/grid spans, and the offset halo geometry — and run across the viewport,
  theme and language matrix.
