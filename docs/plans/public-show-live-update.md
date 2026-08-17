# Unified Show Access and Capability-Gated HMR

Status: contract freeze for first release

Contract version: 1

Machine-readable source: `docs/plans/show-access-contracts/`

## Outcome

Avibe owns Show Page audiences locally. An owner chooses exactly one mode:
private, limited to listed email addresses, or public. The Backend proves a
visitor's identity but never stores the list or decides whether that identity may
read a page. Runtime receives a trusted private/shared context after Avibe has made
the route decision.

Consider one limited page with `alice@example.com` in its local list. Alice opens
`/p/stable_alpha/`, signs in through the paired Backend, and returns with an
identity-only assertion. Avibe re-resolves `stable_alpha`, reads current local
ShowAccess, finds Alice in the list, and admits one shared document. If the owner
then removes Alice, her already loaded document keeps working until it naturally
ends. A new navigation or manual refresh is denied. Alice never gains `/show`,
HMR, annotations, Workbench, local API, or resource authority.

## Three Boundaries

### 1. Local ShowAccess and Apply

The authoritative local aggregate is:

- `page_id`
- `access_mode`: `private | limited | public`
- stable nullable `share_id`
- monotonic `revision`
- sorted, deduplicated, normalized exact-email set

The controller's page-scoped stable writer is the only write owner. It applies
mode, binding, and canonical email-set changes in one local transaction. The UI
calls authenticated local HTTP; the UI process forwards to the controller over
the authenticated internal socket. Route, request, result, and returned
`ShowAccess.page_id` must identify the same page.

`Apply` uses `expected_revision` as its compare-and-swap input. An effective
canonical change advances `revision` exactly once. A canonical no-op returns
`no_change` without advancing. A stale revision returns `conflict`; a binding
collision returns `share_id_taken`; neither writes. There is no mutation ID or
receipt. After a lost response, read current state and decide whether another
Apply is necessary.

Email normalization is ASCII surrounding-whitespace trim, ASCII lowercase,
syntax validation, deduplication, and lexical sort. Syntax means exactly one `@`,
a dot-atom local part with no leading, trailing, or consecutive dot, and lowercase
DNS-style domain labels with no leading or trailing hyphen. The canonical set is
persisted only in Avibe. Settings reads require owner or existing sharing-control authority,
are delivered with `Cache-Control: private, no-store`, and never leave local Avibe.
Their explicit request is `{page_id}` and their result is `{show_access}`. The
authorized route, controller request, and returned `show_access.page_id` must be
equal; a controller mismatch returns no email data.

Mode behavior:

| Mode | Shared binding | New `/p` admission |
| --- | --- | --- |
| private | Existing binding may remain inactive | Deny |
| limited | Required and stable | Verified identity plus current local membership |
| public | Required and stable | Allow anonymous |

Switching between limited and public preserves the binding. Only an explicit
rotation or custom replacement changes it. Enterprise per-page ACL administration
is a non-goal; future organization policy may only narrow local settings.

### 2. Identity Assertion and Login

Avibe starts a browser navigation to:

`GET /api/v1/instances/{instanceId}/show-identity/authorize`

The only request fields are `state`, `nonce`, and `redirect_uri`. The Backend
returns a compact RS256 JWT/JWS by `form_post` to the one configured paired HTTPS
origin at `/auth/show-identity/callback`. No assertion appears in a URL, fragment,
history entry, or referrer.

The exact identity claims are `iss`, `aud`, `sub`, `iat`, `exp`, `jti`, `nonce`,
`instance_id`, and normalized `verified_email`. The paired record supplies issuer,
audience, instance, and same-origin JWKS authority. The Backend obtains the email
from a fresh verified identity record. Browser input cannot supply identity,
membership, role, page, or share claims.

Assertion, signed state, and pending-flow-cookie lifetimes are fixed at 300 seconds
with no post-expiry grace. One login flow is current for a browser and configured
callback origin. Starting a newer flow replaces the prior flow; a stale, cookie-less,
or expired callback returns a fixed retry result. This intentionally does not
promise simultaneous flow success. The opaque pending-flow cookie uses Path
`/auth/show-identity`, so both local login start and callback receive it. Login
start signs the cookie value's SHA-256 digest into the state and stores no pending
flow. Callback receives the HTTP cookie name/value pair, validates its digest,
and expires the matching cookie before creating the identity session. Repeated
cookie-less starts therefore allocate no durable or in-memory pending records;
separate browser cookie jars remain independent. Version 1 adds no server replay
ledger; ordinary replay fails because the matching browser cookie was expired.

A known signing key is cached for at most 300 seconds. Once older, it is fetched
once from the paired issuer before assertion validation; fetch failure returns
`identity_retry_required`. An unknown signing key likewise permits at most one
issuer-coalesced forced JWKS refresh for that login attempt. Refresh is scoped by
the paired issuer, never by an attacker-selected `kid`. Version 1 defines no
background refresh, seamless key rotation, or previous-key overlap.

The resulting local session uses one host-only Secure, HttpOnly, SameSite=Lax
`__Host-avibe_show_identity_session` cookie at Path `/`. Its value is 32 random
bytes; Avibe stores only the SHA-256 digest, paired instance, configured callback
origin, subject, normalized verified email, creation time, and expiration time.
The lifetime is a fixed 30 days with no sliding refresh, session family, or
cross-session revocation. A successful callback overwrites the browser cookie;
older bearer records may expire naturally. Logout deletes only the presented
record. Every new limited `/p` navigation or manual refresh still validates the
identity session, re-resolves the local page, and checks current membership once.
The session never contains page, share, membership, or role claims and never
creates `InstanceAccessContext`, an Instance role, or access to `/show`,
Workbench, resource APIs, HMR, annotations, or Agents.

### 3. Runtime Private/Shared Containment

Avibe strips browser-supplied Runtime protocol/context headers and constructs one
server-owned protocol-1 envelope:

- `X-Avibe-Show-Protocol: 1`
- `X-Avibe-Show-Context: private | shared`

Shared admission requires Runtime feature `show-context-key-v1`. Unsupported or
transiently unknown capability returns one sanitized shared-runtime-unavailable
result after the bounded probe policy. It never falls back to a legacy singleton
graph. A trusted resource viewer/editor redirect from top-level `/p` to canonical
`/show` does not admit shared work and is independent of shared keyed support.

Private and shared graphs have independent context keys and lifecycles. A shared
graph is built only after a successful new-navigation or manual-refresh admission.
Ordinary private editor edits keep the private graph identity and never create,
build, or rebase a shared graph. Shared context has no HMR.

For `/p`, Avibe authorizes only the new top-level navigation or manual refresh,
then requests an opaque shared document capability. The internal admission carries
`source_session_id`, and the shared graph key binds it with page, share, admitted
revision, and context. The source Session ID never enters browser-visible data.
The capability is bound to instance, page, share, admitted revision, namespace,
and document. Capture returns either that capability or one fixed sanitized
`shared_runtime_unavailable`, `snapshot_too_large`, `capacity_exhausted`,
`capture_timeout`, or `reload_required` result. User code receives no session ID,
workspace/source path, identity material, HMR, or annotation bootstrap. A limited
admission always carries a nonempty verified identity subject; public admission may
carry no subject. Namespace and document identifiers use only the URL-safe
`A-Za-z0-9_-` alphabet.

The trusted `/p/<share_id>/` shell places arbitrary page code in an iframe with
exact sandbox token `allow-scripts`, without `allow-same-origin`. Its CSP includes
`worker-src 'none'`. Worker, SharedWorker, and Service Worker construction returns
the fixed `shared_worker_unsupported` result. No Service-Worker-Allowed header is
emitted. Shared code cannot reach the shell's cookies, storage, DOM, local APIs,
HMR, annotations, or another share's capability.

Shared document, module, style, raw-asset, fallback, and page-API responses use
one browser-compatible path prefix:
`/__avibe_show_shared/v1/{namespace_id}/{document_id}/{capability}/`. The root is
the document, opaque assets use `asset/<handle>`, relative page APIs use
`api/<safe-path>`, and a nested `/p` reload uses `history/<safe-path>`. API and
history paths accept practical segments containing ASCII letters, digits,
underscore, hyphen, dot, and at-sign while rejecting empty or exact dot segments,
encoded separators/traversal, queries, and raw source paths. The transformed
fallback document removes page-authored base elements and establishes the exact
capability root as its base URL, so relative APIs, modules, styles, and raw assets
resolve identically on root and nested history documents. Nested imports are also
rewritten to absolute URLs under that prefix.

Page-API requests form one closed union carrying method, protected path, optional
normalized content type, and an optional base64 body whose decoded length is at
most 1 MiB and must equal its declared length. Oversize requests receive the fixed
sanitized `413 request too large` result. Cookies, Authorization, and arbitrary
ambient headers are not part of this wire. The opaque resource handle is never a
source path; query strings and ambient identity confer no authority. Capability-
bearing path segments are redacted from access logs. Responses use credentialless
CORS, never set cookies, and use `Referrer-Policy: no-referrer`.

Admission is intentionally not continuous authorization. The document capability
continues while its Runtime namespace and document handle exist. Membership, mode,
binding, revision, and elapsed time do not expire it, trigger refresh, poll
authority, or close the loaded page. A namespace may be lost only on Runtime
restart, explicit operational shutdown, or genuine process resource pressure under
the fixed global budget; that returns a fixed reload-required result.

`/show` always uses private context. A resource editor receives HMR and annotations
only after connection-admission authorization. A resource viewer may use canonical
`/show` without editor capabilities. Listed-only guests stay on `/p`. Existing
admitted editor connections may live until natural disconnect; later connections
authorize again. Operational shutdown and Runtime restart are separate lifecycle
events, not permission-revocation protocols.

Shared Runtime version 1 uses a 60-second per-request execution timeout, a 64 MiB
per-snapshot limit, and one process-wide shared budget of 64 namespaces and 512
MiB. The request timeout bounds one handler; it does not expire the document.
It defines no worker broker, background shared rebuild from private edits,
permission monitor, or formal pin/reclaim race protocol.

## Cache Boundary

All limited responses are `private, no-store`. Every non-versioned public shell,
document, module, style, raw-asset, fallback, page-API, error, and redirect surface
is also non-cacheable. This includes every access-dependent resource-viewer or
resource-editor redirect from `/p` to canonical `/show`. A separately named,
content-addressed versioned asset may use
`public, max-age=31536000, immutable` only when it contains public bytes and no
page-private or identity data.

## Direct Retirement

There is no production exact-email data. No data bridge, backfill, import, or
compatibility period is required. Future Backend and Avibe implementation lanes
delete the obsolete hosted email table/endpoints, proxy clients, and
`show_page_email` Instance authorization source directly. This contract contains
no migration, cleanup, hosted grant, prepare/commit, or reconciliation model.

## Delivery Sequence

1. Freeze and validate these three version-1 contracts in Avibe.
2. Implement local ShowAccess storage, stable-writer Apply, settings projection,
   and new-navigation admission in Avibe.
3. Implement the identity-only authorize/assertion endpoint and remove the unused
   hosted email model in avibe-backend.
4. Implement shared opaque-document containment and keyed-context behavior in
   vibe-show-runtime.
5. Bundle one reviewed and smoke-tested Runtime SHA, then advertise
   `show-context-key-v1` only when reviewed, smoke-tested, and bundled SHAs match.
6. Run Avibe/Backend/Runtime integration, real-browser security, and local Incus
   acceptance before release.

## Contract Verification

This PR proves only contract conformance:

- all three Draft 2020-12 schemas and embedded examples parse and validate;
- interface fields and closed vocabularies match across the three boundaries;
- existing Avibe Runtime protocol constants equal the frozen values;
- local membership never becomes Backend or Instance authorization;
- `AUTH-SETUP-401` executes limited entry, identity `form_post`, local session
  creation, later session reuse, and next-navigation membership removal;
- admitted shared documents use admission-time, not continuous, authorization;
- admitted capabilities and namespaces never expire because of time or permission changes;
- shared code has no privileged surface and all worker kinds are unsupported;
- `/p` is shared/no-HMR and `/show` editor capability is independently admitted.

Production Avibe, Backend, Runtime, browser, and Incus behavior remains future
consumer evidence. The contract tests must not be reported as that evidence.

## Acceptance Gate

- One local authority and one stable writer own the complete ShowAccess aggregate.
- Apply is revision-CAS, atomic, and contains no hosted or durable-receipt protocol.
- Backend assertions prove identity only and contain no page authorization.
- New limited navigation checks current local membership; a loaded document is not
  continuously reauthorized or actively revoked.
- `/p` is opaque-origin shared content with no HMR, annotations, Workers, local
  authority, session IDs, or source paths.
- `/show` HMR and annotations require resource-editor admission independently of
  shared-page membership.
- Keyed-context failure never touches a legacy shared graph.
- Limited bytes are not cacheable; public immutable assets contain public bytes only.
- Runtime capability advertisement is pinned to one reviewed, smoke-tested, bundled SHA.
