# Pairing Recovery Ownership Contract

## Goal

Make remote pairing recovery a single-owner operation across the pairing
caller, the CLI retry path, the local configuration, and the SQLite binding.
The cloud redeem remains irreversible; the local journal records only what is
durable and never promises recovery across an unrecorded crash.

## Contract

- One fixed `state/pending-pairing.json` record owns the pending operation.
- The record has a schema version, local `operation_id`, phase, source
  identity fingerprint, and canonical target identity.
- Provider-controlled identifiers never select a local path.
- SQLite migration runs before the existing configuration lock. The lock is
  held only for local claim, validation, publication, binding, and retirement.
  Network redeem runs outside it.
- An explicit key and valid backend URL always create a new operation and
  supersede an older pending operation before redeem. Invalid new backend input
  leaves the older operation untouched.
- A no-key retry can only resume a validated redeemed or applied record. A
  prepared, malformed, revoked, or superseded record never silently redeems
  again or prompts for a replacement key through the retry command.
- A redeemed response becomes recoverable only after the fixed record is
  durably published. A failed post-redeem journal publication is reported as
  indeterminate.
- Apply and retire re-read the record and source under the same lock. A stale
  response cannot publish configuration, binding, or connector state.
- Retirement is an explicit local step: an unlink failure returns a structured
  error, keeps the applied record for a no-key local retry, and never starts
  the connector while the operation remains unretired.
- `api.save_config` revocation detection invalidates pending replay authority
  before publishing a real enabled/credential-bearing to disabled/incomplete
  identity transition. Unrelated settings and unchanged empty writes do not
  revoke recovery.
- The CLI treats any existing fixed record as an owned pending operation. It
  never prompts for a replacement key when the record is prepared, malformed,
  revoked, or otherwise not recoverable; `pair()` reports the state instead.
- Applied recovery accepts only the exact durable target identity. The
  generated session secret is created once per operation and reused on retry.

## Recovery boundaries

Local preflight protects predictable local publication failures before redeem.
It cannot eliminate every remote/local crash window. If the cloud has consumed
a key and no redeemed response was durably recorded, the result is reported as
indeterminate rather than presented as recoverable.

## Validation

Focused tests cover real config save/reload, binding agreement, CLI recovery,
explicit replacement, malformed records/responses, path containment, atomic
parent permissions, clear/revocation ABA, retirement failure and retry,
lock-entry failures, cross-process/event-barrier concurrent claim/application,
stale late responses, and ordinary settings controls. The auth/setup catalog
and scenario harness cover the redeem → local failure → fresh consumer retry
→ durable config/binding loop.
