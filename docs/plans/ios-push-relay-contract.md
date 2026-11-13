# iOS Push Relay Contract

Status: Phase 1 shared contract for the `avibe-backend` relay lane and the
`avibe` `core/`, `ui/`, and `ios/` lane.

This contract must merge to `master` before either implementation lane
branches. It defines the cross-repository API, ownership, lifecycle, security,
and acceptance boundaries. Implementation details may vary, but field names,
status meanings, mutation scopes, and observable guarantees may not change
without a contract amendment.

## 1. Scope and fixed architecture

The feature gives the iOS app APNs notifications while preserving the
instance's existing notification policy and Web Push sender:

```text
Instance policy and payload
  -> existing pywebpush RFC 8291 aes128gcm encryption
  -> existing RFC 8292 VAPID ES256 authorization
  -> avibe.bot ciphertext relay
  -> APNs HTTP/2
  -> Notification Service Extension (NSE)
  -> decrypted iOS notification
```

The relay is an opaque ciphertext forwarder. It validates authorization,
looks up routing state, wraps the unchanged encrypted Web Push body in the
APNs envelope, and sends it to Apple. It MUST NOT decrypt or inspect
notification content and MUST NOT persist plaintext or ciphertext. Base64url
encoding the unchanged body into `avibe.ct` is the only relay-side
representation change.

The instance path remains authoritative for unread, authorization, Project,
hidden Session, mute, and notification-preference gates. It still calculates
the per-owner `badge_count`, encrypts with `pywebpush`, signs with its VAPID
key, and disables a subscription after HTTP `404` or `410`.

One normalized HTTPS origin, configured as `PUBLIC_BASE_URL`, is used for all
of these values:

- generated endpoints;
- the exact RFC 8292 VAPID `aud`;
- the exact `Origin` accepted by the account-authenticated binding route.

For the first deployment it is `https://avibe.bot`. It has no path, query, or
fragment and is never supplied by a client.

The endpoint is stateful:

```text
<PUBLIC_BASE_URL>/api/push/apns/<registration_id>
```

`registration_id` is a fresh 32-byte CSPRNG value encoded as 43 base64url
characters without padding. Native creates it before registration, persists
the pending operation, and never reuses it. It is also the APNs envelope
`kid` and the NSE key-record lookup key. There is no stateless sealing.

## 2. Authentication and ownership

### 2.1 Account binding

Only the installation binding route uses account authentication. It runs in a
short-lived transport owned by `MobileAccountCoordinator`, using that
account's existing persistent `WKWebsiteDataStore`. Releasing a hidden
transport does not destroy that store; the coordinator may create another
transport while preserving its account-generation checks, release barriers,
and exact-origin guard.

The app does not copy cookies into `URLSession`, persist a Supabase bearer
token, or create a second account-auth owner. The relay derives `account_id`
from the existing same-origin session cookie and requires:

```http
Origin: <exact PUBLIC_BASE_URL origin>
Cookie: <existing avibe.bot account session>
```

An expired session, an unavailable account transport, or insufficient iOS
execution time fails visibly and safely. Native retains the pending operation
and retries on a later foreground execution opportunity. Network availability
alone does not imply that iOS will run the app.

The account-auth implementation may return `401 login_unauthenticated` when it
cannot distinguish an invalid or expired session from temporary upstream auth
unavailability. It may return `502 auth_unavailable` when it can make that
distinction. Both preserve pending push state and surface a recoverable bind
failure; neither erases local credentials or registrations. A positively
rejected installation credential fails that request and is not retried as an
account-auth failure.

### 2.2 Installation push credential

All management routes, including binding, use one installation-scoped
credential:

```http
Authorization: AvibeInstallation <installation_id>.<installation_secret>
```

`installation_secret` is 32 random bytes encoded as base64url without padding.
Native generates it before the first bind, stores it in a non-App-Group
`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` Keychain item, and never
exposes it to Web content, an instance, the bridge, or the NSE. The relay stores
only a password-hash or keyed-hash representation suitable for bearer-secret
verification. Logs and telemetry contain neither the raw value nor its
reversible representation. Verification uses a constant-time comparison after
the same normalization and hash derivation.

Possession authorizes push management for exactly one installation. It does
not grant account access, read notification content, or manage another
installation. The credential remains stable across login, logout, and account
switch in Phase 1. It is not deleted while a management or orphaned-revoke
operation for that installation remains recoverable. Reinstall recovery moves
the surviving old credential into the orphaned operation before creating a new
installation identity. The credential supplies authorization, not background
execution.

The path installation ID and authorization-header installation ID must match.
Credential validation, account ownership changes, `bind_generation` update,
and old-registration revocation happen under one locked installation
transaction.

### 2.3 Monotonic bind generation

Native owns a monotonic `bind_generation` for each `installation_id`. It
persists the generation before sending a bind and keeps it across process
restart and account switch. One local sign-in activation allocates one
generation; retries for that same sign-in reuse it. A later sign-in uses a
higher generation. Generations may have gaps and never reset while the
installation ID is reused.

Under the installation lock, the relay handles a bind as follows:

- a generation greater than the stored value changes `account_id`, stores the
  new generation, and revokes every registration for that installation whose
  `bind_generation` is lower, all in one transaction;
- a generation equal to the stored value with the same authenticated account
  is an idempotent `200` replay;
- a lower generation, or an equal generation with a different authenticated
  account, returns mutation-free `409 stale_bind_generation`.

A lost bind response is retried with the same account session and generation.
If that session expires or a different account signs in, the later sign-in uses
a higher generation and supersedes the uncertain bind. A delayed old bind
cannot move the installation back after the higher generation commits. There
is no pending secret, credential rotation, credential-confirmation route, or
durable bind-response replay table in Phase 1.

## 3. Data model and mutation invariants

The logical schema is independent of the backend storage library.

### 3.1 `push_installations`

| Field | Type | Invariant |
| --- | --- | --- |
| `installation_id` | UUID string, primary key | Native generates a UUIDv4 once for this app installation. |
| `account_id` | Supabase subject string | Relay derives it from the account session; clients never supply it. |
| `installation_secret_hash` | Secret-verifier bytes | Relay stores only the verifier for the stable Phase 1 installation credential. |
| `bind_generation` | Positive integer | Native-owned monotonic login generation; accepted binds only move it forward. |
| `device_token_ciphertext` | Nullable protected bytes | APNs token encrypted at rest or held in an equivalent secret store. |
| `device_token_sha256` | Nullable 64-character lowercase hex | Relay-derived redacted equality and diagnostics value. |
| `environment` | Nullable `sandbox` or `production` | Part of the complete provider state and validated against the deployment allowlist. |
| `topic` | Nullable bundle identifier | Part of the complete provider state and validated against the deployment allowlist. |
| `provider_generation` | Non-negative integer | Native-owned monotonic generation; `0` means no provider has been stored. |
| `device_token_invalid_at` | Nullable UTC timestamp | Set only by a matching APNs invalidation result; cleared only by a higher provider generation. |
| `created_at`, `updated_at` | UTC timestamps | Relay-owned audit fields. |

The provider state is the tuple:

```text
(device_token, environment, topic, provider_generation)
```

Provider/token updates are whole-state replacements. APNs-invalid marking
compares the token hash it delivered to, together with generation,
environment, and topic. There is no separate relay-owned `provider_version`
CAS.

`provider_generation` is persisted in the installation's ThisDeviceOnly
Keychain state before a PATCH is sent. It survives process restart and account
rebind and never resets while `installation_id` is reused. Only a brand-new
installation ID begins again at generation `1`.

### 3.2 `push_registrations`

| Field | Type | Invariant |
| --- | --- | --- |
| `registration_id` | 32 random bytes / 43 base64url characters, primary key | Native generates and persists it before the create request; it is immutable and never reused. |
| `installation_id` | UUID foreign key | Exactly one owning installation. |
| `account_id` | Supabase subject snapshot | Copied from the installation at creation for audit and isolation checks. |
| `bind_generation` | Positive integer | Must equal the installation generation at creation and never changes. |
| `vapid_key_sha256` | 64-character lowercase hex | SHA-256 of the decoded 65-byte uncompressed P-256 VAPID public key. |
| `created_at` | UTC timestamp | Write once. |
| `revoked_at` | Nullable UTC timestamp | Write once; the only transition is active to revoked. |

A revoked row is never reactivated. Its endpoint continues to return `410`.
The relay retains rows for Phase 1; automatic installation or registration
retention/deletion is not part of this contract.

Every accepted higher bind generation revokes all lower-generation
registrations in the same transaction as the account binding. A registration
from an older account cannot be reactivated or replaced into the newer
generation.

There is at most one active registration for an
`(installation_id, vapid_key_sha256)` pair. Replacement is explicit: the
caller supplies the concrete current IDs it observed. The set includes every
active ID in the target VAPID scope and may also name the prior VAPID scope for
that instance during key rotation. The relay either revokes exactly those IDs
and inserts the new row in one transaction or returns the current set without
mutation.

### 3.3 Mutation ledger

Every delayed mutation fixes its scope before send with a concrete registration
ID, a captured bind generation, or a persisted provider generation. No delayed
request may select the current account's unbounded future state when it
executes.

| Mutation | Scope fixed before send | Atomic rule |
| --- | --- | --- |
| Account bind or rebind | `installation_id`, authenticated account, persisted `bind_generation` | Validate account, exact Origin, stable credential, ownership update, and lower-generation revocation under one installation lock. Greater G commits; equal G/same account replays; stale or equal G/different account never mutates. |
| Provider PATCH | Persisted `provider_generation` and complete provider tuple | Any generation greater than current replaces the tuple; equal and identical is idempotent; lower or equal and different never mutates. |
| Registration create | Fresh `registration_id`, current `bind_generation`, VAPID hash, concrete `replaces_registration_ids` | Require the generation to equal the installation; the supplied set must cover the target VAPID scope and contain only active current-generation rows owned by the installation; revoke exactly it and insert once. |
| Single revoke | One concrete `registration_id` | Revoke only that owned row; retry is idempotent. |
| Logout bulk revoke | Captured `through_bind_generation` | Revoke only rows for that installation whose generation is at most the captured value; a larger-than-current value is rejected. |
| APNs invalidation | Captured generation plus complete token hash/environment/topic tuple | Mark invalid only while every captured value still matches; otherwise return a retryable race result and do not mutate. |

Native serializes new push mutations through one provider/registration
coordinator, but serialization alone is not the safety boundary: every relay
mutation also enforces the fixed scope above because an already-sent request
can outlive local cancellation. The one same-generation ordering dependency is
explicit: before a logout bulk revoke can be sent or considered successful,
native must reconcile every already-sent registration create for that
generation by retrying its persisted registration ID to a terminal result.
If execution ends first, both operations stay pending and the later foreground
attempt completes create reconciliation before bulk revoke. No new create may
start after logout freezes push mutations.

## 4. Native bridge

The iOS shell advertises `capabilities.push >= 1` and implements the v1 shape
linked from `ios-app.md`:

| Direction | Method or event | Exact shape |
| --- | --- | --- |
| Web -> native | `push.status({vapidPublicKey?: string})` | `{state: "unavailable" \| "off" \| "on", revokeState: "active" \| "absent" \| "pending", endpoint?: string, keys?: {p256dh: string, auth: string}}` |
| Web -> native | `push.subscribe({vapidPublicKey, forceResubscribe?: boolean})` | `{endpoint: string, keys: {p256dh: string, auth: string}}`; `forceResubscribe` explicitly bypasses the active-registration reuse path and creates a concrete replacement |
| Web -> native | `push.unsubscribe()` | `{revoked: boolean, revoke: "confirmed" \| "pending" \| "not_needed"}` |
| Web -> native | `push.openedAck({id})` | `{}`; acknowledges one delivered tap ID from the trusted instance main frame. |
| Native -> Web | `push.opened({id, url})` | Same-origin path event after the target instance loads; native may re-dispatch the same ID until acknowledged. |

`push.status` never creates or replaces a registration. Native decodes and
hashes a supplied `vapidPublicKey` exactly as `push.subscribe` does. It returns
`state: "on"` and the existing public subscription only when that hash matches
the stored active record and that record's `bind_generation` equals the current
native installation generation. A missing or different key, or a lower
generation, returns `state: "off"` without endpoint or keys. The Web-owned
enable/reconcile path may then call `push.subscribe({vapidPublicKey})`. A VAPID
mismatch within the current generation names the old concrete IDs in
`replaces_registration_ids`; a lower-generation record is already relay-revoked
by bind and is retained only for accepted-notification decryption, not named as
an active replacement.

`push.unsubscribe().revoked` is true only after the relay confirmed the
concrete registration was already revoked or is now revoked. False means local
key deletion succeeded and a pending remote revoke remains. `revokeState:
"pending"` is also surfaced by `push.status` with the concrete endpoint so the
Web-owned cleanup saga can retry the same target; `revokeState: "absent"` means
there is no scoped native target and is the only status that permits a
`not_needed` cleanup projection.

The Web UI obtains `vapidPublicKey` from the instance's existing
`GET /api/web-push/vapid-public-key`. While the user has notifications enabled,
the instance Web UI polls `push.status({vapidPublicKey})` on instance-origin
load and later authenticated foreground sessions. It POSTs an `on`
subscription to the existing `POST /api/web-push/subscriptions`; for `off` it
uses the same Web-owned enable/reconcile path to call `push.subscribe` and then
POSTs the result. Native does not initiate a Web call or POST to an instance.
After an explicit native disable, the Web UI persists a versioned intent and
cleanup record in the current instance origin and account website-data store
before destructive remote work. The intent has a monotonic revision and keeps
`userIntent: "disabled"` until an explicit Enable. The cleanup record stores
the capture phase, concrete endpoint, instance DELETE state, and native revoke
state independently. Automatic reconciliation, including `on` re-POST and
pending registration replay, must observe that intent and perform no native or
instance subscription write. Endpoint history remains cleanup evidence and
does not grant opt-in authority. A new record in `capture_pending` may resume
its first endpoint capture; an endpoint-less legacy marker remains fail-closed
and never guesses a target. The marker also stores the concrete endpoint
selected for the instance DELETE, so a later cleanup retry uses that endpoint
even when native state is already `off`.
Only a later explicit enable clears the marker. The control reports the local
opt-out separately from unconfirmed remote cleanup.
Inside the native app the Web UI does not use `PushManager`; browser and
installed-PWA behavior remains unchanged.

The bridge exists only in an instance-origin main frame under the existing
`ios/README.md` and `ios-app.md` origin checks. Private keys, auth-management
credentials, account cookies, and APNs tokens never cross it.

## 5. Relay HTTP API

All management JSON is UTF-8 with `Content-Type: application/json` and a
maximum body size of 64 KiB. Error responses use:

```json
{
  "ok": false,
  "error": {
    "code": "machine_readable_code",
    "message": "operator-safe message"
  }
}
```

Messages never contain account cookies, installation secrets, APNs tokens,
Web Push bodies, plaintext, private key material, or full endpoints.

When the registration-create replacement set changed, the response has this
exact additional shape:

```json
{
  "ok": false,
  "error": {
    "code": "replacement_set_changed",
    "message": "Registration state changed; retry with the returned IDs."
  },
  "active_registration_ids": [
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  ]
}
```

Returning IDs is permitted only after the installation credential has been
verified.

### 5.1 Bind or rebind an installation

```http
PUT /api/push/apns/installations/<installation_id>/binding
Origin: <PUBLIC_BASE_URL origin>
Cookie: <existing account session>
Authorization: AvibeInstallation <installation_id>.<installation_secret>
Content-Type: application/json
```

Request:

```json
{
  "bind_generation": 5
}
```

`bind_generation` is an integer from `1` through `2^63-1`. Native persists it
before sending and reuses it only for retries of the same local sign-in
activation.

The relay authenticates the account cookie and exact Origin before allocating
anything. For a new installation it consumes the authenticated account
creation limit, validates the 32-byte installation secret from Authorization,
creates the row with the authenticated account and supplied generation, stores
the secret verifier, and returns `201`.

For an existing installation, the relay locks the row and validates the
stable credential inside the transaction:

- a generation greater than current sets `account_id` from the authenticated
  session, stores the new generation, and revokes every registration for the
  installation whose `bind_generation` is lower;
- a generation equal to current with the same authenticated account returns
  idempotent `200` without changing registrations or provider state;
- a lower generation, or an equal generation with a different authenticated
  account, returns `409 stale_bind_generation` without mutation.

Binding never changes provider state or `provider_generation`. A delayed bind
from an older account cannot retake the installation after a higher generation
has committed. If the delayed request wins first, the higher generation still
commits afterward and revokes the lower-generation registrations.

Success:

```json
{
  "ok": true,
  "installation_id": "00000000-0000-4000-8000-000000000000",
  "created": false,
  "bind_generation": 5,
  "active_registration_ids": [],
  "provider": {
    "provider_generation": 4,
    "device_token_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "environment": "production",
    "topic": "bot.avibe.ios",
    "invalid": false
  },
  "updated_at": "2026-11-09T00:00:00Z"
}
```

`provider` is `null` before the first provider PATCH. The response never
returns the raw installation secret or device token.
`active_registration_ids` contains only active rows at the returned
`bind_generation`.

A stale response has this exact shape and does not expose either account ID:

```json
{
  "ok": false,
  "error": {
    "code": "stale_bind_generation",
    "message": "A newer or conflicting account bind already exists."
  },
  "installation_id": "00000000-0000-4000-8000-000000000000",
  "bind_generation": 6
}
```

| Status | `error.code` | Meaning |
| --- | --- | --- |
| `400` | `invalid_installation_id`, `invalid_installation_secret`, `invalid_bind_generation`, `invalid_json` | Invalid request. |
| `401` | `login_unauthenticated`, `installation_credential_required`, `installation_unauthorized` | Account authentication failed or could not distinguish upstream unavailability, or installation authentication failed. |
| `403` | `origin_forbidden` | Origin is absent or is not the exact configured origin. |
| `409` | `stale_bind_generation` | The generation is lower, or equal with a different authenticated account. |
| `413` | `request_too_large` | Management body exceeds 64 KiB. |
| `429` | `creation_rate_limited` | New-installation allocation exceeded the authenticated account bucket. |
| `502` | `auth_unavailable` | Optional when account-session validation can distinguish upstream unavailability. |

### 5.2 Replace provider state

```http
PATCH /api/push/apns/installations/<installation_id>/provider
Authorization: AvibeInstallation <installation_id>.<installation_secret>
Content-Type: application/json
```

Request:

```json
{
  "provider_generation": 5,
  "device_token": "<base64url-no-padding>",
  "environment": "production",
  "topic": "bot.avibe.ios"
}
```

This is a complete replacement. Native persists the generation and full
desired tuple before sending, allows at most one provider PATCH in flight, and
retries an ambiguous result with the same generation and tuple.
`provider_generation` is an integer from `1` through `2^63-1`; `device_token`
is non-empty base64url without padding and decodes to at most 100 bytes.
Environment and topic must pass the deployment allowlists.

The relay locks the installation:

- any generation greater than current stores the complete tuple, clears
  `device_token_invalid_at`, and returns `200`; monotonic generations may have
  gaps after a terminally rejected request;
- generation equal to current with an identical tuple returns idempotent
  `200` without clearing a later invalid marker;
- generation lower than current, or equal with a different tuple, returns
  `409 stale_provider_generation` without mutation.

Every new desired provider tuple allocates the next local generation before
sending, even when token bytes are unchanged. This includes an APNs registration
callback and a corrected tuple after a terminal `400`. A higher accepted
generation is the only recovery that clears a provider-invalid marker. Native
never takes a generation from a `409` and retries an obsolete desired tuple. If
a newer local generation exists, it discards the older operation's response. A
new desired tuple that arrives while a PATCH is in flight is queued and uses
the next generation only after the in-flight generation is resolved. If
generation `N+1` receives a terminal `400` without being stored, a later valid
tuple may send `N+2`; the relay accepts that higher generation.

Success:

```json
{
  "ok": true,
  "installation_id": "00000000-0000-4000-8000-000000000000",
  "provider_generation": 5,
  "device_token_sha256": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
  "environment": "production",
  "topic": "bot.avibe.ios",
  "invalid": false,
  "updated_at": "2026-11-09T00:00:00Z"
}
```

A `409` response never returns the token:

```json
{
  "ok": false,
  "error": {
    "code": "stale_provider_generation",
    "message": "A newer or conflicting provider state already exists."
  },
  "installation_id": "00000000-0000-4000-8000-000000000000",
  "provider_generation": 6,
  "device_token_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "environment": "production",
  "topic": "bot.avibe.ios",
  "invalid": false,
  "updated_at": "2026-11-09T00:00:00Z"
}
```

An invalid base64url token, a decoded token longer than 100 bytes, or a provider
tuple outside the environment/topic allowlists returns `400 invalid_provider`
without storing the generation. Other errors are `401
installation_credential_required` or `installation_unauthorized`, `404
installation_not_found`, and `413 request_too_large`.

### 5.3 Create or recover a registration

```http
PUT /api/push/apns/registrations/<registration_id>
Authorization: AvibeInstallation <installation_id>.<installation_secret>
Content-Type: application/json
```

Request:

```json
{
  "bind_generation": 5,
  "vapid_key_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "replaces_registration_ids": [
    "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
  ]
}
```

Native generates and persists the new ID, P-256 key, 16-byte auth secret, VAPID
hash, current bind generation, and concrete replacement IDs before sending.

For a new ID, the relay verifies the credential, checks the authenticated
account creation limit before row allocation, and locks the installation,
target VAPID scope, and supplied replacement rows. The request
`bind_generation` must equal the installation's current value; any mismatch
returns `409 stale_generation` before insertion. The supplied set must include
every current active registration in the target VAPID scope. Every supplied
replacement ID must also be active, belong to the same installation and bind
generation, but it may carry the previous VAPID hash so one `push.subscribe`
operation can rotate an instance key. The relay revokes exactly the supplied
rows and inserts the new row with the current account and bind generation in
one transaction. A missing, revoked, differently owned, stale-generation, or
omitted target-scope ID is reported as `replacement_set_changed` without
mutation.

If the same registration ID already exists and is active with the same
installation, bind generation, and VAPID hash, a lost-response retry returns
`200` without processing a new replacement set. A reused ID with different
ownership or content returns `409 registration_id_reuse`; a row revoked after
creation returns `410 registration_revoked`.

Success is `201` for insertion and `200` for an idempotent replay:

```json
{
  "ok": true,
  "registration_id": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "endpoint": "https://avibe.bot/api/push/apns/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "bind_generation": 5,
  "vapid_key_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "created_at": "2026-11-09T00:00:00Z"
}
```

`endpoint` is generated from `PUBLIC_BASE_URL`; the literal host above is the
first deployment example.

| Status | `error.code` | Meaning |
| --- | --- | --- |
| `400` | `invalid_registration_id`, `invalid_bind_generation`, `invalid_vapid_key_hash`, `invalid_replacement_ids`, `invalid_json` | Invalid request. |
| `401` | `installation_credential_required`, `installation_unauthorized` | Invalid installation credential. |
| `404` | `installation_not_found` | Installation does not exist. |
| `409` | `stale_generation`, `replacement_set_changed`, `registration_id_reuse` | Bind generation is not current, current VAPID scope differs, or the ID was already used for different content. |
| `410` | `registration_revoked` | The idempotency ID exists but is terminally revoked. |
| `413` | `request_too_large` | Management body exceeds 64 KiB. |
| `429` | `creation_rate_limited` | Account creation bucket exhausted before insertion. |

`replacement_set_changed` includes `active_registration_ids`. Native persists
that observed set before a new attempt. `stale_generation` has this exact
additional shape:

```json
{
  "ok": false,
  "error": {
    "code": "stale_generation",
    "message": "The registration bind generation is not current."
  },
  "installation_id": "00000000-0000-4000-8000-000000000000",
  "bind_generation": 6
}
```

Web/native must complete the current account bind before creating another
registration.

### 5.4 Revoke registrations

Single-registration disable:

```http
DELETE /api/push/apns/registrations/<registration_id>
Authorization: AvibeInstallation <installation_id>.<installation_secret>
```

It sets `revoked_at` only on that owned row and returns:

```json
{
  "ok": true,
  "registration_id": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "revoked": true,
  "revoked_at": "2026-11-09T00:00:00Z"
}
```

Retrying an already-revoked owned row is `200`. Unknown or differently owned
IDs are indistinguishable and return `404 registration_not_found`.

Logout bulk revoke:

```http
POST /api/push/apns/installations/<installation_id>/revoke
Authorization: AvibeInstallation <installation_id>.<installation_secret>
Content-Type: application/json
```

```json
{
  "through_bind_generation": 5
}
```

`through_bind_generation` is an integer from `1` through `2^63-1`. Native
captures the current generation before local account teardown and reconciles
every already-sent create at that generation before sending this request.
Under the installation lock, the relay revokes registrations for that
installation whose `bind_generation` is at most the supplied value. A value
equal to or lower than the installation's current generation is valid and
idempotent; a higher value returns mutation-free `409 stale_generation`. The
route never changes account ownership, installation credential, bind
generation, or provider state.

Success is:

```json
{
  "ok": true,
  "installation_id": "00000000-0000-4000-8000-000000000000",
  "through_bind_generation": 5,
  "revoked": true,
  "updated_at": "2026-11-09T00:00:00Z"
}
```

Both routes may return `401 installation_credential_required` or
`installation_unauthorized` or `404 installation_not_found`. The bulk route
also returns `400 invalid_bind_generation` or `invalid_json`, `413
request_too_large`, and `409 stale_generation` using the current-generation
shape from section 5.3.

### 5.5 Deliver encrypted Web Push

```http
POST /api/push/apns/<registration_id>
Authorization: vapid t=<compact-jwt>, k=<base64url-p256-public-key>
Content-Type: application/octet-stream
Content-Encoding: aes128gcm
TTL: 43200
Urgency: normal
Topic: session:ses_123
```

The relay verifies:

1. the JWT is compact ES256 and verifies with `k`;
2. decoded `k` is an uncompressed 65-byte P-256 key whose SHA-256 equals the
   registration's `vapid_key_sha256`;
3. `aud` exactly equals the `PUBLIC_BASE_URL` origin;
4. `exp` is in the future and no more than 24 hours ahead;
5. `Content-Encoding` is exactly `aes128gcm`;
6. the body is at most 2903 bytes;
7. `TTL` is an integer from `0` through `86400`, default `43200`;
8. `Urgency` is absent or `very-low`, `low`, `normal`, or `high`;
9. `Topic` is absent or valid UTF-8 without control characters and at most
   64 UTF-8 bytes.

VAPID signs the JWT header and claims, not the body or path. The random path
selects the registration, VAPID binds it to the instance sender, and RFC 8291
AES-GCM authenticates the encrypted content.

Before sending to APNs, the relay captures
`provider_generation`, `device_token_sha256`, `environment`, and `topic`. A
missing provider returns `503 apns_token_unavailable`. An already invalid
provider returns `410 apns_provider_invalid`.

If APNs returns `410`, `BadDeviceToken`, `DeviceTokenNotForTopic`, or
`Unregistered`, the relay marks the provider invalid only if all captured
values still match the current row and `device_token_invalid_at` is null. A
matching invalidation returns `410 apns_device_gone`. Any changed generation,
token hash, environment, topic, or validity marker returns retryable `503
apns_provider_changed` and cannot invalidate the newer provider.

Success after APNs accepts the request is `201`:

```json
{
  "ok": true,
  "registration_id": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "apns_status": 200
}
```

| Relay status | `error.code` | Condition |
| --- | --- | --- |
| `400` | `invalid_vapid`, `invalid_audience`, `vapid_expired`, `vapid_exp_too_far`, `invalid_content_encoding`, `invalid_ttl`, `invalid_urgency`, `invalid_topic`, `invalid_request` | Request rejected; registration stays active. |
| `410` | `registration_not_found`, `registration_revoked`, `vapid_key_mismatch` | Missing, revoked, or sender-key-mismatched endpoint; instance disables it. |
| `410` | `apns_device_gone`, `apns_provider_invalid` | Matching APNs invalidation or provider already invalid; instance disables it. |
| `413` | `encrypted_body_too_large`, `apns_payload_too_large` | Encrypted body or final APNs JSON exceeds its limit. |
| `429` | `relay_rate_limited` | Per-registration delivery bucket exhausted; includes `Retry-After`. |
| `502` | `apns_bad_gateway` | Non-retryable upstream/provider configuration or protocol error. |
| `503` | `apns_unavailable`, `apns_timeout`, `apns_upstream_rate_limited`, `apns_provider_changed`, `apns_token_unavailable` | Retryable upstream failure, state race, or missing token. |

APNs `PayloadTooLarge` maps to `413 apns_payload_too_large`; APNs `429` and
retryable `5xx` responses map to the corresponding `503` class. Provider
authentication and other non-device protocol/configuration failures map to
`502`.

APNs response text is classified and redacted. It is never passed through to
the instance or logs.

### 5.6 APNs mapping

The relay uses HTTP/2. It creates an ES256 provider token from the configured
`.p8` key, caches it, and refreshes it every 20-60 minutes and after a provider
authentication failure.

| APNs field | Value |
| --- | --- |
| Host | `https://api.sandbox.push.apple.com` for `sandbox`; `https://api.push.apple.com` for `production` |
| `:path` | `/3/device/<protected device token>` |
| `apns-push-type` | `alert` |
| `apns-topic` | Allowlisted installation topic |
| `apns-priority` | `10` for absent, `normal`, or `high` urgency; `5` for `low` or `very-low` |
| `apns-expiration` | `0` for `TTL: 0`; otherwise current Unix time plus TTL |
| `apns-collapse-id` | If Web Push `Topic` exists, `base64url_no_pad(SHA-256(registration_id_bytes || 0x00 || UTF-8(Topic)))`; otherwise omitted. The result is 43 ASCII bytes, below APNs's 64-byte limit. |

The compact UTF-8 APNs body has exactly this shape:

```json
{"aps":{"alert":{"loc-key":"AVIBE_PUSH_FALLBACK"},"mutable-content":1,"sound":"default"},"avibe":{"v":1,"kid":"<registration_id>","ct":"<base64url body>"}}
```

It never exposes title, body, URL, badge, account, or instance origin outside
the ciphertext.

### 5.7 Abuse controls

Phase 1 adds two push-specific token buckets using the backend's existing
throttling pattern:

| Scope | Rate and burst | Applied before |
| --- | --- | --- |
| Authenticated account creation | 30 allocations/minute, burst 10 | Creating an installation or inserting a new registration row |
| Registration delivery | 60 requests/minute, burst 10 | Opening an APNs request for one registration |

The account key is derived from the authenticated session for installation
creation and from the credential-authenticated installation row for
registration creation. A fresh random ID cannot bypass the account bucket.
Idempotent recovery of an already-created row does not allocate and therefore
does not consume the creation bucket.

Unknown or revoked delivery endpoints return `410` before APNs. A delivery that
reaches validation consumes the registration bucket whether it later succeeds,
is oversized, or is rejected. `429` includes `Retry-After`.

Normal ingress body, connection, and authentication protections still apply,
but Phase 1 does not add IP-prefix, installation-wide, or account-wide delivery
buckets.

## 6. Instance payload and byte budget

The canonical logical payload remains:

```json
{
  "title": "Project name",
  "body": "A notification preview",
  "url": "/chat/ses_123?msg=msg_456&from=notification",
  "tag": "session:ses_123",
  "message_id": "msg_456",
  "session_id": "ses_123",
  "badge_count": 3,
  "category": "sessions"
}
```

`category` is optional. The complete object may contain compatible extra
fields, but it must satisfy the same byte limit.

The instance MUST:

- serialize with
  `json.dumps(payload, ensure_ascii=False, separators=(",", ":"))`;
- UTF-8 encode before encryption;
- limit `title` to 80 Unicode scalar values;
- limit `body` to 240 Unicode scalar values;
- limit `tag`, when sent as Web Push `Topic`, to 64 UTF-8 bytes without control
  characters;
- truncate at a valid Unicode scalar boundary, preferably a grapheme boundary;
- reject or truncate the complete serialized plaintext above 2800 UTF-8 bytes;
- enforce the same limit for browser and iOS subscriptions.

`badge_count` is computed per subscription owner immediately before send.

| Component | Bytes |
| --- | ---: |
| RFC 8188/8291 encrypted-content header | 86 |
| Padding delimiter | 1 |
| AES-GCM authentication tag | 16 |
| Maximum serialized plaintext | 2800 |
| Maximum encrypted Web Push body | 2903 |
| Base64url without padding | 3871 |
| Fixed APNs envelope with 19-byte fallback key and 43-byte `kid` | 165 |
| Maximum APNs payload | 4036 |
| Margin below APNs 4096-byte limit | 60 |

The theoretical maximum under this exact envelope is 2844 plaintext bytes and
4095 APNs bytes. The 2800-byte invariant keeps a 60-byte implementation margin.
The relay rejects an encrypted body over 2903 bytes or final APNs JSON over
4096 bytes. Adding outer fields or whitespace requires a new budget.

The instance settings list labels an endpoint beginning with
`https://avibe.bot/api/push/apns/` as `iPhone App`. This is the current
`PUBLIC_BASE_URL` prefix, not a user-agent or client-supplied label.

## 7. Native keys and NSE

For each successful subscription of an `(account_id, instance_origin)` pair,
native creates:

- a P-256 ECDH key pair;
- a random 16-byte Web Push auth secret;
- a fresh registration ID;
- a shared Keychain record keyed by that ID.

The main app and NSE are both provisioned to use the same dedicated Keychain
access group:

```text
$(AppIdentifierPrefix)bot.avibe.ios.push
```

Both explicitly set that value as `kSecAttrAccessGroup` when reading or
writing push records. This is the contract's chosen shared-secret boundary,
not a claim that Apple requires this exact group name or requires it in
addition to every App Group design. The App Group `group.bot.avibe.ios` is
separately used for the small shared account-generation marker and extension
coordination. Both targets must be provisioned for whichever shared groups
they use.

Private-key and auth-secret items use
`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`. Before first unlock after
boot, the NSE keeps the fallback alert.

A key record contains:

```text
kid
instance_origin
account_id
account_generation
bind_generation
private_key
auth_secret
state: pending | active | retiring
```

The installation credential is not in this access group and is unavailable to
the NSE. Public `p256dh` is the uncompressed 65-byte P-256 point; `p256dh` and
`auth` are base64url without padding.

At most one record for an `(account_id, instance_origin)` pair is active.
During an explicit replacement, native first gets the new subscription stored
by the instance, then revokes and deletes the old instance endpoint. After the
old relay revoke is confirmed, its key may remain `retiring` for the maximum
accepted Web Push TTL of 86400 seconds so an already accepted APNs request can
still decrypt. A pending revoke keeps the exact old ID and key until
confirmation. Disable, logout, and account switch follow their stricter local
deletion rules below.

The NSE:

1. reads `avibe.kid`;
2. finds the shared Keychain record;
3. base64url-decodes `avibe.ct`;
4. performs real RFC 8291 `aes128gcm` decryption;
5. parses UTF-8 JSON;
6. sets title, body, `badge_count`, and `threadIdentifier` from `tag`;
7. writes `url`, `instance_origin`, `account_id`, and `kid` to `userInfo`;
8. checks the shared account generation again immediately before completing.

Lookup, Keychain, decoding, decryption, generation, or JSON failure preserves
the localized `AVIBE_PUSH_FALLBACK` alert. The implementation does not request
Apple's filtering entitlement.

The final generation check reduces the chance that an NSE which started before
logout publishes old plaintext afterward. It cannot recall content already
handed to the system, erase plaintext already held by an in-flight extension,
or remove an already displayed alert; lifecycle behavior below does not claim
otherwise.

While the app is in the foreground, only the test notification is shown: the
instance's `/api/web-push/test` send carries the tag `web-push-test`, which
step 6 puts in `threadIdentifier`, and the app's notification-center delegate
shows that one as a banner, in Notification Center, and with its sound. Every
other notification stays silent in the foreground, as iOS does by default; the
Inbox and the badge in the app already show it. A fallback alert has no thread
(the APNs envelope above carries none), so it stays silent too. The delegate
never applies a push's badge in the foreground: the Web UI sets the live Inbox
total through `badge.set` there, and a push's `badge_count`, computed when it
was sent, can only be as recent or older.

When the selected instance's main frame sets `badge.set({count: 0})`, the
loud unread total there is zero, and the app removes that instance's and the
current account's delivered notifications whose `threadIdentifier` starts with
`session:` (matched on `userInfo`'s `instance_origin` and `account_id`). Only
notifications delivered before the call arrived are removed: one delivered
after it is news the zero did not cover. Pending requests, other instances,
other accounts, the test notification, `harness:failure` notices (not part of
the Inbox's unread total, so a zero says nothing about them), and undecrypted
fallbacks (no thread, origin, or account) stay. A Session read while others
remain unread keeps its notifications until the total reaches zero; the App
cannot see which Session was read from the URL or the total alone.

## 8. Tap routing

On tap, native compares the notification's account and instance origin with
the current account and its configured instance list.

For a current account and available instance it selects the instance, waits for
the primary WebView to load, normalizes `url` as a same-origin path, and emits
`push.opened({id, url})`. Native retains one pending tap for a bounded lifetime
and may re-dispatch it after later primary-document loads until the trusted Web
listener acknowledges that ID with `push.openedAck({id})`. The Web UI accepts
only a path, query, and fragment for the selected origin. Absolute URLs,
another scheme or origin, and control characters are rejected.

For an account mismatch or unavailable instance, the app opens the instance
list and shows the existing recovery message. It never sends the URL to another
instance or Safari. A fallback notification that has no decrypted account,
origin, or URL also takes this recovery path.

## 9. End-to-end lifecycle

### 9.1 Field responsibility

| Field | Produced by | Consumed by | Signed or authenticated coverage | Supplied by |
| --- | --- | --- | --- | --- |
| `installation_id` | Native CSPRNG/UUIDv4 | Relay management; native coordinator | HTTPS plus installation credential on every management request | ThisDeviceOnly Keychain |
| `installation_secret` | Native CSPRNG | Relay credential verifier | HTTPS Authorization; relay stores verifier only | Native Keychain |
| `bind_generation` | Native account coordinator | Relay bind ordering, registration create, bulk revoke | Same-origin account bind plus installation credential; later management uses installation credential | Persisted native installation state |
| `account_id` | Relay from account session | Installation and registration ownership; native local account state | Same-origin cookie plus exact Origin on bind | Existing account authority |
| `provider_generation` | Native provider coordinator | Relay PATCH and APNs invalidation CAS | Installation-credential request; relay monotonic comparison | Persisted native provider state |
| `device_token` | APNs callback | Relay APNs request builder | Installation-credential PATCH; protected at rest | APNs |
| `environment`, `topic` | Build/bundle configuration | Relay host and `apns-topic` | Installation-credential PATCH plus deployment allowlists | Native target |
| `registration_id` | Native CSPRNG | Relay path, endpoint, APNs `kid`, NSE lookup | Installation-credential create request | Pending/shared Keychain record |
| `vapidPublicKey` | Instance `GET /api/web-push/vapid-public-key` | Native status comparison and subscribe | Exact-origin bridge call in the authenticated instance session | Instance Web UI |
| `vapid_key_sha256` | Native from decoded `vapidPublicKey` | Native active-record comparison; relay registration and delivery verifier | Installation-credential request; checked against VAPID `k` | Native status/registration coordinator |
| `p256dh`, `auth` | Native | Instance encryption and NSE decryption | HTTPS subscription POST; RFC 8291 inputs | Native public subscription |
| Notification fields and `badge_count` | Instance | NSE after decryption | RFC 8291 AES-GCM | Existing instance delivery path |
| VAPID JWT | Instance | Relay | RFC 8292 ES256 over JWT header and claims | Instance VAPID private key |
| `avibe.ct` | Relay base64url encoder | NSE | Contains unchanged RFC 8291 authenticated body | Relay from request bytes |
| Tap `url`, instance origin | Instance payload plus native key record | Native and Web UI | AES-GCM for URL; native account/origin checks | NSE `userInfo` |

### 9.2 Enable

1. After account login, `MobileAccountCoordinator` uses a short-lived hidden
   transport backed by the account's persistent website data store. Native
   persists the next bind generation before sending; retries for that sign-in
   reuse it. The relay creates or binds the installation, and session expiry is
   shown as a recoverable failure.
2. A greater bind generation atomically establishes the account and revokes
   lower-generation registrations. An equal-generation replay for the same
   account is idempotent. Native no longer reports lower-generation key records
   as active subscriptions.
3. On notification enable or the next authenticated foreground reconcile, Web
   calls `push.status({vapidPublicKey})`; an `off` result proceeds through
   `push.subscribe({vapidPublicKey})`.
4. Native obtains permission and an APNs token. It persists and PATCHes the
   next provider generation if needed.
5. Native creates and persists a pending P-256 key, auth secret, registration
   ID, current bind generation, VAPID hash, and concrete replacement set.
6. The relay either atomically replaces that exact VAPID scope or returns its
   current IDs for a captured retry.
7. Native marks the key record active and returns the public subscription.
8. Web POSTs it to the instance's existing subscription route.

If the registration response is lost, native retries the same ID. If the
instance POST fails, native keeps the operation recoverable; a later explicit
replacement uses a new ID and names the old ID. It never reuses or silently
selects an endpoint.

### 9.3 Delivery and provider recovery

1. The instance applies existing gates, computes badge, serializes under the
   byte budget, encrypts, signs, and POSTs.
2. The relay validates the bound VAPID sender and forwards only ciphertext. A
   VAPID key mismatch returns terminal `410`, so the instance disables that
   stale subscription.
3. APNs invokes the NSE, which decrypts and presents or keeps the fallback.
4. APNs invalidation returns relay `410` only for the captured current provider;
   the instance follows its existing terminal disable path.
5. A later APNs callback persists and PATCHes a higher provider generation.
6. On the next authenticated instance-origin load or foreground session while
   notifications remain enabled, Web fetches the current VAPID key and calls
   `push.status({vapidPublicKey})`. A matching `on` result is re-POSTed to
   re-enable the instance row after provider recovery. This does not create a
   registration or rotate keys.
7. If the current VAPID key differs, status returns `off`; the same Web-owned
   reconcile path calls `push.subscribe({vapidPublicKey})`, whose fresh
   registration replaces the captured old concrete IDs, then POSTs the new
   public subscription. Native never invents a native-to-Web recovery event or
   posts directly to the instance.

Failed reconciliation remains recoverable at a later authenticated
instance-origin foreground opportunity. Provider PATCH success alone does not
promise background instance re-enablement.

### 9.4 Tap

The NSE writes the decrypted path and native instance/account context into
`userInfo`. On tap, native applies section 8's account, instance-list, and
same-origin checks, loads the selected instance, and emits
`push.opened({url})`. Missing decrypted context takes the instance-list
recovery path.

### 9.5 Disable

Web calls `push.unsubscribe()`, native captures the exact registration ID,
deletes its private/auth key, and attempts the single-ID relay revoke. Web also
deletes the endpoint from the instance. An offline relay failure leaves a
pending revoke for that ID and returns `revoked: false`; later subscriptions
use fresh IDs, so retry cannot select them.
The Web UI records its durable opt-out and the concrete instance endpoint
before these destructive calls. A cleanup retry replays the instance deletion
against that captured endpoint even if native already reports `off`; a marker
without an endpoint fails closed rather than choosing from endpoint history. A
`revoked: false` result is shown as cleanup pending, while a bridge transport
failure is shown as an unconfirmed cleanup failure; neither result permits
automatic re-subscription. A later explicit enable clears the opt-out and
re-enters the existing registration flow.

### 9.6 Logout and account switch

Before local account teardown, native freezes new push mutations, captures all
active registration IDs and the current bind generation, persists the bounded
bulk-revoke operation, increments the shared account generation, deletes old
account key records, and best-effort removes pending and delivered
notifications whose `kid` is in the captured set.

Before sending the bulk revoke, native reconciles every already-sent
registration create at the captured generation by retrying its persisted ID.
Only then can a successful bulk response complete remote logout. If execution,
authorization, or create reconciliation is unavailable, logout continues
locally and the ordered create-then-revoke work remains pending. A later
foreground opportunity uses the stable installation credential; it does not
retain or recreate the old account cookie.

For a new account, native persists a higher bind generation before sending the
account-authenticated bind. The successful transaction changes ownership and
revokes every registration from a lower generation. If a delayed old-account
bind runs afterward, its lower generation returns
`409 stale_bind_generation`; if it runs first, the higher new-account bind
still commits afterward. A delayed registration create with the old generation
returns `409 stale_generation`, a delayed bulk revoke through the old
generation cannot select newer rows, and a concrete single-ID revoke cannot
name a fresh registration ID.

The accepted Phase 1 limitation is explicit:

- connectivity alone does not schedule iOS execution;
- until a remote revoke succeeds, the old endpoint may continue receiving
  generic and possibly audible fallback alerts without a fixed deadline;
- revocation cannot recall an APNs request already accepted or an alert already
  delivered;
- deleting a key does not erase plaintext already held by an in-flight NSE;
- local generation checks and notification-center cleanup are best effort, not
  retroactive suppression.

The installation credential and bind generation remain in ThisDeviceOnly
Keychain while the installation identity is reused. They are not deleted on
logout, because they authorize the bounded revoke and the next
account-authenticated bind.

### 9.7 Token rotation, reinstall, and uninstall

Every APNs registration callback writes a higher provider generation and full
tuple before PATCH. Process restart resumes the same pending generation. An
older PATCH response or APNs invalidation cannot overwrite it. Provider
replacement changes only the installation row; it never creates, revokes, or
reactivates a registration.

On reinstall detection, native treats app-container state as new even if
Keychain items survived uninstall. It captures any surviving old
installation credential and bind generation into a minimal orphaned revoke
record that remains in ThisDeviceOnly storage, deletes old decryption keys,
and creates a fresh installation ID, installation secret, and generation
sequence for the new install. It may retry the orphaned generation-bounded
revoke later, but does not reuse old registration IDs or keys.

If no old credential survives, remote cleanup cannot be initiated by the app.
Uninstall has no reliable callback. The first matching APNs invalid-token
result returns relay `410`, and the instance disables that endpoint. Phase 1
does not promise a deletion deadline or automatically delete retained relay
rows.

## 10. Security model

This is a new encrypted-content forwarding channel through `avibe.bot`.
Product and security review are required before implementation merge or
launch under `avibe-backend/AGENTS.md`, even though the relay cannot decrypt
the content.

| Threat | Phase 1 invariant |
| --- | --- |
| Open-relay abuse | Delivery requires a random stored registration, matching bound VAPID key, exact audience, bounded JWT expiry, strict encoding/body checks, and a per-registration rate limit. Clients cannot supply an APNs token or routing topic at delivery. |
| Resource spraying | Installation and registration allocation share an authenticated-account creation bucket checked before allocation. |
| Cross-account mutation | Account ownership changes require the exact account Origin/session plus the stable installation credential. Native persists bind generation before send; a greater generation changes ownership and revokes lower-generation registrations atomically, while stale or equal/different-account binds cannot mutate. |
| Delayed destructive request | Single revokes carry concrete IDs; bulk revoke carries a captured upper generation. Registration create must match the current generation. Fresh IDs plus these generation bounds prevent old-account operations from selecting newer-account registrations. |
| Stale provider write or invalidation | Native persists one monotonic generation; PATCH replaces the full tuple; APNs invalidation compares generation, token hash, environment, and topic. |
| Installation credential leakage | The credential is deliberately stable until reinstall and usable after cookie logout. It is restricted to one installation's push management, stored ThisDeviceOnly, hashed at the relay, and excluded from Web/NSE/logs. Leakage can disrupt that installation's push state or create rate-limited fallback-producing registrations, but cannot authenticate an account, read notification content, choose a non-allowlisted topic/environment, or manage another installation. Rotation is a Phase 2 candidate if real use requires reducing this lifetime. |
| Cross-instance delivery | Registration stores the decoded VAPID public-key hash and every delivery must prove the matching ES256 key. |
| APNs token leakage | Token is encrypted at rest, sent only to APNs over TLS, and represented elsewhere only by SHA-256. |
| Content or log leakage | Relay never decrypts or stores body bytes and redacts authorization, full endpoint, device token, APNs body, ciphertext, and plaintext from logs, traces, and errors. |
| Wrong APNs routing | Environment and topic are complete provider-state fields validated against deployment allowlists; delivery cannot override them. |
| Notification replay | Phase 1 does not store a ciphertext dedup key. An exact replay can cause a duplicate alert only while it passes VAPID expiry and the per-registration limit; it cannot bypass content encryption or account/VAPID binding. |
| Tap injection | Native checks account and instance availability; Web accepts only a same-origin path. |
| Logout privacy gap | Keys and matching notification-center entries are removed best effort, while the generic fallback and lack of an execution deadline are explicit accepted limitations. |

## 11. Shared fixtures and acceptance

### 11.1 RFC 8291 Appendix A

Instance crypto tests, relay byte-preservation tests, and NSE decryption tests
share fixture ID `rfc8291-appendix-a` from
[RFC 8291 Appendix A](https://www.rfc-editor.org/rfc/rfc8291.html#appendix-A):

| Input or expected value | Base64url without padding |
| --- | --- |
| `plaintext` | `V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24` |
| `as_public` | `BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8` |
| `as_private` | `yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw` |
| `ua_public` | `BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4` |
| `ua_private` | `q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94` |
| `salt` | `DGv6ra1nlYgDCS1FRnbzlw` |
| `auth_secret` | `BTBZMqHH6r4Tts7J_aSIgg` |
| `ecdh_secret` | `kyrL1jIIOHEzg3sM2ZWRHDRB62YACZhhSlknJ672kSs` |
| `PRK_key` | `Snr3JMxaHVDXHWJn5wdC52WjpCtd2EIEGBykDcZW32k` |
| `IKM` | `S4lYMb_L0FxCeq0WhDx813KgSYqU26kOyzWUdsXYyrg` |
| `PRK` | `09_eUZGrsvxChDCGRCdkLiDXrReGOEVeSCdCcPBSJSc` |
| `CEK` | `oIhVW04MRdy2XN9CiKLxTg` |
| `NONCE` | `4h_95klXJ5E_qnoN` |
| `encrypted_header` | `DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8` |
| `ciphertext` | `8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ` |

Tests compare decoded bytes and all intermediate values, including the
86-octet header. Private fixture values are test-only.

### 11.2 Three acceptance properties

1. **Bound sender and opaque relay.** This real payload is serialized with
   `ensure_ascii=False`, encrypted by the instance, sent over relay HTTP,
   wrapped by a mock APNs HTTP/2 boundary, and decrypted by the NSE:

   ```json
   {"title":"任务完成","body":"已完成：你好，世界 🌏","url":"/chat/ses_123?msg=msg_456&from=notification","tag":"session:ses_123","message_id":"msg_456","session_id":"ses_123","badge_count":3,"category":"sessions"}
   ```

   The final title, body, badge, thread, URL, and instance origin match; a
   different VAPID sender is rejected; the relay test has no plaintext input
   and proves the ciphertext bytes are unchanged.

2. **Generation-bounded lifecycle and recovery.** Tests drop successful bind
   and registration-create responses, restart native, and prove same-generation
   bind replay plus same-registration-ID recovery. A race holds account A's
   bind at generation `G`, commits account B's bind at `G+1`, and proves the
   delayed A bind returns mutation-free `409`. The inverse order must converge
   to B when `G+1` commits. A delayed create at `G` returns `409`, a bulk revoke
   through `G` cannot revoke a registration at `G+1`, and a concrete old-ID
   revoke cannot name a fresh row. When the app remains signed out, an
   ambiguous create at `G` is reconciled by its persisted ID before the bulk
   revoke through `G`; a successful logout revoke therefore cannot be followed
   by that older create. A later authenticated Web-owned reconcile re-POSTs an
   existing subscription after provider recovery; after a VAPID hash change it
   observes `off`, creates a fresh current-generation registration that
   replaces the captured old concrete IDs, and POSTs the replacement.

3. **Newest provider wins.** Provider generation and desired tuple survive a
   process restart and account rebind. A terminally rejected `N+1` is not
   stored, and a later valid `N+2` succeeds because monotonic generations may
   have gaps. Once `N+2` is accepted, a generation `N` or `N+1` PATCH cannot
   overwrite it, and an APNs invalid result captured for an older generation or
   for a different token/environment/topic returns retryable `503` without
   marking `N+2` invalid.

A direct function call that bypasses the instance HTTP, relay HTTP, mock APNs,
or NSE crypto boundary does not satisfy the first property.
The same non-ASCII payload must also pass the browser Web Push budget check, so
Unicode escaping cannot change its text or silently exceed the shared limit.

## 12. Dependencies, lanes, and Phase 2

### 12.1 Apple and deployment prerequisites

| Dependency | Phase | Code development | Device or launch |
| --- | --- | --- | --- |
| Contract merged to `master` | Contract gate | Blocks both implementation branches | Blocks integration |
| Account Holder creates the main app and NSE App IDs | Device setup | Does not block mock/local code | Blocks signed NSE push |
| Account Holder enables Push Notifications for the main app and NSE | Device setup | Does not block mock code | Blocks APNs token/device delivery |
| Account Holder creates App Group `group.bot.avibe.ios` and provisions the chosen shared Keychain access for both targets | Device setup | Does not block isolated crypto tests | Blocks device NSE decryption |
| Account Holder creates the APNs `.p8` provider key | Integration | Does not block mock APNs | Blocks relay-to-Apple testing |
| Product and security review | Before implementation merge/launch | Does not block isolated development | Blocks implementation merge and launch |

The existing main-app TestFlight signing/upload path proves only the main app
pipeline. It does not prove Push Notifications, an NSE target, shared
entitlements, APNs delivery, or Apple-side configuration. Those remain explicit
device gates.

The relay uses these environment/configuration names; values are never
committed:

```text
APNS_PRIVATE_KEY
APNS_KEY_ID
APNS_TEAM_ID
APNS_TOPIC
APNS_ENVIRONMENT
PUBLIC_BASE_URL
```

The APNs values are supplied by the deployment configuration. `APNS_TOPIC` and
`APNS_ENVIRONMENT` define deployment allowlists, not arbitrary client routing.
The current deployment is production-only. TestFlight and App Store use
`production` and are in Phase 1 validation scope. `sandbox` remains a protocol
value for Xcode Debug builds, but Debug-device push waits until the backend
owner enables a sandbox provider.

### 12.2 Lane order and mock boundary

1. Merge this contract.
2. Backend relay and Avibe implementation lanes branch from that merged commit.
3. Backend owns account binding, credential verification, storage, provider and
   registration APIs, VAPID validation, APNs mapping, abuse controls, redaction,
   and a mock APNs HTTP/2 server.
4. Avibe owns instance serialization/labeling, native coordinator and Keychain
   lifecycle, bridge, NSE, tap routing, and a mock relay.
5. Integration connects the real instance serializer/encryptor to relay HTTP,
   mock APNs HTTP/2, real NSE decryption, and tap-origin validation.

The lanes may develop in parallel after contract merge. Their mocks must use
the exact fields and status table here; neither may replace the final
cross-boundary scenario with direct calls.

### 12.3 Phase 2 candidates, not Phase 1 guarantees

The following are deferred proposals. Evidence may later justify them, but
implementations must not assume them now:

- an installation hard cap or eviction policy;
- extra IP, installation, or account delivery buckets;
- ciphertext replay deduplication;
- automatic registration sweep or installation retention/deletion;
- leases, expiry, or renewal to impose a remote-stop deadline;
- installation-credential rotation if operational evidence justifies reducing
  the accepted stable-credential lifetime;
- richer cleanup for orphaned reinstall records.

Phase 1 deliberately has no epoch, durable bind-response replay table,
pending-secret recovery protocol, credential-confirmation GET,
five-installation cap, seven-day sweep, or ciphertext-dedup table.

## 13. Release gates

Implementation is ready for integration only when:

- the three acceptance properties pass across the owned boundaries;
- all management routes enforce the mutation ledger, bind-generation ordering,
  and idempotent lost-response recovery;
- payload math, APNs mapping, response codes, Keychain access, fallback, and
  tap-origin checks match this contract;
- the current deployment allowlist is production-only while protocol tests
  cover both environment values;
- relay logs and errors pass a security review for token, credential,
  endpoint, ciphertext, and plaintext redaction;
- product and security review approve the new encrypted-content forwarding
  channel before implementation merge or launch.

This contract does not authorize Apple configuration, secret changes,
deployment, implementation PR merge, or this PR's merge.
