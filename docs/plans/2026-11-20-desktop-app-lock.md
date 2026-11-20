# Desktop App Lock (D7-7A)

## Contract

Based on origin/master `3913091383` (including #4031). The approved product
specification is the orchestrator's `app-lock-plan.md` and `app-lock-design/`.
All seven recommendations were accepted on 2026-11-20.

One native owner protects every AppKit window and owns the local preference,
continuous-time departure deadline, device-owner authentication, and a single
memory-only pending entry. Default Off preserves existing behavior. An enabled
policy locks on cold start and after the selected departure threshold; changing
policy requires successful system authentication. No account password makes the
policy ineffective without discarding the saved preference. Availability is
rechecked on activation. Authentication focus changes cannot start a new timer.

The owner covers content before presentation, hides Mini and update popovers,
and restores their prior state only after authentication. New windows inherit
the same protection through AppKit observation. Entry points defer their effect
until unlock; a newer pending entry replaces the older one. Quit remains usable.
Only the main local Workbench can call the two settings commands. There is no
Web unlock API. Background Runtime and Agent operations continue.

## Boundaries

- Native implementation: `desktop/src-tauri`, including AppKit, LocalAuthentication,
  lifecycle, capabilities, main-document markers and entry routing.
- Shared Web panel, types, translations and adapters: iOS lane, cherry-picked
  from the orchestrator-provided commit before the freeze.
- Vault's existing view-retirement checks must distinguish a temporary lock
  cover from actual replacement; locked content must never accept input.
- W1 and Ask Vibey are independent lanes. Their final integration must retain
  the common native entry gate; Ask Vibey payloads remain memory-only until unlock.

## Validation and delivery

Focused state-machine tests, AppKit coverage checks, desktop-client browser
scenarios against a compiled test peer, cargo test/clippy/fmt and Vitest. The
validation peer is compiled only for test/e2e, with a release-build prohibition.
Real Touch ID/password/Watch, lid/sleep, Mission Control and signed-package
behavior require the two Mac acceptance groups in the product specification.

Freeze a local commit for main to package. No packaging, installation or PR in
this lane before Max's local acceptance. GitHub review/CI gates follow acceptance.
