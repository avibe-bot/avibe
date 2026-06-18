# Vibe Remote Web UI Redesign Plan

> **Branch**: `feature/web-ui-redesign`
> **Status**: In progress
> **Design source**: `design.pen` top-level `vibe-remote` screens and `VR/*` reusable components
> **Reference frontend**: `../avibe-bot-backend` UI architecture and theme primitives

## Summary

Vibe Remote's current Web UI is functionally useful but structurally behind the
new product design.

The redesign will:

- rebuild the Web UI around the new `design.pen` console and wizard flows
- support desktop, mobile, light, and dark variants from the start
- align frontend architecture with `avibe-bot-backend` without migrating the
  runtime from Vite SPA to Next.js
- move the UI to a reusable token + primitive + feature architecture so future
  work across `vibe-remote` and `avibe.bot` feels like one product family

## Goals

- Deliver a high-fidelity implementation of the new Vibe Remote Web UI from
  `design.pen`
- Keep Vibe Remote on `React + Vite + TypeScript + Tailwind v4`
- Align visual tokens, component primitives, naming, and layout semantics with
  `avibe-bot-backend`
- Make light theme and mobile first-class architectural concerns instead of
  post-hoc patches
- Simplify future maintenance by separating page rendering from API and config
  persistence logic

## Non-Goals

- Do not migrate `vibe-remote/ui` to Next.js
- Do not proxy local Web UI traffic through `avibe.bot`
- Do not preserve the current route tree if it conflicts with the new
  information architecture
- Do not preserve the current page markup when a clean rewrite is simpler

## Constraints and Product Decisions

- Vibe Remote remains a locally served Web UI bundled by the Python project.
- New architecture should be UI-homologous with `avibe-bot-backend`, not
  framework-identical.
- `Remote Access` is absorbed into `Settings -> Service` in the new design.
- The redesign is allowed to introduce a new route hierarchy and shell.
- Frontend user-facing strings must continue to go through
  `ui/src/i18n/en.json` and `ui/src/i18n/zh.json`.

## Current State

Current frontend stack in `vibe-remote/ui`:

- React 19 + Vite 7 + TypeScript 5
- Tailwind CSS v4
- React Router 7
- partial shadcn-style building blocks (`cn`, `popover`, `command`,
  `combobox`)
- page-level components still own too much fetch/save behavior

Current gaps vs. design:

- route map is flatter and narrower than the new console IA
- shell/navigation does not match the new desktop/mobile layouts
- theme tokens do not match the new product family tokens
- light/mobile are not built as first-class layout concerns
- there is no full primitive layer for cards, buttons, badges, tables, dialogs,
  settings rows, wizard progress, and console navigation

## Reference Architecture Alignment

The new Vibe Remote UI should align with `avibe-bot-backend` in these areas:

- semantic theme tokens and naming
- Tailwind v4 token exposure via CSS custom properties
- shadcn/ui-style primitive layer
- `class-variance-authority`-based variant composition where it reduces class
  duplication
- component naming, spacing language, border/radius system, and page shell
  vocabulary

The new Vibe Remote UI should intentionally differ in these areas:

- keep Vite SPA routing instead of Next.js App Router
- keep client-side API access to the local Vibe Remote Web UI backend
- avoid server-component or server-action assumptions

## Target Information Architecture

### Main routes

- `/dashboard`
- `/groups`
- `/users`
- `/logs`
- `/settings/service`
- `/settings/platforms`
- `/settings/backends`
- `/settings/messaging`
- `/settings/diagnostics`

### Setup routes

- `/setup`
- `/setup/:step`

### Mapping from old UI

- old `/doctor` concepts split into `Settings -> Diagnostics` and `Logs`
- old `/doctor/logs` moves to `/logs`
- old `/remote-access` moves into `Settings -> Service`

## Design Source Mapping

Primary top-level screen families detected in `design.pen`:

- Dashboard
- Channels
- Users
- Logs
- Settings: Service / Platforms / Backends / Messaging / Diagnostics
- Wizard: Welcome / Backends / Platforms / Slack creds / Channels / Summary

Primary reusable `VR/*` design components:

- `VR/Sidebar`
- `VR/RoutingConfig`
- `VR/CM/Backends`
- `VR/CM/Service`
- `VR/CM/Platforms`
- `VR/CM/Messaging`
- `VR/CM/Diagnostics`

These should drive implementation structure rather than be treated as mere
visual references.

### Reuse Interpretation from Design

The design intentionally reuses the same configuration modules across setup and
settings. Do not fork business UI just because a module appears in two flows.

Shared module surfaces:

- platform choice and platform credential setup share the `VR/CM/Platforms`
  interaction model across Wizard and `Settings -> Platforms`
- backend detection, CLI path editing, default Agent selection, and install
  affordances share `VR/CM/Backends` across Wizard and `Settings -> Backends`
- messaging rows, diagnostics checks, routing configuration, and service runtime
  controls should map to their `VR/CM/*` modules wherever the same controls
  appear

Container-specific surfaces:

- Wizard owns setup chrome: progress bar, step badge, Back/Next/Skip actions,
  and first-run guidance copy
- Settings owns console chrome: sidebar, settings tabs, save/autosave actions,
  and persistent management copy
- Dashboard, Groups, Users, and Logs should not reuse Wizard containers; they
  reuse only lower-level rows, badges, buttons, routing controls, and data
  display primitives

Implementation rule: extract reusable configuration modules first, then render
them through Wizard or Settings adapters. Avoid copying save/validation logic,
but also avoid embedding a whole Wizard step inside Settings unchanged.

## Target Frontend Structure

```text
ui/src/
  app/
    AppProviders.tsx
    router.tsx
    shell/
  api/
    client.ts
    config.ts
    settings.ts
    users.ts
    logs.ts
    doctor.ts
    remote-access.ts
  components/
    ui/
    shared/
  features/
    dashboard/
    groups/
    users/
    logs/
    settings/
    setup/
  context/
  hooks/
  i18n/
  lib/
    theme/
    utils/
    routes/
    platforms/
    layout/
```

## Execution Plan

### Phase 0: Foundation and Safety

- create isolated git worktree
- write plan and lock route + architecture decisions
- inspect current API surface and preserve working behaviors

### Phase 1: Theme and Primitive Alignment

- replace current global tokens with the new shared product token system
- add light/dark theme support based on `data-theme`
- align fonts, radii, border strengths, surfaces, gradients, and focus rings
- add missing primitives inspired by `avibe-bot-backend`:
  - button
  - badge
  - card
  - input
  - label
  - separator
  - dialog
  - table/list building blocks

### Phase 2: App Shell and Routing Rewrite

- rebuild route tree around the new IA
- replace the current shell with the design-driven desktop sidebar and mobile
  bottom navigation
- add shared page header, section, stats, status, and settings-row primitives

### Phase 3: Setup Wizard Rewrite

- implement the new wizard layout and step flow
- map current setup state and persistence logic into the new feature structure
- keep API behavior correct while replacing page markup and routing

### Phase 4: Console Pages Rewrite

- implement Dashboard
- implement Channels
- implement Users
- implement Logs
- implement Settings pages:
  - Service
  - Platforms
  - Backends
  - Messaging
  - Diagnostics

### Phase 5: Mobile and Light Polish

- verify all routes for mobile layout correctness
- verify light theme semantic contrast and component parity
- fix page-specific overflow, density, and interaction issues

### Phase 6: Validation and Packaging

- `npm run build` in `ui/`
- targeted manual preview of new routes
- local Incus regression when user-facing behavior needs end-to-end confirmation
- ensure built assets still package correctly through the existing Python flow

## Architectural Rules During Implementation

- Pages should not own raw fetch orchestration when a feature-level hook or API
  module can own it.
- Visual primitives should be reusable across pages; avoid one-off page-only
  button/card/input variants unless the design truly requires it.
- Mobile and light theme support must be validated while building each feature,
  not deferred until the very end.
- Existing backend API contracts should remain intact unless a separate backend
  change is explicitly required.
- Prefer replacing whole legacy page implementations when incremental patching
  would leave structural debt behind.

## Initial Implementation Order

1. Theme tokens + primitive layer
2. New app shell + route map
3. Setup wizard screens
4. Dashboard
5. Settings pages
6. Channels / Users / Logs
7. Mobile/light cleanup and QA

## Risks

- Large route and layout rewrite may temporarily break navigation if done in the
  wrong order.
- Current components mix rendering and data writes; careless migration can cause
  silent config regression.
- Font parity and gradients matter to perceived fidelity; token alignment alone
  is not enough.
- Mobile table/list behavior likely needs design-aware reinterpretation rather
  than literal desktop compression.

## Success Criteria

- New console visually matches the design direction across desktop/mobile and
  light/dark.
- Vibe Remote and `avibe.bot` look and feel like one product family.
- The UI codebase is easier to maintain because primitives, theme, routing, and
  feature logic are clearly separated.
- The local packaging model remains intact.
