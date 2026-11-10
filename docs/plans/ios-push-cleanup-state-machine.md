# iOS Push Cleanup State Machine Root Fix

## Background

PR #3412 repeatedly exposed Class P in the native notification Disable path. The current head `43eb3796656de1e82dc6c387fcf02f508b4951ec` has a security P1: a failed instance DELETE plus native `revoked:false` can later become a single `confirmed` marker even while relay/native revoke remains pending. The same compressed marker also causes a separate stale-pending UX issue. This is an architecture/data-model defect, not another isolated branch condition.

## Goal

Make Disable an honest, durable, retryable cleanup saga. A user opt-out must
remain authoritative until explicit Enable, including across same-origin
documents. Instance cleanup and native/relay revoke must be persisted and
advanced independently. The UI may only project `disabled`/confirmed when
every required component is confirmed.

## Solution

- Define a versioned cleanup record with independent `userIntent`, captured account/origin-scoped endpoint target, `instanceCleanup`, and `nativeRevoke` facts. Preserve loading of old markers safely.
- Persist a monotonic intent revision separately from the cleanup record. Serialize
  every Enable, Disable, Retry, automatic reconcile, and pending replay through a
  same-origin cross-document lifecycle lock, and validate the revision before
  each native or instance side effect.
- Capture the concrete endpoint once on first Disable. New records use
  `capture_pending` so an interrupted first capture can resume. Old markers with
  no endpoint fail closed and never guess a target.
- Make Retry execute only unfinished components. Native `off` is not proof that a deferred native/relay revoke is complete.
- If an Enable becomes stale, revoke any native endpoint it created before
  posting it. Automatic recovery never clears a durable Disable intent.
- Extend the native bridge/status contract so the Web layer can distinguish active, absent, pending revoke, confirmed, not-needed, and bridge failure. Reuse the existing native concrete `retiring + pendingRevoke` recovery; do not redesign relay or add cross-system atomicity.
- Carry an explicit force-replacement mode through the bridge so repair can
  create a new native registration instead of reusing an active one. Serialize
  APNs tuple persistence with lifecycle mutation ownership, and merge the latest
  tuple before logout writes its account transition.
- Keep automatic reconcile and pending registration replay fenced by the durable opt-out. Only explicit Enable clears it.
- Keep browser Web Push behavior unchanged unless shared types/tests require a safe additive abstraction.
- Add invariant-focused tests, especially failure -> native off/pending -> Retry -> independent completion, plus reload/scoping/legacy fail-closed and no-resubscribe interleavings.

## Todo

- [x] Implement the versioned persisted state and migration-safe store API.
- [x] Implement the cleanup reducer/transition logic and truthful aggregate projection.
- [x] Expose native pending-revoke facts through the bridge and wire the Web layer.
- [x] Add/update iOS and UI contract tests, including the cross-component failure sequence.
- [x] Update the native bridge contract documentation for the additive payload shape.
- [x] Run focused UI/native tests, `npm run typecheck:tests`, `npm run build`, `git diff --check`, and required local checks.
- [x] Self-review the implementation diff and full PR scope against `origin/master`.
- [ ] Push the new head, verify exact-head Codex review, and request a fresh security review after Codex/current CI evidence. Stop on any new/repeated Class P/R/K/A/B/409/E finding; do not start another repair round automatically.

## Boundaries

- Do not modify chat-scroll/WebKit tests or PR #3428 work.
- Do not change relay backend/API semantics or Apple Portal/secrets/TestFlight.
- Do not claim cross-system atomicity. The contract is durable, conservative, and eventually retryable.
