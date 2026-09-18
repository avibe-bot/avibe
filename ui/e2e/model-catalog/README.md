# Model Catalog Interaction Regression

Run `npm run test:model-catalog` from `ui/` after installing the pinned Chromium
binary with `npx playwright install chromium`. CI runs this in `ui-checks`.
The suite follows the existing
isolated UI fixture pattern and never starts or contacts Avibe or a backend CLI.
Only `modelsApi` is replaced with in-memory reads/writes. The catalog, picker,
editor, Radix dialogs and browser pointer/focus behavior are real.

All three backends run in English and Chinese with desktop mouse and mobile
touch input. Cases verify picker additions, save/readback, child Cancel/Escape/
outside dismissal, editor commit, custom-editor handoff and catalog cancellation.
The host unmounts the catalog on close, matching `SettingsModelsPage`.

This consumes the shared `MH-MENU-COMPOSE-001` addition workflow. It guards the
React ownership of portalled child dialogs: a delayed touch click must not be
classified as outside the catalog after the child closes. Mouse-only and JSDOM
tests do not reproduce that browser event timing. Backend persistence and routing
contracts remain covered by their existing tests; this is not live acceptance.

The live `playwright.config.ts` excludes this fixture directory. The consuming
`scripts/modelCatalogDiscovery.test.mjs` checks both real configs with `--list`:
live discovery excludes the fixture, while isolated discovery retains the original
36 catalog scenarios and discovers every fixture spec on desktop and mobile.
Collection starts no browser, web server or fixture.

`gateway-status.spec.ts` consumes `MH-GATEWAY-STATUS-001`. It renders the real
Agent card with isolated catalog and Agent supply data. The route summary and
collapsible Agent footer remain separate, and long identifiers wrap inside the
footer. Every localized route status fits the existing compact control, including
a wider fallback-font check. These are browser layout contracts, not live routing
or backend acceptance.

`gateway-layout.spec.ts` extends `MH-GATEWAY-STATUS-001` with fixed header rows:
backend title/status above model count/two horizontal actions, and route mappings
below model IDs. It checks all three backends in EN/ZH, narrow desktop columns
and mobile viewports (320–418px), plus the unchanged direct-mode entry point.
The fixture inherits the production overview action-size tokens.

`route-direct-edit.spec.ts` consumes `MH-ROUTING-007` through the production
route dialog. All backends and origins open directly editable without writing;
Cancel changes rereads saved intent in place, nested picker dismissal preserves
the parent, and only Save persists the exact ordered pairs. Desktop/mobile EN/ZH
screenshots also cover explicit pinning, long identities and light/dark footer
geometry. Its in-memory route API is installed only for the route fixture view.

`token-scope.spec.ts` extends `MH-MENU-COMPOSE-001` with finite rendering
contracts for the catalog name, picker row/name, portalled editor input/body,
and the editor's forwarded field label. An opt-in fixture also mounts the real
`GuardImpact` / `GuardGapList` with no dialog ancestor and checks their body,
label/count, list/hop, and hint geometry. Expected values are painted CSS
properties, not the existence of custom-property names.

The matrix covers desktop/mobile Chromium and explicit dark/light themes
against the opposite system preference, plus both system preferences with no
explicit theme. A nested light boundary on the standalone guard body checks its
label against the local muted-ink role. The catalog → picker → custom-editor
path uses production components and Radix portals. The direct edit path is also
used by the controls. Tailwind's generated utilities and the imported production
stylesheets participate in every run.

Sensitivity controls remove/relocate emitted declarations, put a metric under
inactive print media, use a root alias with a missing dependency, and freeze an
ink above the nested light boundary. Positive controls use `:where`, `:is`,
an ancestor-qualified selector, active media/supports conditions, a repaired
dependency, and a theme selector list. These run in fresh browser contexts;
they never rewrite source or contact Avibe. They verify this contract's
sensitivity, not a new cascade evaluator.

The suite does not certify every Model Hub class, arbitrary React branches or
CSS conditions, theme nesting in both directions, Tailwind directive modes,
pseudo-elements, or other browser engines. The existing root-derived washes,
accent mixes, and shadows retain their known mixed-theme limitation. Production
CSS and component behavior are unchanged. See
`docs/plans/issue-1856-model-hub-token-guard.md` (repository root) for the source
mutation reproduction and the historical analyzer boundary.
