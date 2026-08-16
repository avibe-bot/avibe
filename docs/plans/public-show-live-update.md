# Public Show Live Update

Status: Proposed

Date: 2026-08-16

Scope: `avibe` public Show proxy and `vibe-show-runtime`

## Decision

Public Show Pages remain live. Making a page public must not turn it into a
snapshot or require a viewer to refresh it manually.

The two surfaces use different update protocols because they have different
trust boundaries:

| Surface | Viewer trust | Update behavior | Runtime protocol exposed to the browser |
| --- | --- | --- | --- |
| Private `/show/<session>/` | Authenticated and authorized | Vite HMR and React Fast Refresh | Full Vite HMR |
| Public `/p/<share>/` | Anonymous read access | Automatic full-page Live Reload after a renderable revision | Avibe revision SSE only |
| Offline | No live page | None | None |

The public browser must never receive the raw Vite HMR protocol. It receives a
small, one-way, path-free revision notification and reloads the document. This
keeps the public page current without making Vite's development control channel
part of Avibe's anonymous product API.

This is deliberately called **Live Reload**, not HMR. Public viewers get the
updated page automatically, but a reload does not preserve arbitrary component
or application state. Private authors keep Fast Refresh and its state-preserving
developer experience.

## Why This Is Needed

The current implementation contains two conflicting contracts:

- `docs/plans/show-service-runtime.md` says private and public Show Pages both
  receive live HMR.
- The public proxy rewrites `@vite/client` and `@react-refresh` to inert,
  immutable shims, so a public browser never opens the existing
  `/p/<share>/__vite_hmr` socket.

The shims were introduced by `8dc1319eb` (`fix(show): stabilize public runtime
modules`). They solved a real stabilization problem and explicitly prevented an
anonymous Vite socket, but they also disabled public live updates. That behavior
became an accidental product policy while the design documents continued to
promise a live public page.

There is also a deeper runtime ownership problem. The sidecar currently keys an
active Vite context by both `session_id` and the external base path. Alternating
requests from `/show/<session>/` and `/p/<share>/` can therefore close and rebuild
the same Session's Vite server as its base path changes. A private author and a
public viewer should be able to use the page concurrently without restarting
each other's runtime.

## Product Contract

Visibility and update transport are separate capabilities:

- `public` controls who may read the rendered page.
- authorization controls who may annotate, dispatch, or use private tools.
- the surface trust boundary controls which update protocol is exposed.

For a concrete example, an Agent edits `src/App.tsx` while an anonymous viewer
has `/p/brief/` open:

1. Vite invalidates the affected module graph.
2. Show Runtime waits for the write burst to settle and validates the affected
   graph.
3. If validation succeeds and no newer write superseded it, Runtime commits the
   next in-memory revision.
4. Avibe relays only `{epoch, revision}` to the public browser.
5. The browser reloads `/p/brief/` and renders the new source.

If validation fails, the existing public document stays visible. The private
author still receives normal Vite diagnostics. The public viewer receives no
stack, source path, plugin name, source frame, or error text.

### User-visible requirements

1. An idle, visible public page reloads within 2 seconds in local regression
   after the last edit of a renderable write burst.
2. A failed transform does not replace an already-rendered public page with an
   error overlay or a blank page.
3. Once the source becomes renderable again, the next revision reloads the
   public page without manual action.
4. A hidden tab records a pending revision and reloads when it becomes visible.
5. A revision received while a text input, textarea, select, or editable element
   is active waits for blur, with a 30-second upper bound. This limits avoidable
   loss of in-progress public interaction without allowing a stale page to wait
   forever.
6. Public Live Reload is best effort when the runtime or connection is
   unavailable. The rendered page remains usable and the client reconnects with
   bounded backoff.

## Architecture

```text
Agent file write
  -> one canonical Vite context for the Session
  -> affected-graph validation + revision producer
  -> internal revision SSE (loopback sidecar API)
  -> Avibe public share/visibility gate and SSE relay
  -> small public live client
  -> location.reload()

Private browser
  -> authenticated /show/<session>/__vite_hmr
  -> raw Vite HMR for the same canonical Vite context
```

### One canonical runtime base

Each Session has one Vite context with a canonical base:

```text
/show/<session-id>/
```

The Vite context is keyed by `session_id`, not by an external share URL. Public
HTTP responses continue to rewrite canonical `/show/<session>/...` URLs to
`/p/<share>/...` at the Avibe boundary. Public requests no longer send the share
base as `x-vibe-show-base`, and rotating a share link does not restart Vite.

This invariant is required before Live Reload is enabled. Otherwise concurrent
private and public traffic can repeatedly change the Vite epoch and make both
update paths unreliable.

### Ownership

`vibe-show-runtime` owns:

- Vite invalidation and transform state
- the renderable-revision decision
- the per-Session `{epoch, revision}` counter
- the loopback-only revision stream

`avibe` owns:

- public share resolution and visibility checks
- public origin and host policy
- revocation when a share rotates or leaves `public`
- the redacted public SSE contract
- the small public browser client

The existing Show event stream remains separate. Show events are durable product
events with replay and authorization semantics. Runtime revisions are ephemeral
render state. Combining them would make one stream own two unrelated retention
and failure models.

## Renderable Revision Model

A revision is not emitted for every filesystem event. Runtime maintains a
generation token and follows this sequence:

1. A Vite-relevant invalidation increments the candidate generation and records
   the affected module URLs.
2. Runtime waits for a 250 ms quiet window and coalesces the burst.
3. Runtime transforms the affected first-party module graph. A Vite full-reload
   invalidation also validates the entry graph.
4. If any required transform fails, Runtime records a private diagnostic and
   emits no revision.
5. If another invalidation arrived during validation, Runtime discards the stale
   result and validates the newer generation.
6. Only a successful, still-current validation increments `revision` and emits
   one notification.

The validator must fail closed for a required first-party module. Dependencies
already resolved from the immutable shared vendor bundle do not need to be
revalidated on every edit.

This contract improves the experience for an already-open public page, but it is
not an atomic publishing system. Source files remain mutable on disk, and there
is no durable last-known-good artifact. A fresh viewer can still arrive while the
workspace is temporarily broken. Guaranteeing atomic public releases would
require a separate build-and-snapshot publishing model and is outside this
change.

## Revision Stream Contract

The sidecar adds a loopback-only endpoint:

```text
GET /sessions/<session-id>/revisions?stream=1
```

The public Avibe surface exposes:

```text
GET /p/<share-id>/__show/revisions
```

The public endpoint is a terminating relay, not a byte-for-byte proxy. It parses
the internal contract and serializes the public allowlist.

Initial state:

```text
event: ready
data: {"protocol":1,"epoch":"rte_7f42","revision":12}

```

Committed update:

```text
id: rte_7f42:13
event: revision
data: {"protocol":1,"epoch":"rte_7f42","revision":13}

```

Contract rules:

- `protocol` is the schema version and is currently `1`.
- `epoch` is an opaque random identifier for one active Vite context.
- `revision` is a monotonically increasing integer inside the epoch.
- The payload contains no Session ID, share ID, module URL, filesystem path,
  source code, error, or plugin data.
- Keepalives are SSE comment frames and carry no data.
- A new subscriber receives exactly one `ready` event with the current state;
  revision history is not persisted.

The Runtime health/status payload advertises revision-stream capability so Avibe
can stage the runtime release before activating the public client. An older
runtime yields a closed/no-content public stream; it never falls back to raw
public HMR.

## Public Client State Machine

The existing public `@vite/client` replacement becomes a small versioned live
client while retaining no-op HMR exports required by transformed modules.
`@react-refresh` remains inert on the public surface.

The live client runs once per document:

1. Read `globalThis.__AVIBE_SHOW__.basePath` and open
   `<basePath>__show/revisions` with `EventSource`.
2. Keep the current `{epoch, revision}` as an in-memory baseline for this
   document.
3. Treat the first `ready` as the baseline without an unnecessary reload.
4. On a greater revision, or an epoch change after that baseline, update the
   in-memory baseline before reloading. The new document also treats its first
   `ready` as a baseline, so no persisted reload marker or reload loop exists.
5. If the document is hidden or an editable control is active, mark the reload
   pending and apply it on visibility/focus release, subject to the 30-second
   bound.
6. On stream failure, close the failed `EventSource` and reconnect with capped
   exponential backoff. Stop after six consecutive failures; resume on `online`
   or a later visibility transition.

The client shows no public error overlay. Connection and validation diagnostics
belong in private Runtime/Avibe logs.

## Security Boundary

Opening private HMR unchanged on a public page is not acceptable because the
current route is a bidirectional proxy into Vite. Depending on installed Vite
plugins, client `custom` messages can reach server listeners, and development
error payloads can contain stack traces, source frames, plugin code, and local
paths. The Vite wire protocol is also an implementation detail that can change
with a Runtime release.

The public revision endpoint therefore enforces all of the following:

- the share exists, currently maps to the requested Session, and is `public`
- the request uses an allowed public host and an exact same-origin browser
  origin when an `Origin` header is present
- no browser cookies, authorization headers, CSRF headers, or query credentials
  are forwarded to the sidecar
- the connection closes when the share rotates or visibility changes
- only `ready` and `revision` payloads matching protocol v1 are relayed
- subscriber queues retain at most the latest pending revision
- responses use `Cache-Control: no-store`, `Content-Type: text/event-stream`,
  `X-Accel-Buffering: no`, and `Referrer-Policy: no-referrer`

The anonymous `/p/<share>/__vite_hmr` route is removed. Private
`/show/<session>/__vite_hmr` keeps its existing authentication, origin,
authorization-revision, and resource-revocation checks.

## Performance Model

The current public shim payload is about 1.3 KB decoded. Loading the real Vite
client and React Refresh on the sampled regression page added about 60.9 KB
compressed and about 291.8 KB decoded, both from no-cache development modules.
Those figures are a measured reference, not a permanent protocol guarantee.

The selected model has these budgets:

- public live client: at most 3 KB gzip
- no real Vite client or React Refresh runtime on `/p/`
- SSE connection starts after the module script executes and does not block the
  initial render
- one validation per Session write burst, independent of public viewer count
- no per-viewer Vite WebSocket or per-viewer module-graph validation
- immutable shared vendor assets remain cached across a full reload

Initial public page load should therefore not become materially slower. It adds
one small client and one asynchronous SSE handshake. An update is heavier than
private HMR because it reloads the document and first-party modules, but shared
vendor modules remain cached. That is the intentional cost of keeping the public
protocol small, stable, and read-only.

## Failure Behavior

| Failure | Public behavior | Private/operator evidence |
| --- | --- | --- |
| Transform or syntax error | Keep current rendered document; emit no revision | Normal Vite/Runtime diagnostic |
| Runtime unavailable | Keep page; bounded reconnect | Runtime health/logs |
| SSE interrupted | Compare `ready` against stored revision after reconnect | Connection metric/log |
| Share rotated/private/offline | Close stream; old public URL cannot reconnect | Visibility/resource event |
| New invalidation during validation | Discard stale result; validate latest generation | Debug metric only |
| Runtime context restarted | New epoch; existing client reloads once | Runtime lifecycle log |

## Rejected Alternatives

### Expose private Vite HMR on `/p/`

This gives the fastest update and preserves React state, but exposes a
bidirectional development protocol, diagnostics, version coupling, and a Vite
socket per anonymous viewer. Filtering enough of Vite to make that protocol safe
would create a second partial HMR implementation that remains coupled to Vite.

### Poll the public document

Polling is simple but creates traffic while nothing changes, adds update latency,
and still needs a revision/ETag source. A push stream has a smaller idle cost and
a clearer state model.

### Put revisions on the durable Show event stream

This reuses a connection but conflates ephemeral compilation state with durable
human/assistant events and their replay, storage, redaction, and dispatch rules.
The apparent transport reuse creates a larger conceptual system.

### Build and publish immutable snapshots after every write

Snapshots could provide atomic publication and a durable last-known-good page,
but they introduce build artifacts, retention, activation, rollback, and storage
policy. That is a separate publishing capability, not the smallest complete fix
for live public viewing.

## Delivery Sequence

1. Freeze protocol v1 as shared example fixtures consumed by Runtime and Avibe
   contract tests.
2. In `vibe-show-runtime`, add the generation validator, revision state, internal
   SSE stream, capability advertisement, and focused tests.
3. Release the Runtime artifact and update Avibe's managed Runtime manifest.
4. In `avibe`, enforce the canonical Session base, add the terminating public SSE
   relay, activate the versioned public live client, and remove the public raw HMR
   route.
5. Update `show-service-runtime.md` and the multipage scaffold plan so `HMR`
   means the private protocol and `Live Reload` means the public behavior.
6. Run local Incus regression with simultaneous private and public viewers before
   enabling the feature by default.

The rollout must not activate the public client before the managed Runtime
advertises protocol v1. During a mixed-version development checkout, public pages
remain readable but do not fall back to raw HMR.

## Verification Plan

### Runtime unit tests

- coalesce a file burst into one committed revision
- do not emit when an affected transform fails
- discard a successful validation superseded by a newer generation
- validate the entry graph for a full-reload invalidation
- issue a new epoch after a Vite context restart
- replay only the current `ready` state to a new subscriber

### Avibe unit and contract tests

- public live client uses revision SSE and retains no-op HMR exports
- anonymous public revision stream succeeds only for the current public share
- private/offline/rotated shares cannot open or retain the stream
- public relay drops unknown event types and all extra fields
- browser credentials are never forwarded to the sidecar
- public raw `__vite_hmr` is unavailable while private HMR still works
- private and public HTTP requests reuse one canonical Runtime context

### Browser and regression scenarios

| ID | Scenario | Expected evidence |
| --- | --- | --- |
| `SHOW-LIVE-001` | Agent edits a renderable public page | Open public page reloads within 2 seconds and shows the change |
| `SHOW-LIVE-002` | Private author and public viewer are open together | Private Fast Refresh works; public Live Reload works; Runtime epoch stays stable |
| `SHOW-LIVE-003` | Agent writes a syntax error, then fixes it | Public page keeps old render, exposes no diagnostic, then reloads after the fix |
| `SHOW-LIVE-004` | Public share rotates or becomes private/offline | Existing stream closes and the old share cannot reconnect |
| `SHOW-LIVE-005` | Many viewers watch one page | One revision validation and no per-viewer Vite sockets |
| `SHOW-LIVE-006` | Public page cold load | No real Vite/React Refresh client; live client stays within the 3 KB gzip budget |
| `SHOW-LIVE-007` | Viewer is typing or the tab is hidden | Revision waits according to the focus/visibility contract, then reloads once |

The implementation adds these scenarios to the Show scenario catalog before its
PR is considered complete. Focused unit/contract tests, Ruff, the repository CI
suite, and local Incus browser regression are all required; green unit tests do
not replace the simultaneous private/public browser check.

## Acceptance Gate

The feature is complete when all of the following are true:

- Public Show is live by default and requires no manual viewer refresh.
- Public browsers cannot reach a raw Vite HMR channel or receive Vite diagnostics.
- Private HMR behavior and authorization remain unchanged.
- Concurrent private/public use does not rebuild the Session Runtime because of
  external base-path changes.
- Failed edits preserve the already-rendered public document and recover on the
  next valid revision.
- Initial-load and update measurements satisfy the budgets above in local Incus
  regression.
