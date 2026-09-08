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
