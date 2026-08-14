# Web Push notification authorization policy split

Issue: #1434 (related auth-policy issue: #1433).

## Background

On iOS PWA, a Web Push test notification arrives while normal agent-result
notifications are silently missing. The test endpoint (`POST /api/web-push/test`)
resolves the current subscription and calls the provider directly. The normal
inbox delivery path instead re-validated the persisted prompt authorization
snapshot (`_web_push_authorization_contexts` message metadata) and discarded the
recipient when either:

- `session_needs_authorization_refresh(...)` was true — the 12-hour
  Organization-style interactive refresh cutoff introduced with commit
  `9177928` ("fix(auth): expire persisted push claims"); or
- `session_authorization_is_current(...)` could not accept the stored revision —
  which also treated a stale/missing device watermark (control-plane outage)
  as if it were confirmed revocation.

Normal reachability therefore depended on recent interactive Web use, and the
skip was only logged at debug level.

## Goal

- Personal and Organization notification authorization use independent
  policies, consistent with the #1433 direction.
- Personal: no 12-hour refresh cutoff, no Organization membership/group/revision
  gates; the installed PWA's subscription is durable state across
  sliding-session renewal.
- Organization: resolve current access at delivery time; confirmed revocation
  stops delivery; temporary revision/control-plane unavailability is never
  treated as confirmed revocation and gets a bounded retry plus an explicit,
  visible disposition.
- Test/status surface reports the same structured authorization state so a
  successful test send can be explained against the normal-only gates.

## Solution

- `vibe/remote_access.py`: new public `session_authorization_revision_state()`
  returns the tri-state classification `not_configured` / `unsigned` /
  `unavailable` / `current` / `mismatch`; `session_authorization_is_current()`
  delegates to it (same truth table as before).
- `core/web_push_notifications.py`:
  - Policy selection per persisted record: paired `instance_kind` selects the
    policy; unknown (legacy) kinds fall back to the record's claim shape
    (organization-group claims → Organization, else Personal).
  - Personal policy authorizes from the persisted signed snapshot alone.
  - Organization policy maps `mismatch` → `revoked` (skip), `unsigned` →
    `authorization_refresh_required` (skip), `unavailable` → one bounded
    `sync_authorization_revision_once()` retry, then skip with
    `revision_unavailable` if still unavailable. Per-delivery decision only:
    subscriptions stay enabled and the next message retries.
  - Structured dispositions (`sent`, `no_owner`,
    `authorization_refresh_required`, `revision_unavailable`, `revoked`,
    `no_subscription`, `provider_failure`, `suppressed_read`) recorded in a
    bounded ring persisted to `state_meta`
    (`web_push.recent_delivery_dispositions`) so entries written by the
    controller-process delivery worker are visible to the UI-process
    test/status surface, and surfaced through
    `recent_delivery_dispositions()` / `evaluate_delivery_authorization_for_context()`.
  - Authorization skips now log at warning/info instead of debug.
  - Review round 1 hardening (Codex findings): a revision-signed Organization
    snapshot with missing/unreadable config is `revision_unavailable`, never
    authorized (the snapshot proves the instance was revision-synced when it
    was minted); the single bounded watermark retry is shared across all
    merged owners of one delivery; local owners are labeled with the `local`
    policy instead of a bogus remote refresh disposition.
- `vibe/ui_server.py`:
  - `_web_push_user_key()` no longer applies the 12-hour refresh cutoff;
    `parse_session_cookie` still enforces signature, expiry, and confirmed
    revision revocation.
  - `/api/web-push/status` and `/api/web-push/test` return a
    `normal_delivery` evaluation (policy, authorized, disposition, reason,
    revision state, recent per-owner deliveries) for the calling owner.

## Todo / follow-ups

- [x] Core policy split, dispositions, bounded retry.
- [x] Test/status diagnostics surface.
- [x] Regression tests: 12-hour boundary, Personal/Organization isolation
      (including unknown-kind fallback), confirmed revocation, revision
      unavailability (recovery and skip), unsigned organization claims,
      no-subscription disposition, provider delivery.
- [ ] #1433 remains the owner of the interactive session-surface policy split;
      once its policy owners land, the push-path selection here should delegate
      to them instead of reading `instance_kind` directly.
