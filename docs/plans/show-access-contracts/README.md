# Show Access Contract v2

This directory freezes the machine-readable cross-repository contract for local
Show Page audience state, identity-only limited login, orthogonal resource
capabilities, direct retirement of the unused hosted email model, and Runtime
context isolation.

The authority is Issue #1498 and `../public-show-live-update.md`. The design bytes
are pinned by Git blob SHA in `mirror-registry.json` and
`scenario-bindings.json`. The exact PR head that receives review pins the complete
contract set; the blob pin avoids an impossible self-referential commit SHA.

## Ownership

Local Avibe is the only authority and persistence location for `access_mode`, the
stable share binding, `audience_revision`, and normalized exact-email membership.
The controller process owns one transactional writer under a stable cross-process
lease. That transaction also records a durable receipt keyed by page and mutation,
so replay remains deterministic after later Apply operations. Receipts live for the
page lifetime, are never evicted by time or count, and cascade-delete with the page.
The receipt digest covers the full normalized Apply body using recursive RFC 8785
canonical JSON, UTF-8, SHA-256, and lowercase hex; known-answer vectors freeze the
bytes across language and version changes.
The UI process
authorizes and forwards; it never coordinates or writes.

Avibe Backend authenticates identity only. Its compact RS256 JWT/JWS assertion is instance-bound
and contains a verified email, but no page membership, page authorization, Instance
role, or `show_page_email` access source. Local Avibe re-resolves the share and
checks current local membership on every limited request.
The executable identity owner derives one server-owned callback origin as exact
scheme, normalized host, and effective port, then creates one host-only, callback-scoped,
`SameSite=None`, `Secure`, `HttpOnly` cookie per signed nonce. The cookie value is
an independent 32-byte secret, state is verified before cookie selection, and atomic
nonce/`jti` retention supports concurrent flows. The reference harness performs a
real HTTP form POST plus strict compact JWT/JWKS verification. Signed state and paired-issuer
JWKS caches have closed clocks and fail-closed refresh behavior. Callback success
atomically advances a token-hash-backed, exact-origin-bound identity-only session
lineage and invalidates every prior generation. Its `SameSite=None` cookie supports
the cross-site callback without becoming page authorization. A nonce-scoped server
flow record captures only a currently valid prior token hash and lineage, so concurrent
callbacks serialize without giving a later stale token lineage authority. The session has a
non-renewable 24-hour maximum; every later limited request still checks current local
membership. The HTTP harness executes the cookie policy model; real-browser delivery
remains residual conformance evidence.

There is no migration or compatibility phase. The unused hosted storage, endpoints,
clients, and Instance authorization source are direct deletion targets.

## Contract Table

| Artifact | Frozen surface |
| --- | --- |
| `show-access.schema.json` | Local aggregate, exact membership, stable binding, revision, mutation receipt |
| `apply-invocation.json` | Owner-or-sharing-control invocation boundary and early denial |
| `apply-mutation.schema.json` | Local Apply request, terminal result, and rejected result |
| `apply-transition-algebra.json` | Exhaustive CAS, idempotency, binding, membership, and revision algebra |
| `fixtures/apply-mutations.json` | Canonical transitions, crash/idempotency trace, next-request revocation |
| `owner-settings.schema.json` | Page-correlated local settings IPC and actual private/no-store HTTP metadata |
| `identity-auth.json` | Compact RS256/JWKS, bounded state/cache, concurrent callback, and identity-session state machines |
| `local-legacy-mapping.json` | Deterministic local SQLite private/public/offline mapping with offline fail-closed |
| `capability-matrix.json` | Closed `/p` and `/show` decision over independent resource and membership axes |
| `shared-browser-containment.json` | Trusted shell, opaque Runtime capture, every-request validation, sibling isolation |
| `retirement.json` | Direct removal inventory with migration and compatibility forbidden |
| `runtime-context.json` | Protocol, trusted envelope, isolated graphs, demand-only shared work, confinement and budgets |
| `mirror-registry.json` | Exact future producer, consumer, signature, delivery and serialization owner |
| `scenario-bindings.json` | Executable scalar claims for every SHOW-LIVE expected-evidence clause |

## Invariants

- Private disables but retains a stable binding; limited/public reuse it; explicit
  rotation or custom binding replaces it.
- Apply never changes availability and never calls Backend. Apply and durable
  availability transitions share one page writer; an effective transition advances
  the same `audience_revision` exactly once and a no-op advances zero.
- Canonical mode, binding, or email-set change advances `audience_revision` once;
  canonical no-op does not.
- Same mutation and payload replays its stored terminal result even after later
  Apply operations; different payload reuse and stale revision reject before write.
- Receipts remain for the page lifetime with no time/count eviction and are removed
  only by the page cascade.
- Legacy local offline maps to offline/private; private/public map active in their
  matching mode; any existing stable binding remains, a null binding stays null, and
  no hosted data is imported.
- Listed-only identity can read current limited `/p` and nothing privileged.
- Resource viewer/editor authority is independent. Trusted top-level `/p`
  navigation redirects to canonical `/show`; only editor authority enables HMR and
  annotations there.
- `/p` always selects shared Runtime and never exposes HMR, annotations, private
  context, Session internals, Workbench, APIs, or Agents.
- Arbitrary shared code runs in an opaque-origin sandbox and cannot obtain or reuse
  another share's bootstrap, handle, capability, cookie, DOM, CORS response, opener,
  or protected bytes. Service Worker registration is unsupported and no
  Service-Worker-Allowed header is emitted; ordinary Web Workers remain separate.
- Public anonymous and current-member limited admission both mint opaque capability-
  path authority. Protected document/module/resource/API requests are credentialless;
  JSON mutation preflight has an exact closed method/header policy.
- Every protected surface and method revalidates active local ShowAccess, shared mode,
  exact binding, revision, capability lifetime, and limited membership before Runtime
  atomically pins a live namespace/document handle. Offline-to-active never revives an
  old capability; a request pinned before change may finish only within the hard
  request deadline, and later requests reload.
- Every shared response, including streams, has a hard deadline at the earlier of
  60 seconds after admission or namespace absolute expiry. The deadline terminates the
  work and atomically releases its pin and weighted charge; no request can keep a
  namespace or process slot alive past absolute expiry.
- Membership removal affects the next request without Backend. Loaded guest tabs are
  not actively closed.
- Ordinary editor file edits never create, rebase, or background-build a shared
  Runtime graph. Shared prewarm is explicit and admitted.
- Canonical HMR accepts only one exact Origin resolved by the existing WebSocket trust
  classifier across hosted/custom, loopback, setup, enumerated wildcard interface,
  Docker-loopback, and trusted-proxy sources. Origin and resource-editor authority
  both pass before upstream open. One coalesced persistent monitor polls every durable
  editor/resource/remote-auth/revision/offline source and gives each a five-second
  total closure bound even without notifications.

## Validation

`tests/test_show_access_contracts.py` parses every JSON document, validates all
schema examples, exhausts Apply, capability, protected-request, identity-flow, and
HMR-origin state spaces, rejects
retired vocabulary/files, checks Runtime constants and release provenance, and
evaluates every scenario leaf claim. Each scalar claim is also evaluated with a
mutated expectation to prove the check is sensitive to the claimed value.

This PR is contract evidence only. UI, storage, Backend, Runtime, browser, and local
Incus conformance remain work for their named implementation lanes.
