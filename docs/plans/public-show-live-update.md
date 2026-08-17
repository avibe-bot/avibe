# Unified Show Access and Capability-Gated HMR

Status: Proposed

Date: 2026-08-17

Authority: Issue #1498, "Show Page access: keep limited-viewer lists local and
separate from Instance access"

Scope: contracts for Avibe Show access, Avibe Backend identity-only login and
direct retirement, and `vibe-show-runtime` context isolation. This plan does not
implement UI, storage, authorization, routing, Backend endpoints, migrations, or
Runtime behavior.

## Decision

A Show Page has one locally owned audience setting and a separate resource
capability. The audience controls only the shared `/p/<share_id>/` representation.
Existing Instance and resource authorization independently controls canonical
`/show/<page_id>/`, Workbench, APIs, Agents, HMR, and annotations.

For example, Alice owns a page, Bob is listed locally as `bob@example.com`, and
Carol has independent resource-viewer authority:

1. Bob opens the limited `/p/` route. Avibe Backend proves only his verified,
   instance-bound identity. Local Avibe normalizes his email and checks its current
   local membership set. Bob receives the shared read-only page and no other
   capability.
2. Carol opens the same `/p/` route. Her independent resource authority redirects
   her to canonical `/show/`, where she may read but cannot use HMR or annotations.
3. Alice opens the route and redirects to `/show/`. Her resource-editor authority
   grants `ShowEditorCapability`, HMR, and annotations on that canonical surface.
4. Removing Bob locally denies his next request even if Backend is unavailable.
   Avibe does not close content already loaded in Bob's tab and creates no guest
   heartbeat or HMR-like channel.

The Backend may authenticate a person. It never receives a Show Page whitelist,
decides page membership, or derives an Instance role from page membership.

## Product Contract

### One audience setting

The settings surface exposes one mutually exclusive selector:

| `access_mode` | Label | `/p/<share_id>/` | Conditional controls |
| --- | --- | --- | --- |
| `private` | Private | Disabled, with any stable binding retained | No email editor; retained link may be rotated while disabled |
| `limited` | Limited access | Verified identity plus current local exact-email membership | Exact-email editor and share-link controls |
| `public` | Fully public | Anonymous shared read | Share-link controls |

`offline` is an orthogonal operational state, not a fourth audience. An offline
page serves neither `/p/` nor `/show/` while retaining its configured mode, stable
binding, and limited membership for future activation. Audience Apply never changes
availability.

Limited and public modes share one stable slug. Switching between them preserves
the binding. Switching to private disables the route but retains the binding.
Reopening limited or public reuses it. Only explicit rotation or a custom-binding
operation replaces it. A page that has never been shared allocates a binding when it
first enters limited/public or when the owner explicitly rotates/customizes it.

The separate public-link switch is removed. Limited requires at least one exact
email. Private and public store an empty membership set. UI validation and the
machine contract use the same normalization: trim surrounding whitespace, lowercase,
deduplicate, sort, and compare exactly. Provider-specific dot, plus-alias, or Unicode
rewriting is forbidden.

### Local authority and storage

Local Avibe is the sole authority and persistence location for:

- `availability: active | offline`;
- `access_mode: private | limited | public`;
- the stable `share_binding`;
- the monotonic device-local `audience_revision`;
- the normalized exact-email membership set.

The machine contract models one aggregate `ShowAccess`, while the implementation
uses one parent page-access row, a dedicated child table keyed by
`(page_id, normalized_email)`, and a durable Apply-receipt table keyed by
`(page_id, mutation_id)`. The controller process owns the only write service. One
database transaction replaces membership, writes mode, binding and revision, and
records the canonical request digest plus terminal result. No UI-process
coordinator or direct store writer is allowed.

The durable ownership rules are:

- one stable cross-process writer lease is acquired before the authoritative read;
- `expected_audience_revision` is the only revision compare-and-swap input;
- a canonical change to mode, resulting binding, or normalized membership advances
  `audience_revision` exactly once;
- a canonical no-op keeps the revision;
- any retained same `mutation_id` and canonical payload returns its stored terminal
  result before CAS evaluation, including after later Apply operations;
- any retained same `mutation_id` with a different canonical payload rejects before
  CAS evaluation or a write;
- `canonical_request_sha256` hashes the complete normalized Apply request body
  (`schema_version`, `message_type`, `page_id`, `mutation_id`,
  `expected_audience_revision`, and `target`) using recursive RFC 8785 canonical
  JSON, UTF-8 bytes, SHA-256, and lowercase hex; route authority and actor context
  are outside the digest;
- page-scoped receipts remain for the page lifetime, have no time/count eviction,
  and cascade-delete with the page;
- a stale expected revision rejects before a write;
- availability is copied from the authoritative source and cannot be supplied by
  Apply;
- route page ID, request page ID, result page ID, and result aggregate page ID are
  equal;
- canonical emails are rejected before digest, comparison, persistence, or result
  serialization unless they are lowercase, unique, and lexicographically sorted;
- a new or replacement share binding is checked for global uniqueness and written
  inside the same stable-writer transaction; a binding owned by another page returns
  `share_id_taken` without a write, including under concurrent custom-slug Apply.

Apply is fully local. It has no hosted prepare, commit, current-grant, operation,
acknowledgement, cleanup, reconciliation, commitment, or cloud-availability phase.
A process crash can occur before or after the transaction, never inside a partly
authoritative audience. Retrying the same mutation determines which terminal state
committed without introducing a second coordinator.

Only the owner or existing sharing-control resource authority may read exact-email
settings or invoke Apply. Page membership alone and ordinary resource read authority
are insufficient. Authorization failure occurs before controller IPC, store access,
or any identity-provider call. Authorized route, HTTP request, controller IPC request
and result, and projected aggregate all carry the same page ID. Request mismatch
prevents store access; result mismatch is an internal protocol failure and returns no
settings. `Cache-Control: private, no-store` and `Vary: Cookie` are HTTP response
metadata, not JSON fields. The body is a user-facing projection of the local aggregate,
including exact emails but excluding `last_mutation`, request digests, and Apply
receipts, only to an authorized caller.

### Orthogonal authorization

Page guest membership and resource authority are independent axes:

- page guest membership is a current local `(page_id, normalized_email)` row and
  can authorize only a limited shared `/p/` request;
- resource viewer/editor authority comes from the existing Instance/resource model
  and governs canonical `/show/`, Workbench, resource APIs, and editor capability;
- only resource editor authority creates `ShowEditorCapability`;
- page membership never creates an `InstanceAccessContext`, an Instance viewer, or
  an `InstanceAccessSource`;
- `show_page_email` is not a valid Instance access source or OIDC authorization
  claim.

A person may have either axis or both. A listed-only person stays on `/p/`. A
resource viewer or editor making a trusted top-level `/p/` navigation redirects to
canonical `/show/` regardless of local membership. The viewer remains read-only;
the editor receives HMR and annotations only after `/show/` independently validates
`ShowEditorCapability`.

The closed route outcomes are:

- public `/p/`: anonymous shared read;
- limited `/p/`: verified identity plus current local membership;
- private or offline `/p/`: deny;
- active `/show/`: existing resource viewer reads private modules, resource editor
  additionally receives HMR and annotations, everyone else denies;
- offline `/show/`: deny;
- every served `/p/`: shared Runtime, no HMR, no annotations, no private context,
  no Session internals.

Resource authority does not make limited membership true. A resource-authorized
top-level `/p/` request takes the canonical redirect; a direct non-navigation `/p/`
request still follows the current page audience gate.

### Identity-only limited login

Limited login starts only after Avibe resolves the current share binding. Local
Avibe owns signed state, the safe same-share return target, nonce and single-use
handling, callback correlation, and final share re-resolution.

Each login flow has a distinct host-only correlation cookie named
`__Secure-avibe_show_identity_c_<base64url_nonce>`. Its independent value is a
32-byte CSPRNG secret; it is `SameSite=None`, `Secure`, `HttpOnly`, scoped exactly
to `/auth/show-identity/callback`, single-use, and expires no later than signed
state. One server-owned callback origin is represented as `(scheme, normalized_host,
effective_port)`; HTTPS without an explicit port means 443. The Backend authorize
request, `redirect_uri`, signed state, actual callback, identity-session record, and
safe return flow must agree on that exact origin and the fixed callback path. Signed
state binds the page, its hash, callback origin, nonce, instance, share, safe return,
and expiry. Browser values never create callback authority. Avibe rejects scheme,
host, effective-port, path, query, or fragment mismatch before assertion, cookie,
session-store, or page use. Avibe verifies state before selecting a cookie;
invalid state deletes none, and a terminal callback consumes only its flow. Nonce
and `jti` consumption is atomic and retained through the later state/assertion
expiry plus verifier skew. Concurrent same-host flows may finish in either order,
while swapped state/assertion/cookie inputs and replay have exactly one winner.
This permits the Backend's cross-site `form_post` to an active custom hostname
without placing assertion material in a URL. Real browser cookie delivery remains
browser conformance, not something an HTTP reference harness can prove from a
handwritten Origin header.

Avibe Backend returns one short-lived RFC 7519 compact JWT/JWS identity assertion.
It has exactly three base64url segments and uses RS256 only. The protected header
contains exactly `alg=RS256`, `typ=JWT`, and one nonempty `kid`; `none`, other
algorithms, token-directed key URLs/embedded keys, unsupported critical headers,
and unencoded payload behavior fail closed. Authorization-code exchange is not part
of this contract. Header `alg`, `typ`, and `kid` plus payload `iss`, single-string
`aud`, `sub`, `jti`, `nonce`, `instance_id`, and `verified_email` are bounded nonempty
strings. `iat` and `exp` are JSON integers, booleans are rejected, and they must
satisfy `iat <= exp` plus the maximum-lifetime bound. Duplicate,
null, wrong-type, empty, extra, or missing members fail with one sanitized result
before any authority value becomes a key, URL, cookie name, or store input.
The browser calls
`GET /api/v1/instances/{instanceId}/show-identity/authorize` with exactly signed
`state`, `nonce`, and an HTTPS `redirect_uri` on the active instance/custom hostname
at `/auth/show-identity/callback`. The Backend delivers the assertion only by
`form_post` to that fixed callback. No assertion appears in a URL, query, fragment,
history entry, or referrer.
The signed assertion contains only:

- issuer, audience, subject, issue/expiry times, nonce, and unique token ID;
- the paired `instance_id`;
- one verified normalized email.

Issuer, OAuth client ID/audience, instance ID, and JWKS URI come only from the local
pairing record. Its audience is the single string
`avibe-show-identity:<oauthClientId>`. The trusted issuer is HTTPS and the only JWKS
URI is the exact same-origin `<issuer>/oauth/jwks.json`; discovery, when checked,
must agree, and redirects or token-selected key sources cannot cross origin. One RSA
signature key must match `kid`, `kty=RSA`, `use=sig`, and `alg=RS256`, with a modulus
of at least 2048 bits. Duplicate `kid` or changed key material under an existing
`kid` fails closed. An unknown `kid` causes one issuer-coalesced forced refresh and
one verification retry, then fails closed.
The paired-issuer JWKS cache lives for at most 300 seconds and must revalidate even a
known key at expiry. Refreshes are issuer-coalesced and atomically replace the key set;
removed or stale keys are not accepted, and refresh failure fails closed.

Normal lifetime is 300 seconds, hard maximum is 600 seconds, and verifier skew is
60 seconds. The email is ASCII-trimmed and lowercased from a fresh verified Backend
identity lookup. Only the current key signs; JWKS keeps current plus previous public
keys for at least `maximum lifetime + 2 * verifier skew = 720` seconds after the last
possible old-key signing. This derived window covers future-`iat` and expiry skew;
old and new assertions remain verifiable throughout it. `identity_not_verified` and `identity_unavailable`
are no-store, assertion-free terminal errors. Backend issues unique `jti`; local
Avibe consumes the nonce and `jti` once.

It contains no `page_id`, `share_id`, membership result, page authorization,
Instance role, `InstanceAccessSource`, audience revision, grant revision, or
whitelist data. The browser cannot supply or override the instance binding or
verified email.

The local callback verifies signature, issuer, audience, expiry, nonce, single use,
and instance binding, then returns only to the signed safe `/p/` target. Signed state
uses integer NumericDate fields, is non-renewable, permits 60 seconds of verifier skew,
and cannot exceed 600 seconds. Success rotates an opaque local server-side identity
session only after every callback check succeeds. The host-only `__Host-` cookie is
Secure, HttpOnly, SameSite=None, Path=/, stores no identity, and maps by token hash to
an instance/issuer/subject/email/exact-origin record for at most 24 non-renewable
hours. Each browser lineage has one current generation. A valid prior cookie advances
the lineage atomically and invalidates every earlier record. At login start, a
server-side nonce record captures the prior token hash, lineage, and generation only
when that session is current, and expires with signed state; the callback cookie hash
must match it. Concurrent flows that captured the same valid generation advance that
one lineage in callback order and leave at most one valid generation, while a stale
response token fails closed and restarts login. A flow started with no valid prior
session creates a new lineage. Invalid and terminal callbacks never rotate. Expiry, origin or
instance mismatch, missing
record, and local key/session-store reset fail closed. The next limited request
re-resolves the share and performs a fresh local membership lookup. The identity
session is evidence of identity, never cached page authorization. An unlisted identity
receives one generic denial with no page bytes and no login loop.
Removal from the list takes effect on the next request without Backend access; an
already loaded guest tab is not actively closed.

### Shared viewer browser containment

The visible URL remains `/p/<share_id>/`. Avibe serves a trusted platform shell at
that URL and runs arbitrary shared page code only inside a sandboxed opaque-origin
iframe with `allow-scripts` and without `allow-same-origin`. Only the trusted shell
may frame the shared document. Shared page code has no DOM, cookie, CORS, Service
Worker, opener, or ambient-identity path to another share's bytes.

Avibe applies one admission rule before capture: active public audience admits
anonymous readers; active limited audience admits only verified identities with
current local membership; every other state denies. Avibe then authorizes an
internal Runtime capture request. Runtime returns only an opaque namespace handle,
document handle, and expiry.
Avibe then issues an entropy-backed browsing capability; browser routes accept only
the opaque handles and capability, never a Session ID, workspace/source path, or
browser-supplied Runtime context. The browsing capability is bound to
`(instance_id, page_id, share_id, audience_revision,
namespace_handle, document_handle)`. The trusted shell places it only as a high-entropy
opaque protected-route namespace segment; headers, query, fragment, cookies, and
referrers cannot transport it. Rewritten document, module, CSS, raw, worker, fallback,
and API URLs stay under that namespace and use `credentials: omit` plus
`Referrer-Policy: no-referrer`. Those responses are credentialless CORS
(`Access-Control-Allow-Origin: *`, never credentials or `Set-Cookie`). API preflight
allows exactly GET, HEAD, OPTIONS, POST, PUT, PATCH, DELETE and `Content-Type`; the
capability is validated from the path and invalid requests receive one sanitized
not-found result. Vendor assets stay namespace-scoped and capability-required. The
capability is neither the Backend identity assertion nor a browser cookie, Instance
role, resource authority, or editor capability. A binding or revision mismatch
re-resolves the share and current membership before any new credential or page bytes.
Consequently, code on a sibling public page cannot fetch, frame, open and read, or
reuse ambient credentials for a limited page.

Minting is not a lasting authorization decision. Every protected document, module,
CSS, raw, worker, fallback, API, preflight, and write request first linearizes one
current local `ShowAccess` snapshot and requires active availability, a shared mode,
the exact binding and `audience_revision`, an unexpired capability, and current
membership for the server-recorded limited identity. Only then does Runtime
atomically pin the live namespace/document handle; Runtime never decides audience.
The same validation covers GET, HEAD, OPTIONS, POST, PUT, PATCH, and DELETE.

`audience_revision` is also the single request-admission revision. Every durable
availability transition advances it once even though Apply cannot mutate
availability; effective mode, binding, or email-set changes retain their existing
single increment, and no-ops do not advance it. Thus offline-to-active cannot revive
an old capability. Active/offline replay, shared/private changes, public/limited
changes, binding/revision changes, member remove/re-add, capability expiry,
namespace expiry, and budget reclaim each have one closed later-request outcome.
An in-flight request pinned before a transition may finish only within the Runtime
hard request deadline; every later request revalidates. Already loaded guest
DOM/JavaScript is never actively closed, and
Instance/resource ACL revision remains orthogonal.

### Direct retirement of the unused hosted model

The local SQLite schema migration is intentionally separate from hosted retirement.
Existing active private/public values map to `availability=active` with the matching
mode. Legacy offline cannot recover its historical audience mode, so it maps fail
closed to `availability=offline` and `access_mode=private`. Any existing `share_id`
is retained as an inactive stable binding; a null legacy `share_id` remains a null
binding. Emails initialize empty, and
`audience_revision` initializes deterministically to zero. This local migration has
no hosted import, bridge, or double write.

The owner confirmed there is no production exact-email data. There is no migration,
snapshot import, compatibility bridge, dual-write phase, legacy writer window, or
data-preservation requirement.

Future implementation lanes directly delete:

- Backend `remote_access_show_page_email_grants` storage and all hosted authorized-
  email CRUD or grant-operation endpoints;
- Avibe proxy clients for hosted exact-email reads and writes;
- `show_page_email` from `InstanceAccessSource`, OIDC authorization context, and
  signed Instance claims;
- hosted grant revisions, commitments, prepare/commit/status/acknowledgement,
  cleanup/reconciliation, and rollout compatibility code.

After retirement, any obsolete authorized-emails route is absent or returns a
terminal removed response. It cannot reopen authority. Development and regression
rows may be discarded. The retirement contract proves absence; it does not model a
migration state machine.

Backend logical deletion and destructive DDL are separate deployments. Application
code must first stop every legacy table and `show_page_id` column select or write;
only then may a later deployment drop the column and table. This ordering is a release
dependency, not a data bridge, backfill, import, or compatibility phase.

### Runtime negotiation and isolation

The Runtime protocol remains version `1`, with `X-Avibe-Show-Protocol` and
`X-Avibe-Show-Context: private | shared`. Avibe removes browser-supplied copies and
creates exactly one server-owned loopback envelope.

Every Runtime graph is keyed by `(session_id, context)`. Private and shared graphs
have independent lifecycle. Shared traffic, shared failures, another Session, and
ordinary editor file edits cannot create, rebase, or close a shared graph as a side
effect. Shared prewarm is an explicit admitted operation. Arbitrarily many ordinary
editor edits keep private HMR identity stable and leave shared graph counts unchanged
unless a real shared request or explicit prewarm is admitted.

Protocol-1 validation precedes Session resolution and every graph lookup, create,
rebase, ownership mutation, prewarm, or HMR connection. Headerless released clients
retain the legacy singleton base path. Unknown protocols reject without graph side
effects. Capability probe failures retry with a bounded deadline and all cached
outcomes reset when Runtime process identity changes.

Shared graphs use immutable opaque namespaces. Recursive TSX, CSS, raw-loader, and
worker dependencies remain in one namespace with captured provenance; handles never
reopen source paths or cross namespaces. Namespace lifetime is non-renewable, memory
uses one process-wide weighted budget with per-Session bounds and private reserve,
and sanitized overload is the only shared admission failure. Every shared document,
module, resource, fallback, API response, and stream is bounded to 60 seconds. Its
hard deadline is the earlier of admission plus 60 seconds and namespace absolute
expiry. Runtime then terminates the handler or stream, atomically releases the pin and
weighted charge, and emits only a sanitized timeout or reload-required result. No
pre-expiry admission can keep a namespace or process slot alive past absolute expiry;
unbounded shared streams are unsupported and clients may reconnect.

Every public and limited shared response is `private, no-store`: shell, entry,
document, module, SPA fallback, API handler, redirect, and error. Limited responses
also vary on cookie-sensitive identity. Shared responses strip unsafe URL headers and
cannot be reused across principals or after an audience-mode change. Arbitrary page
code runs in an opaque-origin iframe without `allow-same-origin`, so Service Worker
registration is unsupported and fails with a security error. Avibe emits no
`Service-Worker-Allowed` header, and no worker controls `/p/` or `/show/`; ordinary
Web Worker/module loading remains a separate capability-path surface. Redirects
preserve safe route suffix and query. `/p/<share_id>/__vite_hmr` never exists.

Canonical `/show/` HMR accepts exactly one normalized Origin only after the existing
local/remote WebSocket trust classifier resolves one server-owned source: configured
hosted instance, active custom hostname, direct localhost/IPv4/IPv6 loopback at the
actual UI port, explicit private/CGNAT/link-local setup host, a wildcard bind resolved
to a concrete enumerated LAN/Tailscale interface, an explicitly enabled loopback-only
Docker bridge, or trusted-proxy facts resolving to a configured public origin.
`0.0.0.0`, `::`, `*`, raw Host, untrusted forwarded values, wildcard/suffix matches,
and scheme/host/effective-port drift are never authority. Origin and resource-editor
authority both pass before any upstream WebSocket opens. One coalesced persistent
monitor per active Session polls the durable editor capability, resource authority,
remote authorization and its revision, and ShowAccess availability at most every four
seconds. Notifications may wake it earlier but are never required; read uncertainty
fails closed. Editor loss, resource revocation, remote authorization loss/revision
change, and durable offline each close every upstream socket within five seconds of
the durable change, with at most one second after detection, including just-after-poll
and lost-notification traces. Audience mode, binding, guest membership, or audience
revision alone does not close canonical HMR while independent editor authority remains.

### Runtime release gate

The reviewed Runtime baseline remains exact head
`ee3b0b490ad8b4afafb59cf37e2d57a20325208a` from Runtime PR #59. It does not
implement keyed context, so `show-context-key-v1` advertisement remains forbidden.
Advertisement may become true only when the named Runtime context-isolation and
opaque shared capture/admission property owners are implemented and the same exact
reviewed Runtime SHA is smoke-tested and pinned by the bundled manifest. Workflows
may not resolve Runtime `main` dynamically after advertisement. Contract tests require
the reviewed, smoke-tested, and bundled SHAs to match.

## Architecture

The access flow has one owner at each boundary:

1. The browser calls the local settings HTTP boundary.
2. The UI process authorizes owner or sharing-control authority and calls the
   controller over the internal socket.
3. The controller holds the page-scoped stable writer lease for both Apply and durable
   active/offline transitions, transactionally replaces the local aggregate, and
   records the page-scoped idempotency receipt. Workbench archive/reactivate cannot
   write availability around that owner.
4. A limited `/p/` request either has a verified identity or starts the local-owned
   identity handshake.
5. Backend authenticates identity only and signs the instance-bound assertion.
6. Local Avibe re-resolves the share and evaluates current membership plus independent
   resource authority.
7. Avibe selects redirect, private, shared, login, or deny. Shared admission produces
   the trusted `/p/` shell and, for limited viewers, a binding-scoped browsing
   credential for the opaque-origin iframe.
8. Avibe constructs the trusted Runtime envelope; Runtime owns isolated, bounded
   private/shared graph behavior.

No step sends the local email set to Backend. No Backend response contains page
membership or an Instance role derived from it.

## Delivery Sequence

1. Freeze this design, schemas, fixtures, executable scenario claims, and mirror
   registry.
2. Implement local ShowAccess storage, child membership rows, writer lease, Apply,
   and owner settings read in Avibe.
3. Implement the identity-only Backend assertion and local login/callback flow.
4. Retire hosted table/endpoints/clients and remove `show_page_email` Instance
   authorization.
5. Implement the closed route/capability decision, trusted shared shell, opaque-origin
   containment, browsing credential, and trusted Runtime envelope.
6. Implement Runtime keyed-context isolation and recursive shared confinement.
7. Implement capability probe/release gating and private HMR revocation.
8. Build the three-mode UI and complete local Incus/browser acceptance.

Each implementation lane consumes the same registry version. Contract proof in this
PR does not count as later Backend, Runtime, UI, or Incus conformance evidence.

## Verification Plan

Contract tests must:

- validate every schema and fixture and reject removed hosted/migration vocabulary;
- exhaust the local Apply cartesian product and prove every point selects exactly one
  terminal transition or reject;
- prove mode, resulting binding, normalized membership, idempotency, CAS, revision,
  availability preservation, and page identity relations directly;
- prove local membership removal denies the next limited request without Backend and
  does not promise active tab closure;
- prove identity assertions cannot carry page authority, membership, Instance roles,
  or privileged surface capability;
- execute exact callback-origin equality, strict compact RS256 JWT/JWKS types, paired
  trust, derived 720-second rotation/refresh boundaries, bounded signed-state and
  JWKS-cache clocks, the flow-specific cookie/state/nonce/`jti` concurrent login
  machine, browser-accurate session-lineage rotation, and the later identity-session
  request through expiry/removal/reset;
- prove the trusted `/p/` shell can load a current limited member while arbitrary
  sibling page code cannot fetch, frame, open/read, or use ambient credentials for it;
- exhaust every protected surface and method across current/offline/mode/binding/
  revision/membership/expiry/reclaim inputs and the full post-mint transition table;
- execute hung handler, infinite stream, request-deadline, namespace-expiry,
  concurrent reclaim, ledger recovery, and post-release admission traces;
- exhaust public/limited shared response surfaces and require `private, no-store`;
- reject noncanonical stored/result emails, globally contended share bindings, and a
  latest-only receipt implementation using known-answer canonical digest vectors and
  Apply A, Apply B, then replay A;
- exhaust the orthogonal resource/membership capability matrix;
- prove stable binding retention and explicit rotation in every audience mode;
- prove direct retirement contains no migration or compatibility phase;
- prove repeated editor edits never create or rebase a shared graph;
- exhaust every HMR server-owned origin source and mutate cardinality, scheme, host,
  port, peer, forwarded trust, and editor authority before upstream open;
- keep existing Runtime constant, response-sanitization, confinement, budget,
  revocation, and release-SHA checks;
- bind every semicolon-delimited Expected evidence clause below to one or more
  executable scalar claims; mutating any expected scalar must fail its claim.

### Browser and regression scenarios

The scenario catalog is acceptance input for future implementation lanes. Contract
claims prove the intended values exist and relate correctly; they do not replace
browser, Backend, Runtime, or local Incus evidence.

| ID | Scenario | Expected evidence |
| --- | --- | --- |
| `SHOW-LIVE-001` | Resource editor opens `/p/<share>/` and the Agent edits | Trusted top-level navigation redirects to canonical `/show/` with private no-store metadata and safe suffix/query preservation; resource editor receives HMR and annotations only on `/show/`; missing multiple or untrusted HMR Origin rejects before upstream WebSocket; React Fast Refresh preservation remains a future browser check |
| `SHOW-LIVE-002` | Listed-only guest opens limited `/p/` and the Agent edits | Trusted shell plus a current binding-scoped credential serves the opaque-origin shared page; sibling page code cannot fetch frame or open/read it; `/p/` has no HMR or annotations; new content appears only after a later refresh |
| `SHOW-LIVE-003` | Unlisted identity completes limited login and retries | Exact callback origin strict JWT types and derived JWKS lifecycles close one cross-site form POST; callback atomically rotates one local identity-only session lineage; the later limited request re-resolves current membership; absent membership returns one generic denial with no page bytes or loop |
| `SHOW-LIVE-004` | The same stable link changes from limited to fully public | Local Apply preserves the binding and advances audience revision once; anonymous `/p/` receives an admitted protected-content capability and becomes readable; shared readers still have no HMR or annotations |
| `SHOW-LIVE-005` | Private `/show/` and shared `/p/` traffic run together | Runtime graphs are keyed by Session and context; shared lifecycle cannot rebase or close private HMR; both `/p/` audiences remain shared |
| `SHOW-LIVE-006` | Identity resolution fails for public and limited requests | Public `/p/` independently admits anonymous protected content and remains readable; limited `/p/` fails closed; neither outcome selects HMR |
| `SHOW-LIVE-007` | Editor authority is revoked while canonical HMR is open | One persistent authority monitor closes every unauthorized socket within five seconds even after a lost notification; remaining resource viewer can still read private modules; page membership does not affect the result |
| `SHOW-LIVE-008` | A listed email is removed while its limited page is open | One local transaction replaces the membership set; the next request is denied without Backend access; an already loaded guest tab is not actively closed |
| `SHOW-LIVE-009` | A viewer connects directly to `/p/<share>/__vite_hmr` | The shared HMR endpoint is absent; cookies and membership cannot enable it |
| `SHOW-LIVE-010` | Editor and shared viewers remain open through repeated edits | Private HMR graph identity stays stable; repeated edits create or rebase no shared graph; context and namespace resources stay bounded |
| `SHOW-LIVE-011` | Identity changes while a shared document loads modules | Entry and modules remain on the shared representation selected for that request chain; all shared surfaces are private and no-store; opaque-origin code cannot cross into a sibling share; no private Session path or mixed graph appears |
| `SHOW-LIVE-012` | Independent resource viewer opens canonical `/show/` | Complete private modules are readable; HMR and annotations remain editor-only |
| `SHOW-LIVE-013` | Shared content attempts Service Worker registration before an editor opens the link | Opaque-origin registration fails with a security error and no Service-Worker-Allowed header; no worker controls `/p/` or `/show/` and no private bytes are exposed; ordinary Web Worker support remains separate |
| `SHOW-LIVE-014` | Avibe runs against Runtime without keyed-context support | Shared viewers use the explicit legacy singleton compatibility path; HMR stays disabled on `/p/`; compatibility is not advertised as isolation |
| `SHOW-LIVE-015` | First capability probe is transiently unavailable | Current request remains shared; retry delay is bounded; Runtime process identity change clears all cached outcomes |
| `SHOW-LIVE-016` | Shared transforms emit nested TSX, CSS, raw-loader, worker, and unsafe responses | Recursive dependencies stay in one immutable opaque namespace; handles never reopen paths or escape namespaces; diagnostics and host paths are sanitized |
| `SHOW-LIVE-017` | Startup and show-update explicitly prewarm graphs | Every prewarm carries a typed server envelope; protocol validation occurs before graph mutation; ordinary editor edits never implicitly prewarm shared context |
| `SHOW-LIVE-018` | Direct CLI changes an active page to offline with HMR connected | Apply and offline transitions share one page writer and preserve both ordered effects; one coalesced persistent monitor observes durable state; polling plus closure completes within five seconds even after a lost event |
| `SHOW-LIVE-019` | Public changes to limited while Backend is unavailable | One local transaction preserves the stable binding and installs normalized membership; anonymous access stops immediately; Backend availability is irrelevant to Apply |
| `SHOW-LIVE-020` | The unused hosted exact-email model is retired | Backend table and authorized-email endpoints are deleted; Avibe hosted-email clients are deleted; application reads stop before destructive DDL; no migration or compatibility bridge exists; local legacy null bindings remain null while non-null bindings are preserved |
| `SHOW-LIVE-021` | Process crashes around a limited-list replacement and retries | Before-transaction crash leaves the old aggregate; after-commit crash leaves the complete new aggregate; canonical digest vectors make Apply A then B then replay A return A's original terminal result; no cloud coordinator exists |
| `SHOW-LIVE-022` | Limited changes to private while Backend is unavailable | Private commits locally and disables `/p/`; the stable binding is retained but inactive; listed-only identity still cannot use `/show/` |
| `SHOW-LIVE-023` | Legacy Runtime returns an unsafe transform error and nested raw path | Both responses use fixed path-free output; no host path or development diagnostic reaches the browser |
| `SHOW-LIVE-024` | Source path changes after an opaque handle is issued | Existing handle serves immutable captured bytes; handle reads do not reopen the path; unsafe replacement cannot receive new admission |
| `SHOW-LIVE-025` | Apply uses stale revision reuses a mutation ID or contends for a custom slug | Stale expected audience revision rejects before write; conflicting payload reuse rejects before write; an atomically detected binding collision returns share_id_taken without write; none changes the local aggregate |
| `SHOW-LIVE-026` | Owner applies a canonically identical limited email set | Normalization produces one lowercase unique lexicographically sorted set; noncanonical persisted or result order rejects; Apply returns no change; audience revision and stable binding remain unchanged |
| `SHOW-LIVE-027` | Anonymous reads touch superseded namespaces before idle deadlines | Absolute lifetime remains non-renewable and cancels every remaining pin; whole namespace and snapshot bytes are reclaimed; stale document must reload |
| `SHOW-LIVE-028` | Shared traffic exceeds the Runtime-wide budget | Process and per-Session bounds hold with a sixty-second in-flight limit and one ledger owner; private reserve cannot be consumed by shared traffic; excess shared admission is sanitized |
| `SHOW-LIVE-029` | Limited changes to public without rotating its link | Local Apply preserves the binding; membership becomes empty; anonymous reads begin from the same slug immediately |
| `SHOW-LIVE-030` | Released headerless Avibe client sends only legacy base | Entry module fallback and prewarm retain singleton propagation; keyed context remains disabled; unknown protocol still rejects safely |
| `SHOW-LIVE-031` | Apply crashes at every local transaction boundary | There is no partially authoritative aggregate; receipts with frozen canonical digests survive later Apply and replay the original result; no hosted operation cleanup or reconciliation exists |
| `SHOW-LIVE-032` | Offline page changes future audience and rotates its custom link | Availability stays offline; mode binding and membership commit atomically; neither surface is admitted until explicit activation |
| `SHOW-LIVE-033` | Instance ACL revision changes while local membership is unchanged | Local membership is evaluated fresh on every limited request; Instance revision cannot add or remove membership; membership never becomes Instance access |
| `SHOW-LIVE-034` | Custom-slug public page changes to limited | The exact custom binding is preserved; normalized local membership installs atomically; listed shared reads and anonymous denial use the same slug |
| `SHOW-LIVE-035` | Resource viewer and editor open `/p/` with a legacy Runtime | Both trusted top-level requests redirect to canonical `/show/`; viewer remains read-only; editor HMR remains on the private canonical surface |
| `SHOW-LIVE-036` | Authorized owner reads limited settings | Route HTTP IPC result and projection page identities all match or no settings return; exact normalized emails come only from local storage; actual HTTP metadata is private and no-store while the projection excludes mutation receipts; Backend receives no whitelist data |
| `SHOW-LIVE-037` | Listed guest completes identity-only login | Backend returns one strictly typed signed verified instance-bound identity assertion with derived fail-closed JWKS retention; Avibe closes exact-origin callback correlation and atomically rotates one local identity-only session lineage; a later current-membership check mints a binding-scoped credential only for opaque-origin shared `/p/` |
| `SHOW-LIVE-038` | Listed-only guest copies page ID and requests canonical `/show/` | Current membership plus a binding-scoped credential positively serves limited `/p/`; canonical document module API and HMR remain denied; membership and browsing credential create no Instance role or editor capability |
| `SHOW-LIVE-039` | Malicious code on a public sibling page targets a limited share | Public sibling code obtains no limited shell bootstrap handle capability cookie DOM CORS response Service Worker registration opener or protected bytes; every forged or ambient request is denied; capability-path OPTIONS and JSON mutations are credentialless and sibling attempts are sanitized; trusted shell plus current membership and public anonymous admission remain positive paths |

## Acceptance Gate

The design is ready for implementation lanes only when:

- one local transactional writer owns mode, stable binding, audience revision,
  canonical membership replacement, and page-scoped idempotency receipts;
- receipts remain durable for the page lifetime without time/count eviction and
  cascade-delete only with the page;
- local legacy private/public/offline rows map deterministically, with offline
  failing closed to private and no hosted bridge;
- the complete Apply product selects exactly one transition or reject for every
  closed input point;
- local removal denies the next limited request without Backend and no contract
  promises active guest-tab closure;
- identity-only Backend assertions cannot express page or Instance authorization;
- the shared auth scenario executes form POST with compact RS256/JWKS verification,
  exact server-owned callback-origin checks, strict wire types, flow-specific callback
  cookies, atomic concurrent nonce/`jti` consumption and session-lineage rotation,
  paired issuer/audience/instance trust, derived key retention, and safe return checks;
- resource viewer/editor authority remains independent of local membership;
- private disables but retains a stable binding, limited/public preserve it, and
  explicit rotation replaces it;
- the unused hosted table, endpoints, clients, grant protocol, migration, and rollout
  concepts are direct-retirement targets, not compatibility states;
- `/p/` always stays shared and capability-poor while canonical `/show/` remains the
  only private HMR and annotation surface;
- malicious sibling page code cannot obtain or use another share's bootstrap,
  opaque capability, browser authority, or protected bytes;
- public anonymous and current-member limited admissions both reach protected shared
  content through credentialless capability-path requests and a closed API preflight;
- every protected request revalidates active local state and the single admission
  revision before atomically pinning Runtime handles;
- private HMR validates one exact server-owned Origin from the closed source algebra
  plus resource-editor authority before upstream open, and one persistent monitor
  gives every durable revocation source a five-second total closure bound even when
  notification delivery is lost;
- Runtime protocol, graph isolation, immutable shared confinement, the 60-second
  in-flight deadline and absolute-expiry pin cancellation, budgets, proxy sanitation,
  and release provenance remain frozen;
- every SHOW-LIVE Expected evidence clause has executable scalar contract proof;
- later UI, Backend, Runtime, browser, and Incus lanes remain explicitly residual.

## Non-Goals

- Enterprise administration of each page's guest list. A future organization policy
  may only narrow local choices; it cannot remotely add guests or require uploading
  the list.
- Immediate revocation of content already loaded in a listed guest tab.
- Page guest access to `/show/`, Workbench, APIs, Agents, HMR, or annotations.
- Hosted storage, migration, compatibility, or distributed coordination for exact
  page-email membership.
- Anonymous Live Reload.
