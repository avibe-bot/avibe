# Model Hub — F4 UI completeness

Closes the UI-side gaps the completeness audit
(`model-hub-completeness-audit.md`) found: shipped backend capabilities that the
live UI leaves unreachable or dishonest. Server side is F2's (#995); this lane
codes strictly against the frozen contracts (`model-hub-contracts/`), which are
already implemented on master (`cf1b4314`).

## Scope (audit item → fix)

1. **P0-03 / R38 — OAuth success → persisted Source.** The live `OAuthConnectDialog`
   only toasted + refetched on `success`; the mock hid the gap by appending a
   Source inside `completeFlow`. Fix: on `success`, the dialog finalizes via
   `POST /api/models/sources` with `oauth_flow_ref` (the flow id) — the server's
   `create_source` → `_create_oauth_source` path (`service.py:737,751`). The mock
   is refactored to mirror the handoff: `completeFlow` no longer appends; a new
   `createOAuthSource` materializes the Source from the completed flow. A
   `finalizing` state holds the "Connected" banner until the Source is actually
   persisted, so no premature success is shown; a finalize failure surfaces
   honestly.

2. **P1-02 / R32,R42 — Source row actions + custom-model delete.** New
   `SourceRowMenu` (overflow menu, appears on row hover — quiet per the density
   rulings): rename (`PATCH {display_name}`), re-discover (`POST /test`, hub
   sources only — `native_cli` test is rejected server-side), delete
   (`DELETE`, with the only-supplier guard: a `mode_switch_blocked` response
   escalates the confirm to a `?force=1` delete). Custom-model delete lives in
   the OpenCode drawer's edit dialog (`DELETE /custom-models`).

3. **P1-03 / R30 — Migration triggers.** New self-contained `MigrationBanner`
   (scans on mount, shows a dismissible strip when importable native configs
   exist, owns its `MigrationDialog`, re-scans after apply). Mounted on the
   Models page (first-open-after-upgrade) and in the Setup Wizard's backend step
   (`AgentDetection`, wizard mode only — the Settings→Backends page already has
   `BackendSupplyModeCard`'s strip). Dismissal is persisted by a stable signature
   of the importable set's per-config identities (scan ids + action) so it is
   non-nagging but still resurfaces a genuinely new importable config.

4. **P1-06 / R46 — Honest Hub mode.** `set_agent_mode` returns a fresh
   `AgentSupply` whose `current` is computed honestly (null when hub is selected
   but no eligible+available source can supply — exactly the "silent Direct"
   condition, `service.py:935-958`). "接入中枢" now reports success only when the
   round-trip yields `mode==='hub' && current`; otherwise it warns with the true
   state (both the Models-page button and the backend supply card). The Agent
   row shows a persistent honest note when a hub agent has no usable supply.

Out of scope: backend Python (F2/F3), contracts, design.pen. No workflows.

## Evidence layers

- Contract types mirrored in `types.ts` (client), no schema edits.
- i18n en+zh parity for every new key.
- `npm run build` green; hermetic Vite preview screenshots per surface.
- Live end-to-end is the post-merge Incus pass (as with prior lanes).
