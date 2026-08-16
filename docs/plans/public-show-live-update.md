# Public Show Live Update

Status: Proposed

Date: 2026-08-16

Scope: `avibe` public Show proxy and `vibe-show-runtime`

## Decision

Public Show Pages remain live. Making a page public must not turn it into a
manually refreshed snapshot, but an anonymous browser must not become a client
of the mutable Vite development server either.

The two surfaces therefore use different publication models:

| Surface | Viewer trust | Content source | Update behavior |
| --- | --- | --- | --- |
| Private `/show/<session>/` | Authenticated and authorized | Canonical Vite development context | Vite HMR and React Fast Refresh |
| Public `/p/<share>/` | Anonymous read access | Last atomically activated public artifact | Automatic full-page Live Reload |
| Offline | No live page | None | None |

For a concrete example, suppose the public page imports `renderChart` from
`chart.ts`, and an Agent removes that named export while changing another file:

1. Private HMR may show the author the normal Vite diagnostic.
2. Runtime builds the complete public entry graph into a staging directory.
3. Rollup linking fails because the import no longer exists.
4. Runtime discards the candidate and keeps serving the previous activated
   artifact to every public viewer.
5. After the Agent repairs the export, a complete build succeeds, Runtime
   atomically activates it, and public viewers reload automatically.

The public browser receives neither raw Vite HMR nor mutable development
modules. It receives an opaque active-artifact notification over a small,
one-way Server-Sent Events (SSE) contract and reloads the public document when
the artifact changes.

This is deliberately called **Live Reload**, not HMR. A public reload does not
preserve arbitrary component or application state. Private authors retain Fast
Refresh and its state-preserving development experience.

## Why This Is Needed

The current implementation contains two conflicting contracts:

- `docs/plans/show-service-runtime.md` says private and public Show Pages both
  receive live HMR.
- The public proxy rewrites `@vite/client` and `@react-refresh` to inert,
  immutable shims, so a public browser never opens the existing
  `/p/<share>/__vite_hmr` socket.

The shims were introduced by `8dc1319eb` (`fix(show): stabilize public runtime
modules`). They prevented an anonymous Vite socket, but also disabled public
live updates. That implementation detail accidentally became product behavior.

There is also a deeper ownership problem. The sidecar currently keys an active
Vite context by both `session_id` and an external base path. Alternating private
and public requests can close and rebuild the same Session's Vite server as the
base changes. More importantly, transforming individual modules in that mutable
context cannot establish a stable public revision: static linking, stylesheet
asset URLs, and the document-to-stream baseline can each disagree even when the
individual transforms succeeded.

The public publication boundary must therefore be a complete, immutable build,
not a momentary observation of the development module graph.

## Product Contract

Visibility, publication, and update transport are distinct capabilities:

- `public` controls who may read the activated public artifact.
- authorization controls who may annotate, dispatch, or use private tools.
- the public artifact controls which frontend version an anonymous viewer gets.
- the surface trust boundary controls which update protocol is exposed.

The last successfully activated artifact is the public page's last-known-good
version. A candidate becomes visible only after its complete frontend build and
static module linking succeed. Candidate files are never served in place.

This guarantee is intentionally precise. It prevents syntax, import/export
linking, asset-graph, and build-plugin failures from replacing a working public
page. A successful static build cannot prove that arbitrary application logic
will not throw in a real browser. Browser canary execution would be required for
that stronger guarantee and is not part of this change.

### User-visible requirements

1. An idle, visible public page reloads automatically after the latest valid
   write burst has been built and activated.
2. A failed public build does not replace an activated page with an error
   overlay, partial graph, or blank document.
3. Once the source builds again, the next activated artifact reloads the page
   without manual action.
4. A hidden tab records a pending artifact and waits without a deadline until it
   becomes visible.
5. Once visible, a pending reload waits while a text input, textarea, select, or
   editable element is active, but for at most 30 seconds of visible editable
   time. Visibility takes precedence over this deadline.
6. Public Live Reload is best effort while Runtime or SSE is unavailable. The
   activated page remains usable and the client reconnects with bounded backoff.
7. The first public request after a cold Runtime or after public visibility is
   enabled may wait for one initial build. Once an artifact is active, ordinary
   page loads do not wait for a build.

## Architecture

```text
Agent file write
  -> canonical private Vite development context (private HMR only)
  -> demand-active public artifact builder
  -> complete Vite production build in staging
  -> generation check + atomic artifact activation
  -> loopback active-artifact stream
  -> Avibe public share/visibility gate and SSE relay
  -> small public live client
  -> location.reload()

Public browser
  -> /p/<share>/ document and relative artifact assets
  -> never reads canonical /show/<session>/ development modules
```

### Private runtime ownership

Each Session has at most one Vite development context. It is keyed by
`session_id` and uses the canonical private base:

```text
/show/<session-id>/
```

Only the authenticated private surface reads that context and its HMR channel.
Public requests no longer send a share-specific `x-vibe-show-base`, do not
change the Vite base, and cannot restart the private development context.

### Public artifact ownership

`vibe-show-runtime` owns:

- coalescing Vite-relevant workspace invalidations
- complete public frontend builds
- staging, validation, activation, retention, and cleanup of public artifacts
- the opaque active-artifact identifier
- the loopback-only artifact stream and artifact file endpoint

`avibe` owns:

- public share resolution and durable visibility checks
- public origin and host policy
- injection of request-specific public document configuration
- the gated public artifact proxy
- the redacted public SSE contract and browser client
- bounded revocation when a share rotates or leaves `public`

The existing Show event stream remains separate. Show events are durable product
events with replay and authorization semantics. Artifact activations are
ephemeral render state. Combining them would make one stream own unrelated
retention, redaction, and failure models.

### Demand and cold start

Public builds are demand-active rather than running for every private Session:

- A public document request asks Runtime to ensure an artifact for the current
  workspace generation. Concurrent requests join the same build.
- An open public SSE subscriber holds demand for its Session, so later edits are
  built without waiting for another HTTP request.
- With no public demand, Runtime may stop building. The next public request
  catches up to the latest generation before returning the document.
- If a previous artifact exists and a catch-up build fails, Runtime serves that
  artifact. If no artifact has ever activated, Avibe returns the existing
  sanitized generating/unavailable shell and retries on a later request; it
  never falls back to development modules.

This keeps private-only editing costs unchanged while ensuring a fresh public
viewer cannot observe a partially edited source tree.

## Atomic Artifact Model

Runtime maintains a candidate generation for each Session and follows this
sequence:

1. A frontend-relevant invalidation increments the candidate generation.
2. Runtime waits for a 250 ms quiet window and coalesces the write burst.
3. Runtime allocates a new opaque artifact identifier and builds the complete
   frontend entry graph into a staging directory outside the workspace.
4. The build uses Vite's production build and Rollup linking, disables source
   maps, excludes development HMR/React Refresh clients, and emits content-hashed
   files with relative asset references.
5. Runtime rejects the candidate if the build, link, or any required first-party
   asset resolution fails. Output validation also rejects canonical private URL
   references in emitted HTML, JavaScript, and CSS, including CSS `url(...)`.
6. If another invalidation arrived during the build, Runtime discards the stale
   output and schedules the newest generation.
7. Runtime moves the complete staging directory into immutable artifact storage,
   then atomically replaces the active pointer only if the generation is still
   current.
8. Only pointer activation emits one artifact notification.

The active artifact and its files are immutable. Runtime retains the active and
immediately previous artifact; older inactive artifacts are cleaned up after no
response or stream still references them. A process restart may rebuild the
same source into a new opaque identifier, which causes at most one public reload.
Artifacts are Runtime cache, not durable product history.

Relative output is part of the contract. Avibe serves the same artifact beneath
the current `/p/<share>/` path and injects a matching document `<base>` plus
public configuration. HTML module references, JavaScript chunks, imported CSS,
and CSS `url(...)` references therefore resolve through the gated share path and
contain neither `/show/<session>/` nor a previous share ID. Rotating a share does
not require rebuilding the artifact.

Public `api/` handlers remain permissioned live server code and are not bundled
into the frontend artifact. Relative public API requests continue through the
existing Avibe gate. A handler-only edit does not require a frontend reload.

## Artifact Stream Contract

The sidecar adds loopback-only endpoints:

```text
GET /sessions/<session-id>/public-artifact?ensure=current
GET /sessions/<session-id>/public-artifacts/<artifact-id>/<path>
GET /sessions/<session-id>/public-artifacts?stream=1
```

The public Avibe surface exposes:

```text
GET /p/<share-id>/__show/artifacts
```

The public endpoint is a terminating relay, not a byte-for-byte proxy. It parses
the internal contract and serializes only the public allowlist.

Every served public document contains the identifier of the exact artifact from
which its HTML was read:

```html
<script>
  globalThis.__AVIBE_SHOW__ = { basePath: "/p/brief/", artifact: "art_7f42" }
</script>
```

The real value is serialized with the existing safe configuration-injection
mechanism; the example is illustrative, not an inline-string implementation.

Initial stream state:

```text
event: ready
data: {"protocol":1,"artifact":"art_7f42"}

```

Activated update:

```text
id: art_91ac
event: artifact
data: {"protocol":1,"artifact":"art_91ac"}

```

Contract rules:

- `protocol` is the schema version and is currently `1`.
- `artifact` is an opaque identifier for one immutable activated build.
- Equality is the only browser operation on `artifact`; ordering is unnecessary.
- The payload contains no Session ID, share ID, module URL, filesystem path,
  source code, error, plugin name, or source frame.
- Keepalives are SSE comment frames and carry no data.
- A new subscriber receives exactly one `ready` event with the current active
  artifact. Artifact history is not replayed.
- The document uses `Cache-Control: no-store`; hashed artifact files may use
  immutable caching while the share remains authorized.

Embedding the artifact in the document closes the initial-subscription race. If
activation occurs after HTML is served but before the first `ready`, the two
identifiers differ and the client reloads. The first `ready` is never accepted
as an unverified baseline.

Runtime health advertises artifact protocol v1 so Avibe can stage the Runtime
release before enabling the client. An older Runtime yields a closed/no-content
stream and never falls back to raw public HMR.

## Public Client State Machine

The public document loads a small versioned live client. Public production
artifacts contain no Vite HMR imports, so the client does not need to emulate
the Vite protocol. It runs once per document:

1. Read `basePath` and the document's `artifact` from the injected configuration.
2. Open `<basePath>__show/artifacts` with `EventSource`.
3. Compare both `ready` and later `artifact` events with the document artifact.
   Equal means current; different means a reload is pending.
4. Keep only the newest pending identifier. A later event does not reset any
   active editable-control deadline.
5. If the document is hidden, wait without starting or advancing the 30-second
   editable deadline.
6. When visible, reload immediately unless an editable control is active. While
   visible and editable, reload on blur or after 30 accumulated seconds. If the
   document becomes hidden, pause the deadline; visibility remains dominant.
7. Set an in-memory reload guard immediately before `location.reload()`. The new
   no-store document carries its own artifact and performs the same equality
   handshake, preventing a persistent reload loop.
8. On stream failure, close the failed `EventSource` and reconnect with capped
   exponential backoff. Stop after six consecutive failures and resume on
   `online` or a later visibility transition.

The client displays no public error overlay. Build and connection diagnostics
belong in private Runtime/Avibe logs.

## Security and Revocation Boundary

Raw Vite HMR is not acceptable on a public page because the current route is a
bidirectional proxy into a development server. Client `custom` messages can
reach installed plugin listeners, and development diagnostics can contain local
paths, stack traces, source frames, and plugin code.

Every public document, artifact-file, API, and SSE request therefore resolves
the share against the durable `ShowPageStore` and requires that it currently
maps to the Session with `public` visibility. The SSE relay additionally:

- revalidates durable share state at connection time, before forwarding an
  artifact event, and on every keepalive interval of at most 5 seconds
- closes immediately on an in-process share/visibility broker event when one is
  available
- closes within 5 seconds even when `vibe show update` changed the store from a
  different process and no in-memory broker event exists
- requires an allowed public host and exact same-origin browser origin when an
  `Origin` header is present
- forwards no cookies, authorization headers, CSRF headers, or query credentials
  to Runtime
- accepts only protocol-v1 `ready` and `artifact` payloads
- retains at most the latest pending artifact per subscriber
- responds with `Cache-Control: no-store`, `Content-Type: text/event-stream`,
  `X-Accel-Buffering: no`, and `Referrer-Policy: no-referrer`

The anonymous `/p/<share>/__vite_hmr` route is removed. Private
`/show/<session>/__vite_hmr` keeps its authentication, origin,
authorization-revision, and resource-revocation checks.

Revocation cannot erase bytes an anonymous browser already downloaded or cached;
it prevents new authorized reads and bounds how long an existing live stream can
remain attached. That is the same unavoidable limit as any public web response.

## Performance Model

The cost model has three distinct cases:

| Case | Cost |
| --- | --- |
| Normal public page load with an active artifact | Static hashed files plus one small asynchronous SSE client; no build on the request path |
| Valid edit while public demand exists | One coalesced production build per Session, one atomic activation, then one full reload per viewer |
| First public request with no current artifact | One joined initial build before the document can be served |

Budgets and invariants:

- public live client: at most 3 KB gzip
- no Vite client, React Refresh runtime, source map, or public HMR socket
- one build per Session write burst, independent of viewer count
- no public build while a private-only Session has no public demand
- active hashed assets remain cacheable across document reloads
- active plus previous artifact bounds steady-state artifact retention
- build work is cancellable by generation and never runs concurrently for the
  same Session

An activated public page should not load slower because Live Reload is enabled:
the SSE connection starts after the module script and does not block rendering.
The model does add CPU and update latency compared with transform-only HMR, and a
cold first public request can wait for a build. Local Incus measurements must set
the final starter-page activation budget; the design does not claim a universal
2-second bound before measuring representative pages.

## Failure Behavior

| Failure | Public behavior | Private/operator evidence |
| --- | --- | --- |
| Syntax, import/export, asset, or build-plugin failure | Keep active artifact; activate nothing | Vite build/Runtime diagnostic |
| Arbitrary runtime exception after a successful build | Browser may fail; no stronger guarantee without a canary | Browser telemetry or manual regression |
| New invalidation during a build | Discard stale candidate; build latest generation | Debug metric only |
| Runtime unavailable | Keep loaded page; bounded reconnect | Runtime health/logs |
| SSE interrupted | Compare reconnecting `ready` with the document artifact | Connection metric/log |
| Share rotated/private/offline | Reject new reads; close stream immediately or within 5 seconds | Durable visibility state and relay log |
| Runtime restarted | Reuse a valid cached artifact or rebuild; a new ID reloads once | Runtime lifecycle log |
| Initial build fails with no active artifact | Sanitized unavailable/generating shell; no development fallback | Private build diagnostic |

## Rejected Alternatives

### Expose private Vite HMR on `/p/`

This is fastest and preserves React state, but exposes a bidirectional
development protocol, diagnostics, version coupling, and one Vite socket per
anonymous viewer. Filtering Vite deeply enough would create a second partial HMR
implementation that remains coupled to Vite internals.

### Publish a transform-validated mutable graph

Transforming affected modules is cheaper than a build, but cannot validate
static linking across unchanged importers, cannot make the served document and
first SSE baseline atomic, and still requires response rewriting for JavaScript,
HTML, and stylesheet asset URLs. It has no stable unit to retain as
last-known-good. These are properties of the model, not isolated proxy bugs.

### Poll the public document

Polling creates traffic while nothing changes, adds update latency, and still
needs an authoritative artifact identifier. SSE has a smaller idle cost and a
clearer equality handshake.

### Put artifacts on the durable Show event stream

This reuses a connection but conflates ephemeral compilation state with durable
human/assistant events and their replay, storage, redaction, and dispatch rules.
The apparent transport reuse creates a larger conceptual system.

### Run a browser canary before every activation

A canary can catch some runtime exceptions that a static build cannot, but adds
a browser lifecycle, readiness protocol, timeout policy, side-effect isolation,
and substantially higher edit latency. Static full-graph build validation is the
smallest boundary that fixes the demonstrated blank-page class. A canary can be
added later only if real failures justify the stronger publication guarantee.

## Delivery Sequence

1. Freeze protocol-v1 artifact fixtures and the document configuration shape as
   shared contract files consumed by Runtime and Avibe tests.
2. In `vibe-show-runtime`, add the demand lease, generation-coalesced production
   builder, immutable storage, atomic activation, loopback file endpoint, stream,
   and capability advertisement.
3. Release the Runtime artifact and update Avibe's managed Runtime manifest.
4. In `avibe`, keep the private Vite context canonical, replace public
   development-module proxying with the gated artifact proxy, inject the exact
   document artifact/base, add the terminating SSE relay and durable-state
   revalidation, and remove public raw HMR.
5. Update `show-service-runtime.md` and the multipage scaffold plan so `HMR`
   means the private protocol and `Live Reload` means activated public artifacts.
6. Add scenario-catalog entries, then run local Incus regression with concurrent
   private/public viewers and direct CLI visibility mutation.

The rollout must not activate the public client before managed Runtime advertises
protocol v1. During a mixed-version checkout, the feature remains disabled:
public pages keep the current readable, non-live behavior and never expose raw
HMR. The implementation PR must define the explicit compatibility cutover and
remove the old public development-module path once the bundled Runtime is
guaranteed compatible.

## Verification Plan

### Runtime unit tests

- coalesce a file burst into one candidate build
- statically link the full graph and reject a removed named export referenced by
  an unchanged importer
- leave the active pointer unchanged for syntax, link, plugin, and asset failure
- discard a successful build superseded by a newer generation
- atomically activate only complete immutable output
- join concurrent initial requests to one build
- hold demand while at least one public stream is subscribed
- retain active plus previous artifact and clean only unreferenced older output
- replay only the current active artifact to a new subscriber

### Avibe unit and contract tests

- public documents embed the exact served artifact identifier
- a `ready` value different from the embedded identifier reloads instead of
  becoming an unchecked baseline
- anonymous artifact and SSE requests succeed only for the current public share
- direct `ShowPageStore` mutation from `vibe show update` closes a stream within
  the 5-second bound without relying on an in-memory broker event
- public relay drops unknown event types and extra fields
- browser credentials are never forwarded to Runtime
- emitted HTML, JavaScript, and CSS contain no canonical Session path or old
  share ID; CSS `url(...)` assets resolve through the current gated share
- public raw `__vite_hmr` is unavailable while private HMR remains unchanged
- private and public traffic never changes or rebuilds the private Vite base

### Browser and regression scenarios

| ID | Scenario | Expected evidence |
| --- | --- | --- |
| `SHOW-LIVE-001` | Agent edits a valid public page | One artifact activates and the visible public page reloads automatically |
| `SHOW-LIVE-002` | Private author and public viewer are open together | Private Fast Refresh and public Live Reload both work; private Vite context stays stable |
| `SHOW-LIVE-003` | Agent removes a named export still imported elsewhere, then repairs it | Public page retains the previous artifact with no diagnostic, then reloads after repair |
| `SHOW-LIVE-004` | Public share rotates or direct CLI changes visibility to private/offline | Existing stream closes within 5 seconds and old share reads fail |
| `SHOW-LIVE-005` | Many viewers watch one page | One candidate build per burst and no per-viewer Vite sockets or builds |
| `SHOW-LIVE-006` | Public page cold-loads with a current artifact | No Vite/React Refresh client or source maps; live client stays within 3 KB gzip |
| `SHOW-LIVE-007` | Revision arrives while both hidden and editing | No background deadline fires; after visibility, blur or 30 visible editable seconds causes exactly one reload |
| `SHOW-LIVE-008` | Activation races the first SSE `ready` | Embedded and ready identifiers differ; the client reloads to the active artifact |
| `SHOW-LIVE-009` | Page CSS references `url('./hero.png')`, then the share rotates | Asset loads on both share URLs and no canonical Session path is exposed |

The implementation adds these scenarios to the Show scenario catalog before its
PR is complete. Focused unit/contract tests, Ruff, repository CI, and local Incus
browser regression are required; green unit tests do not replace simultaneous
private/public browser verification.

## Acceptance Gate

The feature is complete when all of the following are true:

- Public Show updates automatically without exposing Vite HMR or diagnostics.
- Every public response belongs to one immutable activated artifact; candidate
  files are never served.
- Complete build/link failures preserve the active public artifact and recover
  on the next valid candidate.
- The document artifact and SSE state use an exact equality handshake, including
  the activation-before-first-`ready` race.
- Public HTML, JavaScript, CSS, and asset requests expose no canonical Session
  path and continue to work after share rotation.
- Direct cross-process visibility changes revoke reads and streams within the
  defined 5-second bound.
- Hidden state dominates the editable-control deadline exactly as specified.
- Private HMR behavior and authorization remain unchanged, and public traffic
  does not mutate the private Vite context.
- Cold-build, normal-load, activation, client-size, build-fanout, and artifact
  retention measurements satisfy the budgets established in local Incus.
