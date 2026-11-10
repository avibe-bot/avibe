# Instance direct-certificate client (I2)

Status: implemented module, **not started by the product**. I3 owns the setting,
fresh permission, worker-thread scheduling, ingress lifecycle and rendezvous.
The Cloudflare path is unchanged. Wire dependency: section 2 of
[avibe-backend#325](https://github.com/avibe-bot/avibe-backend/pull/325), including
the conflict response, SPKI fingerprint vector and rotation admission cap added
at `507d0b04`. Backend routes and production issuance remain separate work.

## Local ownership and persistence

`vibe/direct_certificates.py` stores only under `$AVIBE_HOME/state/direct/`:

- directory: owner-only (`0700` on POSIX), never a symlink;
- `key-<public-key-fingerprint>.pem`: locally generated ECDSA P-256 private key,
  unencrypted PKCS#8 PEM, `0600` from creation through publication;
- `chain-<leaf-DER-SHA256>.pem`: parsed public certificate chain;
- `state.json`: schema version **1**, instance ID, normalized hostname, pairing
  identity fingerprint, public-key fingerprint, exact public CSR, purpose,
  order ID/status, independent order/current-leaf deadlines, expiry, bounded
  error code/backoff and a shared request fence. It also selects the active
  pair/CSR and records conflict cancellation.
  No private key, device secret or tunnel credential is serialized there.

The pairing fingerprint reuses the existing pairing transaction's identity
fields, including its credentials; it contains their digest, not their values.
A changed instance ID, hostname or pairing identity discards every file, pending
keys included. The existing config-save pairing hook retires files **before**
pairing replacement/clear is published. A failed config publication may therefore
lose the optional direct path, never preserve an old key for a new pairing.
Retirement failure is reported with a fixed warning and does not break pairing;
the client rechecks the persisted pairing before using or publishing files.

Writes reuse `config.atomic_io.write_atomic`. A new chain is written before the
manifest selects it; an unsuccessful installation leaves the old selected pair
intact. Rotation holds the active and one pending key, and deletes the old pair
only after successful selection. Interrupted unpublished files are cleaned
before another allocation. Corrupt/unsupported manifests warn and discard the
optional files rather than fail startup. There is no released older schema to
migrate; future changes must preserve this versioned shape or degrade safely.

## Caller and backend contract

All entry points are **synchronous and worker-thread-only**. Constructing
`DirectCertificateClient` does nothing. Each `poll(allowed=...)` makes at most
one bounded request through the existing paired-device HTTP helper, using its
`X-Vibe-Device-Secret` header, timeout and no-redirect behavior. No config lock
is held during HTTP. Publication rechecks both the live pairing and persisted
request fence, so a late reply after unpair cannot recreate credentials.

The caller supplies `allowed=True` only for owner opt-in plus fresh effective
backend permission. Ordinary polls cannot override `direct_access_not_allowed`;
`resume=True` is an explicit notification that fresh permission allows the
instance again. `retry=True` retries a terminal failed order, while
`rotate_key=True` requests a rotation; these are one-shot commands, not settings.

- First issuance and explicit rotation POST the locally persisted CSR with one
  exact DNS SAN: the host of the pairing's `public_url`. Lost replies/restarts
  resend the same CSR. Fingerprints are unpadded base64url SHA-256 of DER SPKI.
- Pending orders advance no sooner than `next_poll_at`; errors honor `retry_at`,
  including the seven-day rotation cap. Order-admission/CA backoff fences order
  POSTs, while a separate current-leaf lower bound fences only current GETs;
  neither can starve the other. Transport and credential backoff fences every
  request. Local retry backoff grows from one minute to one hour; successful
  order polls have a 30-second floor.
- A conflict resumes the supplied order if its key is still held (an active-key
  conflict abandons the pending local rotation). Otherwise it cancels the
  supplied order once, then retries admission at the backend's deadline while
  cleanup/settling completes. It invents no list/status route.
- After issuance, GET current checks at most six hours apart, shortened to
  before local expiry (a five-minute margin and 30-second poll floor). An active
  key keeps fetching renewals even while a pending rotation is failed or queued.
  No instance-owned renewal order is started, even after sleep/expiry. A missing
  current chain retries within five minutes. Older-backend route 404s quietly
  retry no faster than hourly.
- Before selection, every PEM block must parse, the leaf's public key must
  equal the local key, its SAN must be exactly the hostname, and its validity
  interval must contain the current time. The staged files must also load with
  the ingress's own SSL-context loader before any selected file is replaced.
  Public trust verification remains the connecting browser's job; this module
  does not implement another trust store.

`CertificateResult` hands the worker's caller usable file paths, fingerprint,
expiry, scheduling/error state and `reload_required`. A changed accepted leaf
requires ingress reload; the same usable leaf does not. A corrupted selected
chain is repaired even when the backend leaf is unchanged. Expired files and
backend denial are never offered as ready. Local readiness is not fresh policy permission:
I3 must independently stop serving when policy expires, pairing changes or the
owner opts out. No network/crypto exception text or raw backend detail is logged
or returned.

## Copy and observability inventory

- `storage/importer.py` migration backups copy five named legacy JSON files and
  `task_requests/`, not arbitrary state directories. `storage/backups.py` copies
  the SQLite database only. Tests exercise both with a real generated key.
- Config backup and API projection operate on config, not `state/direct`.
  Doctor and remote diagnostics produce structured projections, not a recursive
  state bundle. This PR adds no status, diagnostic or Web payload field.
- Feedback intake posts the explicitly supplied sanitized report; it does not
  collect files/state. Its workflow already forbids raw config/log attachments.
- The Incus runner prepares a separate seed from configured inputs and copies
  that seed, not this desktop's state; source sync excludes `.runtime`, `_tmp`,
  `tmp` and logs. Existing regression state is preserved, not exported.

Known limit: a custom `AVIBE_HOME` placed inside the source tree outside its
excluded directories can be copied by source sync, just like its config file
containing the tunnel token; this PR does not change the regression runner.
Whole-machine/user-directed backups are outside this product-copy inventory.

## Evidence and remaining work

`tests/test_direct_certificates.py` exercises the real filesystem/config/client
boundary against a fake backend: issuance/restart/lost response, invalid leaves
and chains, renewal/rotation, deadlines and failures, denial, conflict recovery,
permissions/redaction, unpair/re-pair races, persistence version skew and backups.
It also starts a real TLS `DirectIngress`, performs a verified handshake, then
reloads and observes the renewed leaf. The shared public SPKI vector pins the
cross-language fingerprint. Existing ingress and pairing recovery tests remain
unchanged and are run alongside it.

All tests use isolated homes and fake HTTP or loopback TLS. Backend route
interoperability, public CA issuance, backend renewal/retirement and the I3
lifecycle are unproven here and must be verified when those components exist.
