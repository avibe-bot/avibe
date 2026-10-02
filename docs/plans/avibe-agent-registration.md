# Avibe Agent registration (C-8)

## Change contract

Avibe Agent is an opt-in, in-process backend. The backend catalog declares the
Agent universe and the native-CLI subset. Agent creation, routing, listings,
configuration, Model Hub and ordinary IM selectors consume the Agent universe.
Native authentication, install/version probes, native session discovery/import,
native configuration files and manual process restart consume the CLI subset.

The existing backend cards, settings page, Agent picker and model-route controls
are reused. Catalog capabilities hide unsupported native operations. A checked-in
TypeScript projection of the Python catalog supplies frontend capabilities and
types; its exact contents are contract-tested.

Persisted configs without `agents.avibe` load with it disabled. The new section
contains only `enabled`. A successful enablement mutation uses the existing
controller reconciliation IPC path. Adapter registration and controller refresh
implementation belong to `feat/avibe-agent-adapter`.

## Validation boundaries

- Catalog/classification contract: all full-universe declarations equal the Agent
  or CLI catalog set; explicit capability subsets/supersets retain their relation.
  A newly introduced literal list must fail the inventory test.
- Released config fixtures and round trips: old backend settings remain unchanged,
  absent Avibe stays off, invalid optional Avibe config recovers off on disk load.
- Agent store and run target: create, list, select and resolve an Avibe Agent using
  temporary SQLite state. Existing tests cover only CLI-backed Agents.
- UI rendering: Avibe is selectable and configurable with no native action or CLI
  probe; Model Hub exposes its existing backend model controls.
- Native boundaries: installing, authenticating, restarting or resuming a native
  Avibe session is rejected before touching any native state.
- Changed-file Ruff, focused pytest/Vitest and UI production build; exact-head
  Codex review, CI and unresolved-thread gates before delivery.

## Known by design

- No default model is invented for an empty Avibe model catalog. Model Hub owns
  its available models and the selected Agent stores the model.
- Native resume modals in Slack/Discord/Feishu are not ordinary Agent pickers;
  Avibe has no native session store and is excluded there.
- Product Session fork and Ask-in-new keep their existing UI. The adapter lane
  supplies C-5 context anchors; native-session exclusion does not disable them.
- Native setup onboarding, import and global native prompt files remain CLI-only.
- No new visual element, dependency, production restart or merge is authorized.

## Validation recorded

- Catalog, config load, readiness and regression-script batch: 143 passed.
- Expanded config-save/API/auth/native-session/IM batch: 490 passed, 99 subtests.
- Routing, dispatch, C-8 inventory, Model Hub and IM batch: 176 passed, 2 subtests.
- UI focused suites, ESLint, test type checking and production build passed.
- Changed Python Ruff and whitespace checks passed.

These are hermetic boundary checks, not a live adapter/provider acceptance run.
The adapter dependency and a later integrated Incus acceptance pass remain
explicit handoff work; no local service was restarted.
