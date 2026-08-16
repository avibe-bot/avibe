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
| Public `/p/<share>/`, authorized editor navigation | Redirect to canonical `/show/<session>/` | Vite HMR and React Fast Refresh after the redirect |
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
2. Avibe redirects Alice's top-level navigation to the corresponding canonical
   `/show/<session>/` path. That document, every module, and
   `/show/<session>/__vite_hmr` use one private representation; React Fast Refresh
   updates her page and preserves component state.
3. Bob's document uses the isolated public transform context. It contains the
   inert Vite/React Refresh shims and never opens an HMR socket.
4. Bob's loaded page stays on the public representation even if identity state
   changes while its subresources load. A later manual navigation or refresh reads
   the current public content and may then take the editor redirect.

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

The fix is a capability-gated navigation redirect plus separate context
ownership, not a weaker client-side HMR switch or two representations at one URL.

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
unverifiable authorization fails closed to the public no-HMR representation. A
remote identity outage must not make an otherwise public page unavailable.

### URL-selected representation

One browser document uses one representation for its complete lifetime:

- `/p/<share>/...` always serves the public no-HMR representation.
- `/show/<session>/...` always serves the canonical private representation.
- Avibe never returns a private entry document, module, API response, or HMR
  client with a `/p/` document URL.

For a `GET` top-level browser navigation to a current public share, Avibe first
computes `ShowEditorCapability`. When it succeeds and Runtime has explicitly
advertised keyed-context support, Avibe returns a private, no-store redirect to
the equivalent `/show/<session>/<route>` URL, preserving the route suffix and
query. `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document` are the trusted
navigation evidence. Missing or contradictory Fetch Metadata fails closed to the
ordinary public document; it never upgrades a subresource, `fetch()`, worker, or
API request.

No update-mode discriminator replaces the existing `globalThis.__AVIBE_SHOW__`
payload. Public and private documents retain the shipped additive bootstrap keys:
`sessionId`, `basePath`, `eventsPath`, `streamPath`, optional `writeToken`, and
`annotation`. The URL and redirect are the representation boundary, so existing
scaffolds, history routing, annotations, and event streams keep their current
bootstrap contract.

Login or permission changes take effect on the next network navigation. A loaded
public document and all relative `/p/` requests remain public even if identity
resolution later recovers. Losing editor authorization closes an existing private
HMR connection; ordinary private module reads then continue or fail according to
the caller's current Show Page read ACL.

### User-visible requirements

1. An authorized editor opening `/p/` is redirected to the equivalent `/show/`
   route and gets full Vite HMR and React Fast Refresh from the canonical
   development context.
2. An anonymous or unauthorized `/p/` viewer never receives a Vite client,
   React Refresh runtime, HMR socket, private module URL, Session identifier,
   source frame, or development diagnostic.
3. The public link and page content remain readable without login.
4. An unauthorized loaded page does not update automatically. Refreshing it
   reads the current public workspace content.
5. A failed or unavailable identity lookup selects the public representation
   rather than delaying or failing the public page.
6. Private HMR traffic cannot be restarted or rebased by an unauthorized public
   request.
7. Existing public path confinement, sensitive-path denial, and symlink escape
   protections remain in force for every public request.

## Architecture

```text
GET /p/<share>/...
  -> resolve current public share
  -> top-level navigation + Runtime keyed-context capability?
     -> yes: compute ShowEditorCapability
        -> authorized editor: redirect to /show/<session>/...
        -> everyone else: public representation
     -> no: public representation

GET /show/<session>/...
  -> existing private Show read ACL
  -> canonical private Vite context
  -> editor-only HMR socket and revocation
```

### Runtime context ownership

Runtime owns at most two contexts for a public Session:

| Context key | Base | Consumers | Lifetime |
| --- | --- | --- | --- |
| `(session_id, private)` | `/show/<session>/` | Private `/show/` only | Existing private demand/lifecycle |
| `(session_id, public)` | Current `/p/<share>/` | Every `/p/` document and subresource | Demand-created; disposable without affecting private HMR |

The private context is canonical. An authorized public-link navigation redirects
before Runtime returns document bytes, so the resulting request is an ordinary
`/show/` request. It does not send a share-specific `x-vibe-show-base`, rewrite
private modules into the share path, or create a second private HMR protocol.

The public transform context preserves today's no-HMR representation but no
longer owns or replaces the private context. A share rotation may recreate only
the disposable public context. Public context startup, failure, or traffic
cannot close the private socket.

Avibe negotiates that ownership once per Runtime process before enabling the
redirect:

```http
GET /capabilities

200 OK
Content-Type: application/json

{"protocol":1,"features":["show-context-key-v1"]}
```

With `show-context-key-v1`, Avibe supplies the loopback-only
`X-Avibe-Show-Context: private` or `X-Avibe-Show-Context: public` header and
Runtime keys Vite ownership by `(session_id, context)`. Avibe strips any
browser-supplied copy of this header. Runtime health alone, an accepted app
request, a package version, or support for `x-vibe-show-base` is not capability
evidence. If the endpoint, protocol, feature, or response is absent or invalid,
Avibe does not enable the editor redirect and keeps every `/p/` request on the
existing no-HMR compatibility path.

The public context is a read transform surface, not a publication guarantee. It
may read the latest workspace state on navigation, so a fresh request during an
invalid edit can receive the existing sanitized unavailable behavior. This
change does not promise last-known-good artifacts or frontend/API revision
atomicity.

### HTTP routing

For an authorized top-level public entry or SPA navigation, Avibe redirects the
same route suffix and query to `/show/<session>/`. The private route then serves
the existing private bootstrap and canonical private Runtime representation.
There is no public document whose subresources can switch to private mode.

Every non-redirected `/p/` request uses only the public context and existing
public-safe response transforms, regardless of current identity. Exact files and
API handlers retain first refusal; route-shaped document misses retain the current
SPA fallback. The implementation must preserve the existing path policy as an
invariant over the whole public request surface:

- no sensitive segment is readable
- no symlink or `@fs` path escapes the allowed workspace/dependency roots
- no private Session path survives a public response
- no public request is forwarded to an arbitrary host file or plugin endpoint

The public context continues to use inert, versioned `@vite/client` and React
Refresh shims. It exposes neither Runtime diagnostics nor a message channel.

### WebSocket routing

`/show/<session>/__vite_hmr` remains the only HMR endpoint. It requires the
effective editor capability, allowed origin, current remote authorization
revision, and Show Page resource access. Existing authorization and resource
broker tasks close it when those facts stop holding.

That editor requirement applies to HMR, annotation writes, and the public-link
redirect, not to ordinary private document and module reads. `/show/` keeps the
existing resource-reader ACL so a read-only user can load the complete private
module graph without receiving an HMR channel. A downgrade from editor to viewer
closes HMR but does not manufacture a blank page by denying modules the viewer is
still entitled to read.

`/p/<share>/__vite_hmr` is removed for every viewer. A redirected editor uses the
canonical private socket, while every document that remains at `/p/` contains no
live client.

### Cache boundary

The redirect decision varies by capability, so redirect and public entry
responses use `Cache-Control: private, no-store` and `Vary: Cookie`. Successful
content-hashed vendor assets at capability-independent global URLs may retain
immutable caching. Avibe never forwards browser cookies, authorization headers,
CSRF headers, annotation tokens, or context-selection headers to Runtime.

`/p/` never carries private document bytes, which also makes Service Worker
interception representation-safe. A Show-owned worker under `/p/<share>/` may
continue serving that public representation and can therefore delay the editor
redirect until a navigation reaches Avibe, but it cannot cache a private document
at the shared URL or control the disjoint `/show/<session>/` scope. Avibe strips
`Service-Worker-Allowed` from public Runtime responses so authored content cannot
expand a public worker beyond the default `/p/<share>/` scope. Client-side mode
replacement is never a security boundary.

## Performance Model

The model adds authorization routing, not a production publication pipeline:

| Case | Cost |
| --- | --- |
| Authorized editor opens `/p/` | Existing session/resource decision, one redirect, canonical Vite transforms, and one private HMR socket |
| Unprivileged viewer opens `/p/` | Existing public transforms and shims from an isolated public context; no socket |
| Both modes are active | Up to two Vite contexts for the Session; only the private context maintains HMR clients |
| Agent edit | Private context pushes HMR; public context does no work until the next public request or refresh |

The capability decision adds one redirect round trip for an authorized public-link
navigation. A request without a Workbench cookie immediately stays public; a
request with a cookie uses the existing bounded/cached identity resolution already
needed by annotations.

After its one redirect, an authorized `/p/` load should have the same cold and
warm profile as `/show/`.
The isolated public context can increase Runtime memory when authorized and
anonymous viewers are active simultaneously, but avoids production builds,
artifact storage, SSE fanout, and anonymous sockets. Runtime must expose context
count and memory so local Incus regression can set a per-Session idle eviction
budget from measurements rather than an assumed universal threshold.

## Failure And Revocation Behavior

| Failure | Browser behavior | Operator evidence |
| --- | --- | --- |
| Missing/expired/read-only identity | Serve the public no-HMR representation | Redacted authorization reason |
| Identity service unavailable | Serve the public representation; public availability does not depend on identity recovery | Authorization availability metric/log |
| Keyed-context capability absent | Serve every `/p/` request through the existing no-HMR compatibility path; do not redirect | Runtime capability metric/log |
| Private Runtime unavailable after an editor redirect | Existing private sanitized recovery behavior; never fall through to a raw public socket | Private Runtime log |
| Public context unavailable | Existing sanitized public unavailable/static fallback | Public Runtime log without source detail |
| Editor role revoked but read ACL remains | Close private HMR; retain entitled private document/module reads; next `/p/` navigation stays public | Existing authorization-revision/resource-revocation log |
| Show Page read access revoked | Close private HMR and reject later private document/module reads; next `/p/` navigation stays public if the share remains valid | Existing resource-revocation log |
| Share rotates or becomes private/offline | Old public reads fail immediately; private access remains governed independently | Durable Show Page state |

Revocation cannot erase module bytes already downloaded by a previously
authorized editor. Editor-role revocation terminates the bidirectional channel;
Show Page read revocation also prevents future reads. These are the same
unavoidable boundaries as the private `/show/` surface.

## Rejected Alternatives

### Enable `/p/` HMR for every public viewer

Public visibility grants content read access, not access to Vite custom messages,
plugin listeners, local paths, source frames, or Runtime diagnostics. This would
turn a share link into a development-server capability.

### Gate HMR only in the browser

An anonymous viewer can remove a client-side condition or connect directly.
Authorization must select the document representation and independently guard
every private module and socket request on the server.

### Serve the private representation at the public URL

This makes document and subresource capability checks race with one another and
lets HTTP caches or a public-scope Service Worker retain private bytes at `/p/`.
Redirecting the editor to `/show/` makes representation identity part of the URL
and lets the existing private route own the complete document graph.

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

1. Freeze `ShowEditorCapability`, top-level navigation detection, and the
   capability-gated `/p/` to `/show/` redirect as contract fixtures; pin the
   existing Show bootstrap payload as unchanged.
2. Factor public annotation authorization into the shared, resource-aware editor
   capability and use the same result for editor navigation.
3. Add the explicit Runtime capability handshake and representation-key header;
   split canonical private and disposable public context ownership without
   changing the private URL or bootstrap protocol.
4. Redirect authorized top-level `/p/` navigations to the equivalent `/show/`
   route. Never route a `/p/` subresource to the private context.
5. Keep every non-redirected `/p/` request on the public transform/shim path and
   remove the public HMR websocket.
6. Preserve resource-viewer authorization for ordinary private modules while
   requiring editor capability for HMR, annotation writes, and public-link
   redirect selection.
7. Add cache, Service Worker scope, revocation, mixed-version, concurrency, and
   path-confinement contract tests.
8. Run local Incus regression with authorized, read-only, anonymous, and revoked
   viewers open concurrently.

The capability branch can roll out with the bundled Runtime change. Avibe enables
the redirect only after `GET /capabilities` returns protocol `1` and the exact
`show-context-key-v1` feature. Mixed versions, malformed responses, and transient
negotiation failures keep `/p/` on the existing no-HMR behavior for every viewer.
An accepted legacy app request is not treated as negotiation, and the old
anonymous public socket is never a compatibility fallback.

## Verification Plan

### Authorization and document contract tests

- seed every supported identity/resource shape and assert that `canAnnotate` and
  the public-link redirect are produced by the same editor capability
- prove anonymous, read-only, expired, resource-forbidden, and unverifiable
  top-level navigations receive only the public representation
- prove query parameters, headers outside the trusted identity boundary,
  annotation write tokens, subresource requests, and missing Fetch Metadata
  cannot trigger the redirect
- prove the redirect preserves the route suffix and query while both redirect and
  public entry responses are private/no-store and vary on cookies
- prove public and private documents retain every existing Show bootstrap key and
  that public configuration contains no Session identifier or private path

### Runtime and proxy tests

- Runtime capability negotiation is explicit, cached only for one Runtime
  process, and rejects absent, malformed, or legacy responses
- Avibe strips browser context headers; negotiated private and public contexts
  have independent keys and lifecycles
- public requests never change, close, or rebuild the private context
- authorized top-level `/p/` navigations redirect before document bytes to the
  equivalent canonical `/show/` route
- unprivileged documents reference only public paths and immutable shims
- `/p/<share>/__vite_hmr` rejects every connection
- private document/module requests retain resource-reader ACL; private HMR repeats
  editor/resource/origin checks and closes on authorization-revision or resource
  revocation
- public Runtime responses cannot expand a Service Worker beyond the public share
  scope, and a public worker cannot control or cache the `/show/` representation
- every existing public path-confinement fixture remains denied by the new
  routing branch, including sensitive segments and workspace escapes
- direct CLI share rotation or visibility mutation invalidates the old public
  route without touching independently authorized private access

### Browser and regression scenarios

| ID | Scenario | Expected evidence |
| --- | --- | --- |
| `SHOW-LIVE-001` | Authorized editor opens `/p/<share>/` and the Agent edits a component | The navigation redirects to `/show/`; React Fast Refresh preserves component state |
| `SHOW-LIVE-002` | Anonymous viewer opens the same link during the edit | No HMR socket exists and the loaded page stays unchanged until refresh |
| `SHOW-LIVE-003` | Signed-in read-only or resource-forbidden viewer opens the link | Behavior and bytes match anonymous public mode; no private identifier appears |
| `SHOW-LIVE-004` | Direct `/show/`, redirected editor `/show/`, and anonymous `/p/` are open together | Both private documents keep HMR while public traffic uses an independent context |
| `SHOW-LIVE-005` | Identity resolution fails for a request carrying an unverifiable cookie | The public page remains readable with no HMR or diagnostic leak |
| `SHOW-LIVE-006` | Editor role is revoked while redirected HMR is open but read ACL remains | The socket closes, private modules remain readable, and the next `/p/` navigation stays public |
| `SHOW-LIVE-007` | Share rotates or direct CLI changes visibility | The old public URL stops serving while authorized private routing remains independent |
| `SHOW-LIVE-008` | Public requests target every existing denied path shape | All remain denied before Runtime access; no sensitive or escaped file is returned |
| `SHOW-LIVE-009` | A viewer connects directly to `/p/<share>/__vite_hmr` | The connection is rejected regardless of cookies or visibility |
| `SHOW-LIVE-010` | Authorized and anonymous viewers remain open through repeated edits | Context count stays bounded, private HMR remains stable, and no public background build occurs |
| `SHOW-LIVE-011` | A public document starts while identity is unavailable and identity recovers during module loading | Every module stays on `/p/`; no Vite client or mixed transform graph appears |
| `SHOW-LIVE-012` | A read-only user opens `/show/<session>/` | The complete private module graph loads, while the HMR socket is denied |
| `SHOW-LIVE-013` | Public content registers a share-scoped Service Worker, then the editor opens the link | No private bytes are served at `/p/`; any network-reached redirect lands outside the worker scope at `/show/` |
| `SHOW-LIVE-014` | Avibe runs against a Runtime without `show-context-key-v1` | Every `/p/` viewer stays on the compatibility no-HMR path and public traffic cannot be mistaken for negotiated isolation |

Focused unit/contract tests, Ruff, repository CI, and local Incus browser
regression are required. Green unit tests do not replace simultaneous
authorized/anonymous browser verification.

## Acceptance Gate

The design is implemented when all of the following are true:

- Authorized public-link navigations redirect to the corresponding private URL
  and get the same Vite HMR and React Fast Refresh behavior as private editors.
- Annotation writes and the public-link editor redirect consume one validated,
  resource-aware editor capability.
- Every other public viewer receives a readable no-HMR representation with no
  private identifier, diagnostics, or bidirectional Runtime channel.
- `/show/<session>/__vite_hmr` is the only HMR endpoint and independently
  revalidates authorization; `/p/<share>/__vite_hmr` does not exist.
- A loaded `/p/` document cannot switch representation as identity changes, and
  anonymous traffic cannot restart, rebase, or close the private Vite context.
- Capability-varying responses cannot be shared across viewers by a cache.
- Public Service Workers cannot cache private bytes at `/p/` or control `/show/`.
- Ordinary private module reads preserve resource-viewer access while HMR remains
  editor-only.
- Runtime support is accepted only through the explicit keyed-context capability
  handshake; mixed versions fail closed to public no-HMR behavior.
- Authorization failure preserves public availability and fails closed to
  no-HMR mode.
- Existing public source-confinement and sensitive-path protections hold for the
  complete request surface.
- Local Incus measurements show bounded context memory and no material page-load
  regression beyond existing private/public transform costs.
