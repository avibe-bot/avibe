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
or any identity-provider call. Settings responses are `private, no-store` and return
a user-facing projection of the local aggregate, including exact emails but excluding
`last_mutation`, request digests, and Apply receipts, only to an authorized caller.

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

Avibe Backend returns one short-lived signed identity assertion. Authorization-code
exchange is not part of this contract.
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

Its audience is `avibe-show-identity:<oauthClientId>`, normal lifetime is 300
seconds, hard maximum is 600 seconds, and verifier skew is 60 seconds. The email is
ASCII-trimmed and lowercased from a fresh verified Backend identity lookup. Only the
current key signs; JWKS keeps current plus previous public keys for at least the hard
maximum plus skew. `identity_not_verified` and `identity_unavailable` are no-store,
assertion-free terminal errors. Backend issues unique `jti`; local Avibe consumes
the nonce and `jti` once.

It contains no `page_id`, `share_id`, membership result, page authorization,
Instance role, `InstanceAccessSource`, audience revision, grant revision, or
whitelist data. The browser cannot supply or override the instance binding or
verified email.

The local callback verifies signature, issuer, audience, expiry, nonce, single use,
and instance binding, then returns only to the signed safe `/p/` target. The next
limited request re-resolves the share and performs a fresh local membership lookup.
An identity session is evidence of identity, never cached page authorization. An
unlisted identity receives one generic denial with no page bytes and no login loop.
Removal from the list takes effect on the next request without Backend access; an
already loaded guest tab is not actively closed.

### Shared viewer browser containment

The visible URL remains `/p/<share_id>/`. Avibe serves a trusted platform shell at
that URL and runs arbitrary shared page code only inside a sandboxed opaque-origin
iframe with `allow-scripts` and without `allow-same-origin`. Only the trusted shell
may frame the shared document. Shared page code has no DOM, cookie, CORS, Service
Worker, opener, or ambient-identity path to another share's bytes.

After current local membership succeeds, Avibe authorizes an internal Runtime capture
request. Runtime returns only an opaque namespace handle, document handle, and expiry.
Avibe then issues an entropy-backed browsing capability; browser routes accept only
the opaque handles and capability, never a Session ID, workspace/source path, or
browser-supplied Runtime context. The browsing capability is bound to
`(instance_id, page_id, share_id, audience_revision,
namespace_handle, document_handle)`. The trusted shell holds it ephemerally and every
protected shared document, module, fallback, and API request presents it. Those
responses are credentialless CORS (`Access-Control-Allow-Origin: *`, never credentials
or `Set-Cookie`). Vendor assets stay namespace-scoped and capability-required. The
capability is neither the Backend identity assertion nor a browser cookie, Instance
role, resource authority, or editor capability. A binding or revision mismatch
re-resolves the share and current membership before any new credential or page bytes.
Consequently, code on a sibling public page cannot fetch, frame, open and read, or
reuse ambient credentials for a limited page.

### Direct retirement of the unused hosted model

The local SQLite schema migration is intentionally separate from hosted retirement.
Existing active private/public values map to `availability=active` with the matching
mode. Legacy offline cannot recover its historical audience mode, so it maps fail
closed to `availability=offline` and `access_mode=private`. Any existing `share_id`
is retained as an inactive stable binding, emails initialize empty, and
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
and sanitized overload is the only shared admission failure.

Every public and limited shared response is `private, no-store`: shell, entry,
document, module, SPA fallback, API handler, redirect, and error. Limited responses
also vary on cookie-sensitive identity. Shared responses strip unsafe URL and Service
Worker scope headers and cannot be reused across principals or after an audience-mode
change. Redirects preserve safe route suffix and query while remaining outside shared
worker scope. `/p/<share_id>/__vite_hmr` never exists.

Canonical `/show/` HMR revalidates origin, resource access, editor capability, and
authorization revision. Existing sockets close on editor loss, resource revocation,
authorization revision change, or durable offline state. Avibe owns one coalesced
monitor per active Session; direct durable offline changes close all sockets within
five seconds. Audience or share changes do not close canonical HMR while independent
editor authority remains.

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
3. The controller holds the stable writer lease and transactionally replaces the
   local aggregate and records the page-scoped idempotency receipt.
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
- execute the signed-state, correlation-cookie, single-use nonce, instance binding,
  safe return, share re-resolution, and current-membership login loop;
- prove the trusted `/p/` shell can load a current limited member while arbitrary
  sibling page code cannot fetch, frame, open/read, or use ambient credentials for it;
- exhaust public/limited shared response surfaces and require `private, no-store`;
- reject noncanonical stored/result emails, globally contended share bindings, and a
  latest-only receipt implementation using Apply A, Apply B, then replay A;
- exhaust the orthogonal resource/membership capability matrix;
- prove stable binding retention and explicit rotation in every audience mode;
- prove direct retirement contains no migration or compatibility phase;
- prove repeated editor edits never create or rebase a shared graph;
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
| `SHOW-LIVE-001` | Resource editor opens `/p/<share>/` and the Agent edits | Trusted top-level navigation redirects to canonical `/show/`; resource editor receives HMR and annotations only on `/show/`; React Fast Refresh preservation remains a future browser check |
| `SHOW-LIVE-002` | Listed-only guest opens limited `/p/` and the Agent edits | Trusted shell plus a current binding-scoped credential serves the opaque-origin shared page; sibling page code cannot fetch frame or open/read it; `/p/` has no HMR or annotations; new content appears only after a later refresh |
| `SHOW-LIVE-003` | Unlisted identity completes limited login and retries | Signed state cookie nonce assertion and instance binding close one callback loop; signed state returns only to the resolved share; current local membership is absent; one generic denial returns no page bytes and does not loop |
| `SHOW-LIVE-004` | The same stable link changes from limited to fully public | Local Apply preserves the binding and advances audience revision once; anonymous `/p/` becomes readable; shared readers still have no HMR or annotations |
| `SHOW-LIVE-005` | Private `/show/` and shared `/p/` traffic run together | Runtime graphs are keyed by Session and context; shared lifecycle cannot rebase or close private HMR; both `/p/` audiences remain shared |
| `SHOW-LIVE-006` | Identity resolution fails for public and limited requests | Public `/p/` remains readable; limited `/p/` fails closed; neither outcome selects HMR |
| `SHOW-LIVE-007` | Editor authority is revoked while canonical HMR is open | Existing unauthorized HMR sockets close; remaining resource viewer can still read private modules; page membership does not affect the result |
| `SHOW-LIVE-008` | A listed email is removed while its limited page is open | One local transaction replaces the membership set; the next request is denied without Backend access; an already loaded guest tab is not actively closed |
| `SHOW-LIVE-009` | A viewer connects directly to `/p/<share>/__vite_hmr` | The shared HMR endpoint is absent; cookies and membership cannot enable it |
| `SHOW-LIVE-010` | Editor and shared viewers remain open through repeated edits | Private HMR graph identity stays stable; repeated edits create or rebase no shared graph; context and namespace resources stay bounded |
| `SHOW-LIVE-011` | Identity changes while a shared document loads modules | Entry and modules remain on the shared representation selected for that request chain; all shared surfaces are private and no-store; opaque-origin code cannot cross into a sibling share; no private Session path or mixed graph appears |
| `SHOW-LIVE-012` | Independent resource viewer opens canonical `/show/` | Complete private modules are readable; HMR and annotations remain editor-only |
| `SHOW-LIVE-013` | Shared content registers a Service Worker before an editor opens the link | Shared worker scope cannot include `/show/`; no private bytes are served at `/p/`; redirect responses are private and preserve safe suffix and query |
| `SHOW-LIVE-014` | Avibe runs against Runtime without keyed-context support | Shared viewers use the explicit legacy singleton compatibility path; HMR stays disabled on `/p/`; compatibility is not advertised as isolation |
| `SHOW-LIVE-015` | First capability probe is transiently unavailable | Current request remains shared; retry delay is bounded; Runtime process identity change clears all cached outcomes |
| `SHOW-LIVE-016` | Shared transforms emit nested TSX, CSS, raw-loader, worker, and unsafe responses | Recursive dependencies stay in one immutable opaque namespace; handles never reopen paths or escape namespaces; diagnostics and host paths are sanitized |
| `SHOW-LIVE-017` | Startup and show-update explicitly prewarm graphs | Every prewarm carries a typed server envelope; protocol validation occurs before graph mutation; ordinary editor edits never implicitly prewarm shared context |
| `SHOW-LIVE-018` | Direct CLI changes an active page to offline with HMR connected | One coalesced monitor observes durable state; every Session socket closes within five seconds; no in-process event is required |
| `SHOW-LIVE-019` | Public changes to limited while Backend is unavailable | One local transaction preserves the stable binding and installs normalized membership; anonymous access stops immediately; Backend availability is irrelevant to Apply |
| `SHOW-LIVE-020` | The unused hosted exact-email model is retired | Backend table and authorized-email endpoints are deleted; Avibe hosted-email clients are deleted; application reads stop before destructive DDL; no migration or compatibility bridge exists |
| `SHOW-LIVE-021` | Process crashes around a limited-list replacement and retries | Before-transaction crash leaves the old aggregate; after-commit crash leaves the complete new aggregate; Apply A then B then replay A returns A's original terminal result; no cloud coordinator exists |
| `SHOW-LIVE-022` | Limited changes to private while Backend is unavailable | Private commits locally and disables `/p/`; the stable binding is retained but inactive; listed-only identity still cannot use `/show/` |
| `SHOW-LIVE-023` | Legacy Runtime returns an unsafe transform error and nested raw path | Both responses use fixed path-free output; no host path or development diagnostic reaches the browser |
| `SHOW-LIVE-024` | Source path changes after an opaque handle is issued | Existing handle serves immutable captured bytes; handle reads do not reopen the path; unsafe replacement cannot receive new admission |
| `SHOW-LIVE-025` | Apply uses stale revision reuses a mutation ID or contends for a custom slug | Stale expected audience revision rejects before write; conflicting payload reuse rejects before write; an atomically detected binding collision returns share_id_taken without write; none changes the local aggregate |
| `SHOW-LIVE-026` | Owner applies a canonically identical limited email set | Normalization produces one lowercase unique lexicographically sorted set; noncanonical persisted or result order rejects; Apply returns no change; audience revision and stable binding remain unchanged |
| `SHOW-LIVE-027` | Anonymous reads touch superseded namespaces before idle deadlines | Absolute lifetime remains non-renewable; whole namespace and snapshot bytes are reclaimed; stale document must reload |
| `SHOW-LIVE-028` | Shared traffic exceeds the Runtime-wide budget | Process and per-Session bounds hold; private reserve cannot be consumed by shared traffic; excess shared admission is sanitized |
| `SHOW-LIVE-029` | Limited changes to public without rotating its link | Local Apply preserves the binding; membership becomes empty; anonymous reads begin from the same slug immediately |
| `SHOW-LIVE-030` | Released headerless Avibe client sends only legacy base | Entry module fallback and prewarm retain singleton propagation; keyed context remains disabled; unknown protocol still rejects safely |
| `SHOW-LIVE-031` | Apply crashes at every local transaction boundary | There is no partially authoritative aggregate; durable page-plus-mutation receipts survive later Apply and replay the original result; no hosted operation cleanup or reconciliation exists |
| `SHOW-LIVE-032` | Offline page changes future audience and rotates its custom link | Availability stays offline; mode binding and membership commit atomically; neither surface is admitted until explicit activation |
| `SHOW-LIVE-033` | Instance ACL revision changes while local membership is unchanged | Local membership is evaluated fresh on every limited request; Instance revision cannot add or remove membership; membership never becomes Instance access |
| `SHOW-LIVE-034` | Custom-slug public page changes to limited | The exact custom binding is preserved; normalized local membership installs atomically; listed shared reads and anonymous denial use the same slug |
| `SHOW-LIVE-035` | Resource viewer and editor open `/p/` with a legacy Runtime | Both trusted top-level requests redirect to canonical `/show/`; viewer remains read-only; editor HMR remains on the private canonical surface |
| `SHOW-LIVE-036` | Authorized owner reads limited settings | Exact normalized emails come only from local storage; response projection excludes mutation receipts and is private and no-store; Backend receives no whitelist data |
| `SHOW-LIVE-037` | Listed guest completes identity-only login | Backend returns only one signed verified instance-bound identity assertion; Avibe closes signed-state cookie nonce callback correlation and re-resolves current membership; a binding-scoped credential serves only the opaque-origin shared `/p/` page |
| `SHOW-LIVE-038` | Listed-only guest copies page ID and requests canonical `/show/` | Current membership plus a binding-scoped credential positively serves limited `/p/`; canonical document module API and HMR remain denied; membership and browsing credential create no Instance role or editor capability |
| `SHOW-LIVE-039` | Malicious code on a public sibling page targets a limited share | Public sibling code obtains no limited shell bootstrap handle capability cookie DOM CORS response Service Worker scope opener or protected bytes; every forged or ambient request is denied; trusted shell plus current membership remains the positive path |

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
- resource viewer/editor authority remains independent of local membership;
- private disables but retains a stable binding, limited/public preserve it, and
  explicit rotation replaces it;
- the unused hosted table, endpoints, clients, grant protocol, migration, and rollout
  concepts are direct-retirement targets, not compatibility states;
- `/p/` always stays shared and capability-poor while canonical `/show/` remains the
  only private HMR and annotation surface;
- malicious sibling page code cannot obtain or use another share's bootstrap,
  opaque capability, browser authority, or protected bytes;
- Runtime protocol, graph isolation, immutable shared confinement, budgets, proxy
  sanitation, and release provenance remain frozen;
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
