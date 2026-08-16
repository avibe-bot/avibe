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
The UI process
authorizes and forwards; it never coordinates or writes.

Avibe Backend authenticates identity only. Its signed assertion is instance-bound
and contains a verified email, but no page membership, page authorization, Instance
role, or `show_page_email` access source. Local Avibe re-resolves the share and
checks current local membership on every limited request.

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
| `owner-settings.schema.json` | Local authorized exact-email settings read with `private, no-store` |
| `identity-auth.json` | Backend identity assertion and local signed-state/callback ownership |
| `local-legacy-mapping.json` | Deterministic local SQLite private/public/offline mapping with offline fail-closed |
| `capability-matrix.json` | Closed `/p` and `/show` decision over independent resource and membership axes |
| `shared-browser-containment.json` | Trusted shell, opaque Runtime capture, protected browser requests, sibling isolation |
| `retirement.json` | Direct removal inventory with migration and compatibility forbidden |
| `runtime-context.json` | Protocol, trusted envelope, isolated graphs, demand-only shared work, confinement and budgets |
| `mirror-registry.json` | Exact future producer, consumer, signature, delivery and serialization owner |
| `scenario-bindings.json` | Executable scalar claims for every SHOW-LIVE expected-evidence clause |

## Invariants

- Private disables but retains a stable binding; limited/public reuse it; explicit
  rotation or custom binding replaces it.
- Apply never changes availability and never calls Backend.
- Canonical mode, binding, or email-set change advances `audience_revision` once;
  canonical no-op does not.
- Same mutation and payload replays its stored terminal result even after later
  Apply operations; different payload reuse and stale revision reject before write.
- Receipts remain for the page lifetime with no time/count eviction and are removed
  only by the page cascade.
- Legacy local offline maps to offline/private; private/public map active in their
  matching mode; any existing stable binding remains and no hosted data is imported.
- Listed-only identity can read current limited `/p` and nothing privileged.
- Resource viewer/editor authority is independent. Trusted top-level `/p`
  navigation redirects to canonical `/show`; only editor authority enables HMR and
  annotations there.
- `/p` always selects shared Runtime and never exposes HMR, annotations, private
  context, Session internals, Workbench, APIs, or Agents.
- Arbitrary shared code runs in an opaque-origin sandbox and cannot obtain or reuse
  another share's bootstrap, handle, capability, cookie, DOM, CORS response, worker
  scope, opener, or protected bytes.
- Membership removal affects the next request without Backend. Loaded guest tabs are
  not actively closed.
- Ordinary editor file edits never create, rebase, or background-build a shared
  Runtime graph. Shared prewarm is explicit and admitted.

## Validation

`tests/test_show_access_contracts.py` parses every JSON document, validates all
schema examples, exhausts both the Apply algebra and capability matrix, rejects
retired vocabulary/files, checks Runtime constants and release provenance, and
evaluates every scenario leaf claim. Each scalar claim is also evaluated with a
mutated expectation to prove the check is sensitive to the claimed value.

This PR is contract evidence only. UI, storage, Backend, Runtime, browser, and local
Incus conformance remain work for their named implementation lanes.
