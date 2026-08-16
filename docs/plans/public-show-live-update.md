# Capability-Gated HMR for Public Show Links

Status: Proposed

Date: 2026-08-16

Scope: `avibe` public Show authorization/proxy and `vibe-show-runtime`

## Decision

A public Show link selects its update behavior from the viewer's effective
server-side capability:

| Surface and viewer | Runtime representation | Update behavior |
| --- | --- | --- |
| Private `/show/<session>/`, authorized editor | Canonical private Vite context | Vite HMR and React Fast Refresh |
| Public `/p/<share>/`, authorized editor | The same canonical private Vite context | Vite HMR and React Fast Refresh |
| Public `/p/<share>/`, anonymous, read-only, expired, resource-forbidden, or unverifiable | Isolated public transform context | No Hot Reload; refresh reads current content |
| Offline | None | None |

Public visibility is an anonymous read grant, not development authorization.
Making a page public therefore does not downgrade its editor's experience, and
being able to read a public link does not grant access to Vite's bidirectional
development protocol.

For a concrete example, Alice is an authorized editor and Bob is an anonymous
viewer. They open the same `/p/brief/` URL while an Agent edits the page:

1. Avibe resolves Alice's existing Workbench session and the same editor
   capability used to enable annotations.
2. Alice's document uses the canonical private module graph and
   `/show/<session>/__vite_hmr`; React Fast Refresh updates her page and preserves
   component state.
3. Bob's document uses the isolated public transform context. It contains the
   inert Vite/React Refresh shims and never opens an HMR socket.
4. Bob's loaded page stays unchanged. A later manual navigation or refresh reads
   the current public content.

This design intentionally does not add anonymous Live Reload. That requirement
would need an independently published revision model with artifact retention,
URL compatibility, API-version coherence, build sandboxing, and global build
budgets. None of that is necessary to deliver the requested authorized-editor
HMR behavior.

## Why The Current Page Does Not Update

The current public proxy rewrites `@vite/client` and `@react-refresh` to inert,
immutable shims. This prevents an anonymous browser from opening the existing
public Vite socket, but it applies the same behavior to an authenticated editor.
The authorization information already used by the annotation surface is not
used to select the Runtime representation.

There is a second ownership problem. Runtime currently lets a Session's Vite
context depend on the requested external base. Alternating `/show/<session>/`
and `/p/<share>/` requests can therefore rebuild or replace one context as the
base changes. Simply making the public socket conditional would still let an
anonymous request disrupt an authorized editor's HMR connection.

The fix is capability routing plus separate context ownership, not a weaker
client-side HMR switch.

## Product Contract

### One editor capability

Avibe computes one `ShowEditorCapability` from server-owned facts:

- a validated Workbench identity
- an editor role
- access to the Show Page resource
- a current public share-to-Session binding when the request uses `/p/`
- non-offline visibility

The public annotation decision and public-link HMR decision consume the same
result. The implementation should factor the existing annotation inputs into
one helper rather than make two similar authorization checks.

A cookie's presence, a browser-provided flag, the public share ID, or a
share-scoped annotation write token is insufficient. The annotation token proves
only one permitted public event write after capability selection; it is never
accepted for module or HMR access.

Missing, expired, read-only, resource-forbidden, ambiguous, or temporarily
unverifiable authorization fails closed to `public-no-hmr`. A remote identity
outage must not make an otherwise public page unavailable.

### Server-selected document mode

Before returning a public entry document or SPA fallback, Avibe selects exactly
one discriminated configuration:

```ts
type PublicRouteDocumentConfig =
  | {
      protocol: 1
      mode: "private-hmr"
      pageBasePath: string
      runtimeBasePath: string
      editorAuthorized: true
    }
  | {
      protocol: 1
      mode: "public-no-hmr"
      pageBasePath: string
      editorAuthorized: false
    }
```

For `private-hmr`, `pageBasePath` remains `/p/<share>/` while
`runtimeBasePath` is `/show/<session>/`. For `public-no-hmr`, the configuration
contains no Session identifier or private Runtime path. The existing structured
configuration injector serializes this object; it is not assembled by replacing
strings in HTML.

The mode is request-scoped, not stored on the page and not sticky in the
browser. Login or permission changes take effect on the next navigation. Losing
authorization closes the existing private HMR connection and rejects subsequent
private module reads; a later `/p/` navigation selects `public-no-hmr`.

### User-visible requirements

1. An authorized editor opening either `/show/` or `/p/` gets full Vite HMR and
   React Fast Refresh from the same canonical development context.
2. An anonymous or unauthorized `/p/` viewer never receives a Vite client,
   React Refresh runtime, HMR socket, private module URL, Session identifier,
   source frame, or development diagnostic.
3. The public link and page content remain readable without login.
4. An unauthorized loaded page does not update automatically. Refreshing it
   reads the current public workspace content.
5. A failed or unavailable identity lookup selects the public representation
   rather than delaying or failing the public page.
6. Private and authorized-public traffic cannot be restarted or rebased by an
   unauthorized public request.
7. Existing public path confinement, sensitive-path denial, and symlink escape
   protections remain in force for every public request.

## Architecture

```text
GET /p/<share>/...
  -> resolve current public share
  -> compute ShowEditorCapability
     -> authorized editor
        -> canonical private Vite context
        -> canonical /show/<session>/ module URLs
        -> existing private HMR socket and revocation
     -> everyone else
        -> isolated public transform context
        -> public URL rewriting and immutable shims
        -> no websocket and no automatic update
```

### Runtime context ownership

Runtime owns at most two contexts for a public Session:

| Context key | Base | Consumers | Lifetime |
| --- | --- | --- | --- |
| `(session_id, private)` | `/show/<session>/` | Private `/show/` plus editor-authorized `/p/` | Existing private demand/lifecycle |
| `(session_id, public)` | Current `/p/<share>/` | Unprivileged public requests only | Demand-created; disposable without affecting private HMR |

The private context is canonical. An authorized `/p/` request does not send a
share-specific `x-vibe-show-base`, rewrite private modules into the share path,
or create a second private HMR protocol. Its document is served at the public
URL, but generated module, React Refresh, and HMR URLs point to the authenticated
`/show/<session>/` routes.

The public transform context preserves today's no-HMR representation but no
longer owns or replaces the private context. A share rotation may recreate only
the disposable public context. Public context startup, failure, or traffic
cannot close the private socket.

The public context is a read transform surface, not a publication guarantee. It
may read the latest workspace state on navigation, so a fresh request during an
invalid edit can receive the existing sanitized unavailable behavior. This
change does not promise last-known-good artifacts or frontend/API revision
atomicity.

### HTTP routing

For an authorized public entry or SPA navigation, Avibe requests the canonical
private Runtime representation while preserving the browser's public
`pageBasePath`. Normal generated subresources then use `/show/<session>/` and
pass through the existing private route. If authored code makes a relative
request that still reaches `/p/`, Avibe recomputes the capability before routing
that request to the private context.

For an unprivileged request, Avibe uses only the public context and existing
public-safe response transforms. Exact files and API handlers retain first
refusal; route-shaped document misses retain the current SPA fallback. The
implementation must preserve the existing path policy as an invariant over the
whole public request surface:

- no sensitive segment is readable
- no symlink or `@fs` path escapes the allowed workspace/dependency roots
- no private Session path survives a public response
- no public request is forwarded to an arbitrary host file or plugin endpoint

The public context continues to use inert, versioned `@vite/client` and React
Refresh shims. It exposes neither Runtime diagnostics nor a message channel.

### WebSocket routing

`/show/<session>/__vite_hmr` remains the only HMR endpoint. It requires the same
effective editor capability, allowed origin, current remote authorization
revision, and Show Page resource access. Existing authorization and resource
broker tasks close it when those facts stop holding.

`/p/<share>/__vite_hmr` is removed for every viewer. An authorized public
document does not need it because its Vite client uses the canonical private
socket; an unprivileged document contains no live client.

### Cache boundary

Every `/p/` response whose bytes can differ by capability uses
`Cache-Control: private, no-store` and `Vary: Cookie`. Successful content-hashed
vendor assets at capability-independent global URLs may retain immutable
caching. Avibe never forwards browser cookies, authorization headers, CSRF
headers, or annotation tokens to Runtime.

This prevents an intermediary or browser cache from serving an editor's private
document to an anonymous viewer. Mode selection must happen before any document
or redirect bytes are returned; client-side replacement is not a security
boundary.

## Performance Model

The model adds authorization routing, not a production publication pipeline:

| Case | Cost |
| --- | --- |
| Authorized editor opens `/p/` | Existing session/resource decision, canonical Vite transforms, and one private HMR socket; equivalent to `/show/` |
| Unprivileged viewer opens `/p/` | Existing public transforms and shims from an isolated public context; no socket |
| Both modes are active | Up to two Vite contexts for the Session; only the private context maintains HMR clients |
| Agent edit | Private context pushes HMR; public context does no work until the next public request or refresh |

The authorization decision adds no browser round trip. A request without a
Workbench cookie immediately selects public mode; a request with a cookie uses
the existing bounded/cached identity resolution already needed by annotations.

An authorized `/p/` load should have the same cold and warm profile as `/show/`.
The isolated public context can increase Runtime memory when authorized and
anonymous viewers are active simultaneously, but avoids production builds,
artifact storage, SSE fanout, and anonymous sockets. Runtime must expose context
count and memory so local Incus regression can set a per-Session idle eviction
budget from measurements rather than an assumed universal threshold.

## Failure And Revocation Behavior

| Failure | Browser behavior | Operator evidence |
| --- | --- | --- |
| Missing/expired/read-only identity | Serve `public-no-hmr` | Redacted authorization reason |
| Identity service unavailable | Serve `public-no-hmr`; public availability does not depend on identity recovery | Authorization availability metric/log |
| Private Runtime unavailable for an authorized editor | Existing private sanitized recovery behavior; never fall through to a raw public socket | Private Runtime log |
| Public context unavailable | Existing sanitized public unavailable/static fallback | Public Runtime log without source detail |
| Editor or resource access revoked | Close private HMR and reject later private reads; next `/p/` navigation uses public mode | Existing authorization-revision/resource-revocation log |
| Share rotates or becomes private/offline | Old public reads fail immediately; private access remains governed independently | Durable Show Page state |

Revocation cannot erase module bytes already downloaded by a previously
authorized editor. It prevents future reads and terminates the bidirectional
channel, which is the same unavoidable boundary as the private `/show/` surface.

## Rejected Alternatives

### Enable `/p/` HMR for every public viewer

Public visibility grants content read access, not access to Vite custom messages,
plugin listeners, local paths, source frames, or Runtime diagnostics. This would
turn a share link into a development-server capability.

### Gate HMR only in the browser

An anonymous viewer can remove a client-side condition or connect directly.
Authorization must select the document representation and independently guard
every private module and socket request on the server.

### Treat any login cookie as permission

A cookie can be expired, read-only, resource-forbidden, or unrelated to the
Show Page. The complete editor capability, not cookie presence, is the boundary.

### Add an authenticated `/p/<share>/__vite_hmr` endpoint

This would duplicate private socket authorization/revocation and retain a
share-specific Vite base. Using canonical `/show/` module and socket URLs reuses
the existing boundary and avoids protocol drift.

### Share one base-switching Vite context

Alternating authorized and anonymous requests would continue to rebase or
restart the editor's development context. Separate private/public ownership is
the minimum isolation needed for reliable HMR.

### Add anonymous artifact Live Reload

An immutable artifact stream sounds safer than public HMR, but a complete model
must also sandbox build inputs, retain every loaded document's files, preserve
runtime-resolved asset URLs, coordinate live API handlers with frontend
revisions, and bound builds across Sessions. It is a separate publication
feature, not a prerequisite for capability-gated editor HMR.

## Delivery Sequence

1. Freeze `ShowEditorCapability` inputs and the discriminated document
   configuration as contract fixtures.
2. Factor public annotation authorization into the shared, resource-aware editor
   capability and use the same result for document mode selection.
3. Split Runtime ownership into canonical private and disposable public contexts
   without changing the private base or protocol.
4. Route authorized `/p/` documents and relative requests to the private context;
   keep their browser-visible page base public and their module/HMR base private.
5. Keep unprivileged `/p/` on the public transform/shim path and remove the
   public HMR websocket.
6. Add cache, revocation, concurrency, and path-confinement contract tests.
7. Run local Incus regression with authorized, read-only, anonymous, and revoked
   viewers open concurrently.

The capability branch can roll out with the bundled Runtime change. Mixed
versions fail closed: if Runtime cannot provide independently keyed contexts,
`/p/` keeps the existing no-HMR behavior for every viewer. It never enables the
old anonymous public socket as a compatibility fallback.

## Verification Plan

### Authorization and document contract tests

- seed every supported identity/resource shape and assert that `canAnnotate`
  and `private-hmr` are produced by the same editor capability
- prove anonymous, read-only, expired, resource-forbidden, and unverifiable
  requests receive only `public-no-hmr`
- prove query parameters, headers outside the trusted identity boundary, and
  annotation write tokens cannot upgrade document mode
- prove both capability-varying entry responses are private/no-store and vary on
  cookies
- prove the public configuration contains no Session identifier or private path

### Runtime and proxy tests

- private and public contexts have independent keys and lifecycles
- public requests never change, close, or rebuild the private context
- authorized `/p/` documents reference canonical private modules, React Refresh,
  and `/show/<session>/__vite_hmr`
- unprivileged documents reference only public paths and immutable shims
- `/p/<share>/__vite_hmr` rejects every connection
- private module and HMR requests repeat editor/resource/origin checks and close
  on authorization-revision or resource revocation
- every existing public path-confinement fixture remains denied by the new
  routing branch, including sensitive segments and workspace escapes
- direct CLI share rotation or visibility mutation invalidates the old public
  route without touching independently authorized private access

### Browser and regression scenarios

| ID | Scenario | Expected evidence |
| --- | --- | --- |
| `SHOW-LIVE-001` | Authorized editor opens `/p/<share>/` and the Agent edits a component | React Fast Refresh applies through `/show/` and preserves component state |
| `SHOW-LIVE-002` | Anonymous viewer opens the same link during the edit | No HMR socket exists and the loaded page stays unchanged until refresh |
| `SHOW-LIVE-003` | Signed-in read-only or resource-forbidden viewer opens the link | Behavior and bytes match anonymous public mode; no private identifier appears |
| `SHOW-LIVE-004` | Authorized `/show/`, authorized `/p/`, and anonymous `/p/` are open together | Both authorized views keep HMR while public traffic uses an independent context |
| `SHOW-LIVE-005` | Identity resolution fails for a request carrying an unverifiable cookie | The public page remains readable with no HMR or diagnostic leak |
| `SHOW-LIVE-006` | Editor or resource access is revoked while public-link HMR is open | The socket closes, private reads fail, and the next `/p/` navigation is no-HMR |
| `SHOW-LIVE-007` | Share rotates or direct CLI changes visibility | The old public URL stops serving while authorized private routing remains independent |
| `SHOW-LIVE-008` | Public requests target every existing denied path shape | All remain denied before Runtime access; no sensitive or escaped file is returned |
| `SHOW-LIVE-009` | A viewer connects directly to `/p/<share>/__vite_hmr` | The connection is rejected regardless of cookies or visibility |
| `SHOW-LIVE-010` | Authorized and anonymous viewers remain open through repeated edits | Context count stays bounded, private HMR remains stable, and no public background build occurs |

Focused unit/contract tests, Ruff, repository CI, and local Incus browser
regression are required. Green unit tests do not replace simultaneous
authorized/anonymous browser verification.

## Acceptance Gate

The design is implemented when all of the following are true:

- Authorized public-link viewers get the same Vite HMR and React Fast Refresh
  behavior as private editors.
- Annotation and public-link HMR consume one validated, resource-aware editor
  capability.
- Every other public viewer receives a readable no-HMR representation with no
  private identifier, diagnostics, or bidirectional Runtime channel.
- `/show/<session>/__vite_hmr` is the only HMR endpoint and independently
  revalidates authorization; `/p/<share>/__vite_hmr` does not exist.
- Anonymous traffic cannot restart, rebase, or close the private Vite context.
- Capability-varying responses cannot be shared across viewers by a cache.
- Authorization failure preserves public availability and fails closed to
  no-HMR mode.
- Existing public source-confinement and sensitive-path protections hold for the
  complete request surface.
- Local Incus measurements show bounded context memory and no material page-load
  regression beyond existing private/public transform costs.
