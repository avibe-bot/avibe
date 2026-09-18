# Model Hub rendered token scope contract

## Problem and decision

At `0e5a672ad183ac56f5ee8df7bcb770754840b3d9`, the 23 travelling-token
tests protect only `GuardImpact`, `GuardGapList`, and their body wrapper.
Moving `--model-hub-model-control-height` from its control class to
`.model-hub-catalog-dialog` still passes that check. The editor portals outside
the catalog DOM subtree, so this declaration cannot supply its inputs.
The picker also portals, but carries the catalog class itself: moving a token
there is not the same counterexample.

The five findings-bearing heads of #1850 produced 16 analyzer findings against
CSS/JSX models. That implementation was withdrawn. Name existence alone cannot
prove inheritance, and a blanket markup-token policy would restrict valid
component styling without demonstrating this boundary.

Reuse the existing Model Catalog Playwright fixture and `lint` / `ui-checks`
owner. Assert finite computed geometry and text-role contracts on production
catalog, picker, editor, and shared guard-body markup. Let Chromium evaluate
selectors, generated Tailwind utilities, conditions, and React's actual DOM.
No new dependency, analyzer, workflow, or component authoring rule is needed.

## Invariant and boundaries

- The named catalog, picker, editor, and guard-body metrics retain their
  current computed values on desktop/mobile under explicit and system themes.
- A shared guard body outside a dialog retains its spacing, type, and count
  padding. Its label follows a nested light theme rather than a dark root alias.
- The editor's forwarded field label and its portalled input are measured at
  the DOM elements that receive the production props.
- Moving/removing a relevant declaration and freezing a label alias must fail
  the corresponding assertion. Equivalent selector spellings and conditional
  declarations active in the tested state must still pass.

This is finite rendering evidence, not proof of all classes, mount locations,
React branches, browser engines, viewports, CSS conditions, or arbitrary token
resolution. No production appearance or behavior is changed. The narrow static
analyzer remains at its existing scope; its `:where` / `:is` false positive is
not generalized or repaired here.

The existing root-derived washes/accents and mixed-theme behavior documented
in #1850 remain outside this invariant. A current visual defect there requires
a separate product-scope decision, not silently changing the baseline to make
this guard pass.

## Validation plan

- Baseline travelling-token tests and same-source browser measurements.
- Missing/misplaced metric and frozen-theme-alias sensitivity controls.
- Valid equivalent selector/scope controls, using the same rendering assertion.
- Targeted and full Model Catalog browser suite, affected UI unit suite,
  lint, test typecheck, theme validation, and production build.
- Local task-isolated Incus only if live integration is needed and capacity is
  available; never use a shared or running personal service as a fixture.

## Status

The targeted suite passes all 26 desktop/mobile cases. A source-level
sensitivity experiment temporarily made both changes below, then ran the
unchanged baseline assertions through Vite/Tailwind:

1. Move the sole `--model-hub-model-control-height: 40px` declaration onto
   `.model-hub-catalog-dialog`. The editor input computes to **28.75px**, and the
   new 40px assertion fails.
2. Add `:root { --scope-control-ink: var(--model-hub-ink-73) }` and make
   `.model-hub-guard-label` read it. Under nested light on dark, the label
   computes to **rgba(255, 255, 255, 0.45)** rather than the local muted-ink role
   **color(srgb 0.360784 0.376471 0.47451 / 0.8)**, and the new assertion fails.

The original 23 travelling-token tests pass with both mutations present.
The stylesheet was restored exactly afterward; there is no production diff.
Equivalent declaration controls and the unchanged source pass in Chromium.
The committed sensitivity cases mutate only the emitted CSS in disposable
browser contexts, so CI can repeat them without rewriting the checkout.

Historical findings are covered only where this mechanism makes a claim:
functional/context selectors use the actual matched element; conditional
availability is observed in screen/print and active supports controls; missing
alias dependencies and nested theme substitution are observed at the consuming
element. The editor's real forwarded label is measured after React renders it.
No source-text/JSX parsing, arbitrary branch enumeration, `@theme` emission-mode
classifier, or universal alias analysis is introduced. Those old analyzer
findings therefore do not become claims of coverage here.

Broader validation passed: 130 Model Catalog browser cases; 304 UI unit-test
files / 4,137 tests; lint with no baseline drift; Model Catalog fixture and
Model Hub unit-test typechecks; theme and scenario-catalog validation; and
the production build. No Python source changed.

No live backend integration is required for this test-only rendering guard:
the fixture integrates the shipped components, Radix, Vite/Tailwind, and real
Chromium while replacing only the API boundary. It starts no Avibe instance
and does not use Incus or the personal service.
