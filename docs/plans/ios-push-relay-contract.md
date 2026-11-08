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

### 2.2 Installation push credential

All other management routes use a rotatable, installation-scoped credential:

```http
Authorization: AvibeInstallation <installation_id>.<installation_secret>
```

`installation_secret` is 32 random bytes encoded as base64url without padding.
Native generates it, stores it in a non-App-Group
`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` Keychain item, and never
exposes it to Web content, an instance, the bridge, or the NSE. The relay stores
only a password-hash or keyed-hash representation suitable for bearer-secret
verification. Logs and telemetry contain neither the raw value nor its
reversible representation. Verification uses a constant-time comparison after
the same normalization and hash derivation.

Possession authorizes push management for exactly one installation. It does
not grant account access, read notification content, or manage another
installation. This narrow credential is retained only when needed to finish a
pending revoke or rebind after the account cookie has expired or been cleared.
It supplies authorization, not background execution.

The path installation ID and authorization-header installation ID must match.
Credential validation, account ownership changes, old-registration revocation,
and credential rotation happen under one locked installation transaction.

### 2.3 Recoverable credential rotation

Native permits at most one credential-changing operation per installation. It
persists `current_installation_secret` and a newly generated
`pending_installation_secret` before sending the request.

The relay accepts the current secret to perform a mutation. It accepts the
pending secret only as an idempotent replay when the stored hash already
matches it and the requested postcondition is already true. Therefore an
ambiguous or lost success response is recovered without a bind replay table:

1. retry the exact request using the pending secret;
2. if it succeeds, the first request committed;
3. if it returns `401` (or `404` during first creation), retry with the current
   secret, or without a credential for a still-absent first installation;
4. promote the pending secret only after an acknowledged success.

Both values remain in Keychain across process restart until one state is
confirmed. Account switching waits for this recovery; it never starts a second
credential mutation from an uncertain secret.

For both credential-changing routes, the relay checks the pending-secret
postcondition before comparing the request's old revocation set with the
current active set. A committed request may have made that old set empty; this
ordering is what makes the lost response idempotent.

## 3. Data model and mutation invariants

The logical schema is independent of the backend storage library.

### 3.1 `push_installations`

| Field | Type | Invariant |
| --- | --- | --- |
| `installation_id` | UUID string, primary key | Native generates a UUIDv4 once for this app installation. |
| `account_id` | Supabase subject string | Relay derives it from the account session; clients never supply it. |
| `installation_secret_hash` | Secret-verifier bytes | Relay stores only the verifier for the current installation credential. |
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
| `vapid_key_sha256` | 64-character lowercase hex | SHA-256 of the decoded 65-byte uncompressed P-256 VAPID public key. |
| `created_at` | UTC timestamp | Write once. |
| `revoked_at` | Nullable UTC timestamp | Write once; the only transition is active to revoked. |

A revoked row is never reactivated. Its endpoint continues to return `410`.
The relay retains rows for Phase 1; automatic installation or registration
retention/deletion is not part of this contract.

An account change on a device revokes all of that device's existing
registrations in the same transaction as the bind. The client names the
concrete set and the relay rejects a changed set instead of choosing a
different set at execution time.

There is at most one active registration for an
`(installation_id, vapid_key_sha256)` pair. Replacement is explicit: the
caller supplies the concrete current IDs it observed, and the relay either
revokes exactly those IDs and inserts the new row in one transaction or
returns the current set without mutation.

### 3.3 Mutation ledger

No delayed request may select an open-ended destructive scope when it executes.
If current state differs from the caller's captured concrete IDs, the relay
returns a conflict so native can persist the newly observed IDs before sending
a new request.

| Mutation | Scope fixed before send | Atomic rule |
| --- | --- | --- |
| Account bind or rebind | `installation_id`, authenticated account, next credential, concrete `revoke_registration_ids` | Validate account, exact Origin, current/pending credential, revocation set, ownership update, revocations, and credential rotation under one installation lock. An account change requires the supplied IDs to equal the current active set. |
| Provider PATCH | Persisted `provider_generation` and complete provider tuple | Exactly `current + 1` replaces the tuple; equal and identical is idempotent; every other value never mutates. |
| Registration create | Fresh `registration_id`, VAPID hash, concrete `replaces_registration_ids` | The supplied replacement set must equal the active set for that VAPID scope; revoke exactly it and insert once. |
| Single revoke | One concrete `registration_id` | Revoke only that owned row; retry is idempotent. |
| Logout bulk revoke | Complete captured active ID set plus next credential | Require the supplied set to equal the current active set, revoke exactly it, and rotate the credential in one transaction. |
| APNs invalidation | Captured generation plus complete token hash/environment/topic tuple | Mark invalid only while every captured value still matches; otherwise return a retryable race result and do not mutate. |

Native serializes new push mutations through one provider/registration
coordinator, but serialization alone is not the safety boundary: every relay
mutation also enforces the fixed scope above because an already-sent request
can outlive local cancellation.

## 4. Native bridge

The iOS shell advertises `capabilities.push >= 1` and implements the v1 shape
linked from `ios-app.md`:

| Direction | Method or event | Exact shape |
| --- | --- | --- |
| Web -> native | `push.status()` | `{state: "unavailable" \| "off" \| "on", endpoint?: string, keys?: {p256dh: string, auth: string}}` |
| Web -> native | `push.subscribe({vapidPublicKey})` | `{endpoint: string, keys: {p256dh: string, auth: string}}` |
| Web -> native | `push.unsubscribe()` | `{revoked: boolean}` |
| Native -> Web | `push.opened({url})` | Same-origin path event after the target instance loads. |

For `state: "on"`, `push.status()` returns the existing public subscription
without creating a registration or rotating keys. For other states, endpoint
and keys are absent. `push.unsubscribe().revoked` is true only after the relay
confirmed the concrete registration was already revoked or is now revoked.
False means local key deletion succeeded and a pending remote revoke remains.

The Web UI obtains `vapidPublicKey` from the instance's existing
`GET /api/web-push/vapid-public-key`, calls the native bridge when the `push`
capability exists, then POSTs the returned standard subscription to the
existing `POST /api/web-push/subscriptions`. Inside the native app it does not
use `PushManager`; browser and installed-PWA behavior remains unchanged.

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

When a concrete registration set changed, the response has this exact
additional shape:

```json
{
  "ok": false,
  "error": {
    "code": "registration_set_changed",
    "message": "Registration state changed; retry with the returned IDs."
  },
  "active_registration_ids": [
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  ]
}
```

The registration-create route uses `replacement_set_changed` in the same
shape. Returning IDs is permitted only after the installation credential has
been verified.

### 5.1 Bind or rebind an installation

```http
PUT /api/push/apns/installations/<installation_id>/binding
Origin: <PUBLIC_BASE_URL origin>
Cookie: <existing account session>
Authorization: AvibeInstallation <installation_id>.<current-or-pending-secret>
Content-Type: application/json
```

`Authorization` is omitted only while the installation row does not exist.

Request:

```json
{
  "next_installation_secret": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "revoke_registration_ids": [
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
  ]
}
```

The next secret is exactly 32 decoded bytes. The revocation array contains
unique, valid registration IDs captured before this request. It may be empty.

The relay authenticates the account cookie and exact Origin before allocating
anything. For a new installation it consumes the authenticated account
creation limit, creates the row, stores the next-secret verifier, and returns
`201`. A first creation requires an empty revocation array.

For an existing installation, the relay locks the row and validates the
current credential inside the transaction. If the authenticated account
differs from `account_id`, the supplied revocation set must equal all active
registration IDs currently owned by the installation. The transaction then
revokes exactly those IDs, changes `account_id`, and rotates the credential.
If the account is unchanged, it revokes only the supplied owned IDs and
rotates the credential; an unknown or differently owned supplied ID produces
the same mutation-free set conflict. Binding never changes provider state or
`provider_generation`.

If the set required for an account change differs, the relay makes no mutation
and returns `409 registration_set_changed` with the current concrete active
IDs. Native persists that new target set before retrying. A stale old-account
request cannot run after a newer rebind because the newer transaction changed
the credential; if the stale request wins the lock first, the newer coordinator
reconciles and retries from the resulting current credential.

If the request is retried with the pending secret after a committed response
was lost, the relay returns `200` when the requested account, credential, and
revocation postcondition already hold. It never rotates twice.

Success:

```json
{
  "ok": true,
  "installation_id": "00000000-0000-4000-8000-000000000000",
  "created": false,
  "credential_rotated": true,
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

| Status | `error.code` | Meaning |
| --- | --- | --- |
| `400` | `invalid_installation_id`, `invalid_installation_secret`, `invalid_registration_ids`, `invalid_json` | Invalid request. |
| `401` | `login_unauthenticated`, `installation_credential_required`, `installation_unauthorized` | Account or installation authentication failed. |
| `403` | `origin_forbidden` | Origin is absent or is not the exact configured origin. |
| `404` | `installation_not_found` | A credential-authenticated retry addressed an installation that still does not exist. |
| `409` | `registration_set_changed`, `credential_recovery_conflict` | The destructive target changed or the pending-secret replay postcondition is not true. |
| `413` | `request_too_large` | Management body exceeds 64 KiB. |
| `429` | `creation_rate_limited` | New-installation allocation exceeded the authenticated account bucket. |
| `502` | `auth_unavailable` | Existing account-session validation is unavailable. |

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
is non-empty base64url without padding and is bounded by the management body
limit.

The relay locks the installation:

- generation exactly one greater than current stores the complete tuple, clears
  `device_token_invalid_at`, and returns `200`;
- generation equal to current with an identical tuple returns idempotent
  `200` without clearing a later invalid marker;
- generation lower than current, more than one ahead, or equal with a
  different tuple returns
  `409 stale_provider_generation` without mutation.

Every new APNs registration callback allocates the next local generation before
sending, even when token bytes are unchanged. This is the only recovery that
clears a provider-invalid marker. Native never takes a generation from a `409`
and retries an obsolete desired tuple. If a newer local generation exists, it
discards the older operation's response. A callback that arrives while a PATCH
is in flight is queued and uses the next generation only after the in-flight
generation is resolved.

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

Other errors are `400 invalid_provider`, `401
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
  "vapid_key_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "replaces_registration_ids": [
    "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
  ]
}
```

Native generates and persists the new ID, P-256 key, 16-byte auth secret, VAPID
hash, and concrete replacement IDs before sending.

For a new ID, the relay verifies the credential, checks the authenticated
account creation limit before row allocation, locks the installation and VAPID
scope, and requires the supplied replacement set to equal that scope's current
active set. It revokes exactly those rows and inserts the new row in one
transaction. Every replacement ID must belong to the same installation and
VAPID hash; an unknown or differently owned ID is reported as
`replacement_set_changed` without mutation.

If the same registration ID already exists and is active with the same
installation and VAPID hash, a lost-response retry returns `200` without
processing a new replacement set. A reused ID with different ownership or
content returns `409 registration_id_reuse`; a row revoked after creation
returns `410 registration_revoked`.

Success is `201` for insertion and `200` for an idempotent replay:

```json
{
  "ok": true,
  "registration_id": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "endpoint": "https://avibe.bot/api/push/apns/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "vapid_key_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "created_at": "2026-11-09T00:00:00Z"
}
```

`endpoint` is generated from `PUBLIC_BASE_URL`; the literal host above is the
first deployment example.

| Status | `error.code` | Meaning |
| --- | --- | --- |
| `400` | `invalid_registration_id`, `invalid_vapid_key_hash`, `invalid_replacement_ids`, `invalid_json` | Invalid request. |
| `401` | `installation_credential_required`, `installation_unauthorized` | Invalid installation credential. |
| `404` | `installation_not_found` | Installation does not exist. |
| `409` | `replacement_set_changed`, `registration_id_reuse` | Current VAPID scope differs, or the ID was already used for different content. |
| `410` | `registration_revoked` | The idempotency ID exists but is terminally revoked. |
| `413` | `request_too_large` | Management body exceeds 64 KiB. |
| `429` | `creation_rate_limited` | Account creation bucket exhausted before insertion. |

`replacement_set_changed` includes `active_registration_ids`. Native persists
that observed set before a new attempt.

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

Logout bulk revoke and credential rotation:

```http
POST /api/push/apns/installations/<installation_id>/revoke
Authorization: AvibeInstallation <installation_id>.<current-or-pending-secret>
Content-Type: application/json
```

```json
{
  "next_installation_secret": "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
  "registration_ids": [
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  ]
}
```

The supplied set must equal the installation's current active registration
set. Under one lock, the relay revokes exactly those IDs and rotates the
credential. A mismatch returns `409 registration_set_changed` and the current
active IDs without mutation; native persists them before retrying. This closes
a race with a registration create that was already accepted by the server.

A pending-secret retry returns `200` when the credential is already current
and the supplied IDs are already revoked with no active registration left.
Success is:

```json
{
  "ok": true,
  "installation_id": "00000000-0000-4000-8000-000000000000",
  "revoked_registration_ids": [
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  ],
  "credential_rotated": true,
  "updated_at": "2026-11-09T00:00:00Z"
}
```

Both routes may return `401 installation_credential_required` or
`installation_unauthorized`, `404 installation_not_found`, `413
request_too_large`, or `409 registration_set_changed` for the bulk route.

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
| `400` | `invalid_vapid`, `vapid_key_mismatch`, `invalid_audience`, `vapid_expired`, `vapid_exp_too_far`, `invalid_content_encoding`, `invalid_ttl`, `invalid_urgency`, `invalid_topic`, `invalid_request` | Request rejected; registration stays active. |
| `410` | `registration_not_found`, `registration_revoked` | Missing or revoked endpoint; instance disables it. |
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

## 8. Tap routing

On tap, native compares the notification's account and instance origin with
the current account and its configured instance list.

For a current account and available instance it selects the instance, waits for
the primary WebView to load, normalizes `url` as a same-origin path, and emits
`push.opened({url})`. The Web UI accepts only a path, query, and fragment for
the selected origin. Absolute URLs, another scheme or origin, and control
characters are rejected.

For an account mismatch or unavailable instance, the app opens the instance
list and shows the existing recovery message. It never sends the URL to another
instance or Safari. A fallback notification that has no decrypted account,
origin, or URL also takes this recovery path.

## 9. End-to-end lifecycle

### 9.1 Field responsibility

| Field | Produced by | Consumed by | Signed or authenticated coverage | Supplied by |
| --- | --- | --- | --- | --- |
| `installation_id` | Native CSPRNG/UUIDv4 | Relay management; native coordinator | HTTPS plus installation credential after creation | ThisDeviceOnly Keychain |
| `installation_secret` | Native CSPRNG | Relay credential verifier | HTTPS Authorization; relay stores verifier only | Native Keychain |
| `account_id` | Relay from account session | Installation and registration ownership; native local account state | Same-origin cookie plus exact Origin on bind | Existing account authority |
| `provider_generation` | Native provider coordinator | Relay PATCH and APNs invalidation CAS | Installation-credential request; relay monotonic comparison | Persisted native provider state |
| `device_token` | APNs callback | Relay APNs request builder | Installation-credential PATCH; protected at rest | APNs |
| `environment`, `topic` | Build/bundle configuration | Relay host and `apns-topic` | Installation-credential PATCH plus deployment allowlists | Native target |
| `registration_id` | Native CSPRNG | Relay path, endpoint, APNs `kid`, NSE lookup | Installation-credential create request | Pending/shared Keychain record |
| `vapid_key_sha256` | Native from instance VAPID public key | Relay registration and delivery verifier | Installation-credential request; checked against VAPID `k` | Instance key through native bridge |
| `p256dh`, `auth` | Native | Instance encryption and NSE decryption | HTTPS subscription POST; RFC 8291 inputs | Native public subscription |
| Notification fields and `badge_count` | Instance | NSE after decryption | RFC 8291 AES-GCM | Existing instance delivery path |
| VAPID JWT | Instance | Relay | RFC 8292 ES256 over JWT header and claims | Instance VAPID private key |
| `avibe.ct` | Relay base64url encoder | NSE | Contains unchanged RFC 8291 authenticated body | Relay from request bytes |
| Tap `url`, instance origin | Instance payload plus native key record | Native and Web UI | AES-GCM for URL; native account/origin checks | NSE `userInfo` |

### 9.2 Enable

1. After account login, `MobileAccountCoordinator` uses a short-lived hidden
   transport backed by the account's persistent website data store to bind or
   create the installation. Session expiry is shown as a recoverable failure.
2. Native completes any uncertain credential rotation before another binding.
3. On notification enable, Web calls `push.subscribe({vapidPublicKey})`.
4. Native obtains permission and an APNs token. It persists and PATCHes the
   next provider generation if needed.
5. Native creates and persists a pending P-256 key, auth secret, registration
   ID, VAPID hash, and concrete replacement set.
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
2. The relay validates the bound VAPID sender and forwards only ciphertext.
3. APNs invokes the NSE, which decrypts and presents or keeps the fallback.
4. APNs invalidation returns relay `410` only for the captured current provider;
   the instance follows its existing terminal disable path.
5. A later APNs callback persists and PATCHes a higher provider generation.
6. After successful provider recovery, native calls `push.status()` for each
   local active instance subscription and the Web UI re-POSTs that existing
   public subscription to the instance. It does not call `push.subscribe`,
   create a registration, or rotate the key merely to re-enable the instance
   row.

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

### 9.6 Logout and account switch

Before local account teardown, native freezes new push mutations, captures all
active registration IDs, persists the credential-rotation revoke operation,
increments the shared account generation, deletes old account key records, and
best-effort removes pending and delivered notifications whose `kid` is in the
captured set.

If execution and authorization are available, native sends the bulk revoke. If
not, logout continues locally and the exact IDs remain pending. A later
foreground opportunity uses the installation credential; it does not retain
or recreate the old account cookie.

For a new account, the account-authenticated bind uses the current credential,
the new account cookie, a next credential, and the captured concrete IDs. The
relay changes ownership, revokes the old set, and rotates authority atomically.
A set conflict is persisted and retried before the new account can subscribe.
Old requests authenticated with the prior credential then fail, and old
concrete revoke IDs cannot match fresh registrations.

The accepted Phase 1 limitation is explicit:

- connectivity alone does not schedule iOS execution;
- until a remote revoke succeeds, the old endpoint may continue receiving
  generic and possibly audible fallback alerts without a fixed deadline;
- revocation cannot recall an APNs request already accepted or an alert already
  delivered;
- deleting a key does not erase plaintext already held by an in-flight NSE;
- local generation checks and notification-center cleanup are best effort, not
  retroactive suppression.

After a confirmed logout revoke, a still-signed-out app may delete the now
unused installation credential. A later login then creates a new installation
ID. If a new login arrives while recovery is pending, it completes the
credential recovery and uses the atomic rebind path instead.

### 9.7 Token rotation, reinstall, and uninstall

Every APNs registration callback writes a higher provider generation and full
tuple before PATCH. Process restart resumes the same pending generation. An
older PATCH response or APNs invalidation cannot overwrite it. Provider
replacement changes only the installation row; it never creates, revokes, or
reactivates a registration.

On reinstall detection, native treats app-container state as new even if
Keychain items survived uninstall. It captures any surviving old
installation credential and concrete registration IDs into a minimal orphaned
revoke record, deletes old decryption keys, and creates a fresh installation
ID for the new install. It may retry the orphaned concrete revokes later, but
does not reuse old registration IDs or keys.

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
| Cross-account mutation | Account ownership changes require the exact account Origin/session plus the current installation credential; rebind, old-ID revocation, ownership, and credential rotation are one transaction. |
| Delayed destructive request | Every revoke carries concrete IDs captured before send. A changed set causes a readback conflict, not server-selected destruction. Fresh registration IDs prevent an old target from naming a new row. |
| Stale provider write or invalidation | Native persists one monotonic generation; PATCH replaces the full tuple; APNs invalidation compares generation, token hash, environment, and topic. |
| Installation credential leakage | The credential is deliberately usable after cookie logout but is restricted to one installation's push management, stored ThisDeviceOnly, hashed at the relay, rotated on bind/logout, excluded from Web/NSE/logs, and bounded by creation throttling. It cannot read account or notification data. |
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

2. **Concrete lifecycle and recovery.** Tests drop successful responses for
   installation credential rotation and registration creation, restart native,
   and prove pending-secret and same-registration-ID recovery. A delayed
   logout/account request carries old concrete IDs and cannot revoke a fresh
   registration after rebind. If current IDs changed before an account switch,
   the first request is mutation-free `409`, native persists the readback, and
   the retry converges.

3. **Newest provider wins.** Provider generation and pending tuple survive a
   process restart and account rebind. Once generation `N+1` is accepted, a
   generation `N` PATCH cannot overwrite it, and an APNs invalid result
   captured for `N` or for a different token/environment/topic returns
   retryable `503` without marking `N+1` invalid.

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

The APNs names are backend-owner supplied. `APNS_TOPIC` and
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
- richer cleanup for orphaned reinstall records.

Phase 1 deliberately has no epoch, durable bind-response replay table,
five-installation cap, seven-day sweep, or ciphertext-dedup table.

## 13. Release gates

Implementation is ready for integration only when:

- the three acceptance properties pass across the owned boundaries;
- all management routes enforce the mutation ledger and lost-response
  recovery;
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
