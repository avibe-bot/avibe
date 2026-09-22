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
- A different explicit key/backend creates a new operation and supersedes an
  older pending operation before redeem. Duplicate submissions of the same
  normalized backend/key preserve the pending owner, using an operation-local
  keyed fingerprint, not a stored plaintext key. Durable duplicate recovery
  revalidates the selected operation ID; it cannot resume a newer operation.
  Invalid new backend input leaves the older operation untouched.
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
  revoke recovery. The fence precedes the existing Discord settings-store
  publication so its rejection cannot partially publish that side effect.
- The CLI treats any existing fixed record as an owned pending operation. It
  never prompts for a replacement key when the record is prepared, malformed,
  revoked, or otherwise not recoverable; `pair()` reports the state instead.
- Applied recovery accepts only the exact durable target identity. The
  generated session secret is created once per operation and reused on retry.
- Definitive rejection, malformed-response, and origin-update failures record
  a terminal `retirement_pending` marker under the configuration lock before
  retirement. A keyless retry only retires this marker; it neither redeems nor
  applies credentials. Uncertain transport/server failures stay `prepared`.
  If even terminal publication fails, report the unrecoverable local state
  instead of promising a keyless recovery.
- A verified revoked fence can be retired on repeated clear or an
  already-unpaired settings save. Unreadable journals block a real revocation,
  but do not block unrelated writes on an already-unpaired configuration.
- Web owners receive only a credential-free phase/action projection through
  status. The projection is advisory; the existing pairing owner revalidates
  every recovery under its lock. Members cannot inspect or replay the record.
- Web submission clears the one-time key even on failure. A durable recoverable
  record offers local recovery after failure or page reload, including when
  config is already paired. Replacing it requires a separate new-key action.
  A failed status refresh disables pairing until status can be checked again.
  Only the latest status request may publish its result or failure; pairing
  starts and component teardown invalidate older reads. A late response must
  not hide a newer recovery action or make failed status verification current.
- Transport and server failures retain an uncertain prepared claim and report
  an indeterminate result. CLI instructions show explicit new-key replacement
  with the intended backend; they never promise that keyless retry can recover
  an unrecorded response.

## Recovery boundaries

Local preflight protects predictable local publication failures before redeem.
It cannot eliminate every remote/local crash window. If the cloud has consumed
a key and no redeemed response was durably recorded, the result is reported as
indeterminate rather than presented as recoverable.

An unchanged generic config write is not a pending-operation cancellation
command. In particular, writing `enabled: false` when the saved identity is
already disabled, or round-tripping that identity from `GET /api/config`,
preserves recovery. Field presence alone cannot distinguish those no-op saves
from an intended cancellation without a separate explicit intent contract.
Actual identity revocation fences replay, and changed source identity fails
recovery validation. A new cancellation surface is outside this change.

## Validation

Focused tests cover real config save/reload, binding agreement, CLI recovery,
explicit replacement, malformed records/responses, path containment, atomic
parent permissions, clear/revocation ABA, retirement failure and retry,
lock-entry failures, cross-process/event-barrier concurrent claim/application,
stale late responses, and ordinary settings controls. The auth/setup catalog
and scenario harness cover the redeem → local failure → fresh consumer retry
→ durable config/binding loop through both HTTP and CLI. Rendered Web controls
cover failed submission, reload, local resume, explicit replacement, status
refresh failure, paired cleanup and owner authorization.
The HTTP error surface also runs through the real API provider, translator and
page in both locales. Its recovery-code inventory comes from the backend so
omissions in both catalogs cannot pass a parity-only check. Shared retirement
errors must not promise applied credentials or a recoverable response when
their structured outcome does not guarantee either.
