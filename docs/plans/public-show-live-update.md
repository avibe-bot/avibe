# Unified Show Access and Capability-Gated HMR

Status: Proposed

Date: 2026-08-16

Scope: `avibe` Show access UI, authorization/proxy, `avibe-backend` exact-email
grants, and `vibe-show-runtime`

## Decision

A Show Page has one audience setting and a separate development capability.
Audience decides who may read. `ShowEditorCapability` decides who may use HMR
and Agents annotations. The route and effective server-side capability select
the resulting representation:

| Surface and viewer | Runtime representation | Update behavior |
| --- | --- | --- |
| Private `/show/<session>/`, authorized editor | Canonical private Vite context | Vite HMR and React Fast Refresh |
| Limited or public `/p/<share>/`, authorized editor navigation with a negotiated Runtime | Redirect to canonical `/show/<session>/` | Vite HMR and React Fast Refresh after the redirect |
| Limited or public `/p/<share>/`, authorized editor with a legacy Runtime | Existing shared compatibility context | Agents annotations remain available; no Hot Reload |
| Limited `/p/<share>/`, signed-in exact-email viewer | Negotiated isolated shared context; legacy compatibility context otherwise | No Hot Reload or annotations; refresh reads current content |
| Limited `/p/<share>/`, anonymous or unauthorized viewer | None | Login challenge or access denied; no page bytes |
| Public `/p/<share>/`, anonymous, read-only, expired, resource-forbidden, or unverifiable | Negotiated isolated shared context; legacy compatibility context otherwise | No Hot Reload or annotations; refresh reads current content |
| Offline | None | None |

Limited and public access are read grants, not development authorization. Making
a page shareable therefore does not downgrade its editor's experience, and being
able to read a share link does not grant access to Vite's bidirectional
development protocol or annotation dispatch.

For a concrete example, Alice is an authorized editor, Bob is on the exact-email
list, and Carol is anonymous. They open the same limited `/p/brief/` URL while an
Agent edits the page:

1. Avibe resolves Alice's existing Workbench session and the same editor
   capability used to enable annotations.
2. Avibe redirects Alice's top-level navigation to the corresponding canonical
   `/show/<session>/` path. That document, every module, and
   `/show/<session>/__vite_hmr` use one private representation; React Fast Refresh
   updates her page and preserves component state.
3. Bob signs in, remains on `/p/brief/`, and receives the isolated shared
   transform context. It contains inert Vite/React Refresh shims, exposes no
   annotation capability, and never opens an HMR socket.
4. Carol is asked to sign in and is denied after authentication because she has
   neither the page-bound email grant nor editor capability.
5. If Alice changes the audience to fully public, Carol can read the same shared
   representation without login. Bob still receives no development capability.
6. A loaded `/p/` document stays on the shared representation even if identity
   state changes while its subresources load. A later network navigation may
   take the editor redirect or re-evaluate limited access.

This design intentionally does not add anonymous Live Reload. That requirement
would need an independently published revision model with artifact retention,
URL compatibility, API-version coherence, build sandboxing, and global build
budgets. None of that is necessary to deliver the requested authorized-editor
HMR behavior.

## Why The Current Page Does Not Update

The current shared proxy rewrites `@vite/client` and `@react-refresh` to inert,
immutable shims. This prevents a share-link viewer from opening the existing
Vite socket, but it applies the same behavior to an authenticated editor.
The authorization information already used by the annotation surface is not
used to select the Runtime representation.

There is also a product-model problem. The UI currently exposes workspace ACL,
exact-email grants, and a separate public-link switch as independent controls.
That permits states users cannot explain: a page can have an email list while no
`/p/` route exists, and the public switch can anonymously bypass the list. These
are not independent product concepts; they are three mutually exclusive audience
modes.

There is a third ownership problem. Runtime currently lets a Session's Vite
context depend on the requested external base. Alternating `/show/<session>/`
and `/p/<share>/` requests can therefore rebuild or replace one context as the
base changes. Simply making the public socket conditional would still let an
anonymous request disrupt an authorized editor's HMR connection.

The fix is a capability-gated navigation redirect plus separate context
ownership, not a weaker client-side HMR switch or two representations at one URL.

## Product Contract

### One audience setting

The share control exposes one mutually exclusive select:

| `access_mode` | UI label | Share route | Read rule | Conditional UI |
| --- | --- | --- | --- | --- |
| `private` | Private | Disabled | Existing canonical `/show/` resource access only | No email editor or share-link controls |
| `limited` | Limited access | `/p/<share>/` | Current, page-bound exact-email grant on this shared route, or `ShowEditorCapability` | Exact-email editor and share-link controls |
| `public` | Fully public | `/p/<share>/` | Anyone; editors may redirect to `/show/` | Share-link controls only |

The separate public-link switch is removed. The exact-email editor exists only
while `limited` is selected, requires at least one valid address, and says
explicitly that these users receive view-only access to this Show Page. The copy,
custom-slug, and rotate-link controls appear for both `limited` and `public`.

`offline` is not a fourth audience. Availability is an orthogonal lifecycle
state (`active | offline`) controlled by archive/offline operations. An offline
page serves no route, but an authorized owner may still change its audience,
replace or clear its limited grants, and rotate its future share binding without
reactivating it. Apply and recovery use the same coordinator while route admission
remains disabled. Separating availability from audience stops an offline transition
from silently destroying the page's intended audience and does not force an owner to
publish a page merely to clean up access.

The persisted model is therefore:

```text
ShowAccess {
  availability: active | offline
  access_mode: private | limited | public
  share_id: opaque slug | null
  audience_revision: device-local monotonic integer
  share_admission_gate: open | closed_pending
  grant_revision: backend-issued page-scoped integer
  grant_commitment: backend-issued opaque random value
}
```

Exact emails remain normalized, unique page-bound grants in `avibe-backend`;
they are not copied into local storage or encoded into the slug. `share_id` is a
locator, never a bearer credential. It exists only for `limited` and `public`;
rotating it invalidates the old route but does not change the page audience. The
backend owns one monotonic `grant_revision` per Show Page and advances it exactly
once when a mutation ID changes that page's exact-email set, including a change to
the empty set. A retry or same-set no-op returns the current revision. The existing
Instance-wide authorization revision still invalidates generic sessions, but it is
not the version of any individual page's grant set.

Existing organization/resource ACLs continue to govern canonical `/show/`
collaborator access and who can edit or manage the page. They are not a second
share-audience control and cannot satisfy the exact-email gate for a limited
`/p/` request. Only a resource-aware editor may bypass that viewer gate by taking
the canonical redirect.

The reverse boundary is equally strict: page-email entitlement is an input only
to the limited `/p/` shared-read gate. It is not general Show Page resource
authority and must not make `can_use_show_page()` or any equivalent canonical
resource check succeed. Every `/show/<session>/` document, module, API, and socket
requires the existing owner/organization resource ACL independently of any
`show_page_email` claim. A viewer whose only authority is the limited email list
therefore remains on `/p/` and cannot obtain the private Vite graph by decoding
their signed page ID and constructing a `/show/` URL.

Only the page owner or existing sharing-control authority may change
`access_mode`, the email set, or the slug. A page-bound `show_page_email` viewer
cannot read these settings APIs, mutate the audience, load the Workbench, or
promote itself through a broader Instance endpoint.

Audience changes use one Apply action and one durable fail-closed coordinator
rather than three independent writes. The coordinator is a cross-process write
boundary, not an in-memory mutex. Every current-version entry point that can mutate
`ShowAccess` or coordinator recovery state -- including the CLI, Web API, and
startup/migration reconciliation -- must acquire the same
exclusive advisory lock at a stable file in Avibe's state directory before reading
mutation preconditions. The lock file is never replaced or deleted, so every process
locks the same inode. Store mutation methods
require a held lock lease; callers cannot bypass the coordinator with a direct
`ShowPageStore` write. The lock is acquired before any database transaction, and no
code may wait for it while holding a database or hosted-operation lock.

Every Apply allocates a client mutation ID and holds the process-shared lease from
its first authoritative precondition read through either its terminal local commit
or the durable recording of a fail-closed pending phase. A process crash releases
the OS lock, but it can leave only a prepared non-authoritative hosted operation or
a locally recorded pending phase that the next current-version process reconciles
before admitting limited reads. For a change that
carries exact emails, the device first sends the normalized in-memory target and
expected page-scoped `grant_revision` to a backend `prepare` operation. Preparation
canonicalizes before comparing it with the authoritative current set. If they match,
it returns a terminal `no_change` result containing the existing commitment and
revision; the device creates no pending hosted operation and does not close an
already-open gate. Otherwise preparation stores the target only inside the existing
hosted grant trust boundary, changes no grant authority, and returns an opaque
operation ID plus an opaque target `grant_commitment`. The backend generates a fresh
cryptographically random 256-bit commitment and binds it to that prepared operation;
it is never derived from an email or a client-held key. A
prepared operation has a non-renewable 24-hour lifetime. If this step fails or its
response is lost, no local transition is accepted and the previous audience remains
authoritative; an unreferenced preparation can only expire, never commit itself.

After preparation succeeds, one local transaction persists only the opaque
operation ID, source audience revision, source page grant revision, target mode,
target or preserved share binding, target commitment, and phase. It never persists
the email addresses. Before a transition whose target mode is `limited` can add,
remove, or replace page-email authority, it also sets
`share_admission_gate=closed_pending` in that transaction. While a limited gate is
closed, every `/p/` read and editor redirect is denied. Entry into limited commits
the conservative target mode and preserved `share_id` together with the closed
gate; an edit to an already limited list leaves that mode and binding intact.
Public reads do not depend on this limited-only gate. Canonical `/show/` reads are
always decided by the independent owner/organization resource ACL and never inspect
page-email entitlement.

The device then asks the backend to `commit` the prepared operation. Commit
atomically replaces the exact-email set under the expected page `grant_revision`,
advances that revision once when the canonical set changes, promotes the prepared
commitment for a changed set, and retains an idempotent
mutation result. After a lost response or process restart, the device resumes by
opaque operation ID; a committed operation returns the exact current set/commitment
and page grant revision, while an uncommitted expired operation cannot mutate grants.
The device reopens the share admission gate only after a read-after-write
reconciliation proves that the
backend's committed operation, exact current set/commitment, and returned page grant
revision match the local record and that revision has been persisted locally. The
backend deletes the prepared target copy after device acknowledgement or the
24-hour hard limit; a terminal result retains only non-PII metadata and the opaque
commitment. A terminal same-set response is also the reconciliation proof for a
local mode-only transition into `limited`: the device persists exactly the returned
existing commitment/revision and opens the new limited audience atomically. A later
real change back to a previously used set receives a new random commitment. Consequently,
a copied SQLite database or backup exposes no deterministic verifier for guessed
email addresses.
If the result copy has expired after commit, the authoritative current page-grant
read supplies the same commitment and revision proof. An unavailable, ambiguous,
conflicting, or unpersisted result leaves the durable gate closed. When an edit to
an already limited audience is proven expired and uncommitted, the coordinator may
reopen that unchanged limited binding only after the authoritative page
commitment/revision equal the recorded source. An entry from `private` or `public`
to `limited` never attempts to reconstruct its overwritten source mode or binding:
an expired uncommitted entry remains limited with the gate closed and requires a
new owner Apply. The owner may resubmit the limited target or explicitly choose a
different audience. Empty-set cleanup can retry automatically. A periodic
page-grant revision poll is recovery evidence, never the security boundary.

The coordinator applies that invariant to every transition:

1. Entering `limited` from `public` atomically commits the conservative target mode,
   closes `share_admission_gate`, and preserves the exact `share_id` before any hosted
   commit. It then replaces and reconciles the cloud email set and reopens that same
   limited binding. A crash therefore cannot preserve anonymous access or silently
   rotate the URL.
2. Entering `limited` from `private` follows the same closed-barrier protocol and
   commits the requested or allocated binding with the closed target mode. At least
   one address is required.
3. Editing a live limited list closes the share admission gate before sending the
   replacement. Removed and retained viewers are both denied during ambiguity;
   the reconciled revision reopens the new set together.
4. Leaving `limited` commits `private` or `public` locally with
   `share_admission_gate=open`. Page-email claims are accepted only by `/p/` and
   only when the current durable mode is exactly `limited`, so the local commit
   removes their shared-route authority before best-effort backend cleanup.
   `/show/` never accepted that authority. Failed cleanup is retried and surfaced
   as pending but cannot restore authority or make a committed public page unavailable.
5. Switching `private` and `public` is one local transaction. Link allocation,
   mode, revision, and old-route invalidation commit together.

Every `/p/` request that considers page-email authority requires active availability,
current mode `limited`, the current share binding,
`share_admission_gate=open`, and a signed claim `show_page_grant_revision` equal to
the reconciled local page `grant_revision`. A change to an unrelated Instance ACL
cannot invalidate an unchanged page grant, while a mutation of this page invalidates
every older page-email claim. The UI does not optimistically display a new mode or
set until the coordinator reaches its authoritative terminal state. Reconnects and
double clicks reuse the mutation ID and expected audience revision, so they cannot
resurrect an older audience.

The hosted identity resolver produces `vibe_show_page_id`,
`vibe_show_page_grant_revision`, and `vibe_instance_access_source=show_page_email`
together inside the existing signed session payload; the signature covers all three
fields. Avibe consumes the first two only for that exact page and obtains its local
comparison value only from an authenticated current-grant or committed-operation
response. Browser input cannot supply either revision. The generic Instance
authorization watermark remains a separate signed freshness input and never
substitutes for the page revision.

Limited-link authentication is initiated only after Avibe has resolved the current
`/p/<share>/` binding to a Show Page. The share handler passes that server-owned
page ID and binding to the existing login helper explicitly; the helper must not try
to infer a page ID only from a `/show/` return path, and a browser-supplied `next`
value can select only a safe return location. The signed OAuth state, the
single-use server-side handshake record, and the browser-bound handshake cookie all
carry the resolved page ID, current share binding, and safe `/p/` return target.
The hosted authorization request receives the page ID from that bound handshake.

A current generic login session does not short-circuit this flow when it lacks a
current exact grant for the resolved limited page. Avibe performs one page-specific
reauthorization so the hosted resolver can issue the page-scoped source, page ID,
and grant revision together. On callback Avibe verifies the signed/state-bound page
ID and share binding, requires any returned page-email claim to match that page,
then re-resolves the return `/p/` route and requires it still to map to the same
active limited page. The ordinary shared-read gate performs the final current mode,
gate, email, and grant-revision check before returning bytes. A rotated, rebound,
public, private, offline, unlisted, or stale result receives one generic denial and
cannot install authority for another page. Completion without a matching page grant
sets only a one-shot reauthorization-attempted marker that can suppress another
redirect and produce the generic denial; the marker is never positive authority and
is stripped from the safe return URL afterward. This is one closed-loop auth
contract; it applies equally to the normal cookie path and the existing device-bound
server-side handshake fallback.

### One editor capability

Avibe computes one `ShowEditorCapability` from server-owned facts:

- a validated Workbench identity
- an editor role
- independent editor/resource access to the Show Page, evaluated without any
  page-email entitlement
- a current limited/public share-to-Session binding when the request uses `/p/`
- active availability

The annotation decision and share-link HMR decision consume the same result. The
implementation should factor the existing annotation inputs into one helper
rather than make two similar authorization checks. Exact-email entitlement is
intentionally excluded from this capability even though it can satisfy limited
read access.

This exclusion is evidence-based, not only a role check. The hosted resolver may
preserve a person's broader Instance role when the same identity also has an
exact-email grant. `ShowEditorCapability` must therefore prove page access through
the owner/organization resource policy while ignoring `show_page_id`; it cannot
let the page-bound entitlement satisfy that proof. A real page editor still gets
HMR even when their email also appears in the list, because their independent
editor authority succeeds. A person whose only page authority is the list remains
view-only regardless of the role text in another unrelated grant.

A cookie's presence, a browser-provided flag, the share ID, or a
share-scoped annotation write token is insufficient. The annotation token proves
only one permitted public event write after capability selection; it is never
accepted for module or HMR access.

Missing, expired, read-only, resource-forbidden, ambiguous, or temporarily
unverifiable authorization never selects the private representation. A fully
public page falls back to the shared no-HMR representation. A limited page fails
closed to login/denial and returns no page bytes; an identity outage must never
turn it public. A remote identity outage must not make an otherwise fully public
page unavailable.

### URL-selected representation

One browser document uses one representation for its complete lifetime:

- `/p/<share>/...` always serves the shared no-HMR representation after the
  current audience read gate succeeds.
- `/show/<session>/...` always serves the canonical private representation.
- Avibe never returns a private entry document, module, API response, or HMR
  client with a `/p/` document URL.

For a `GET` top-level browser navigation to a current limited/public share, Avibe first
computes `ShowEditorCapability`. When it succeeds and Runtime has explicitly
advertised keyed-context support, Avibe returns a private, no-store redirect to
the equivalent `/show/<session>/<route>` URL, preserving the route suffix and
query. `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document` are the trusted
navigation evidence. Missing or contradictory Fetch Metadata fails closed to the
ordinary shared document; it never upgrades a subresource, `fetch()`, worker, or
API request.

No update-mode discriminator replaces the existing `globalThis.__AVIBE_SHOW__`
payload. Public and private documents retain the shipped additive bootstrap keys:
`sessionId`, `basePath`, `eventsPath`, `streamPath`, optional `writeToken`, and
`annotation`. The URL and redirect are the representation boundary, so existing
scaffolds, history routing, annotations, and event streams keep their current
bootstrap contract.

Login or permission changes take effect on the next network request. A loaded
shared document and all relative `/p/` requests remain on the shared Runtime
representation even if identity resolution later recovers. Limited subresources
still revalidate the page-bound read grant; revocation prevents future reads but
never upgrades or mixes the document graph. Losing editor authorization closes
an existing private HMR connection; ordinary private module reads then continue
or fail according to the caller's current Show Page read ACL.

### User-visible requirements

1. An authorized editor opening `/p/` is redirected to the equivalent `/show/`
   route and gets full Vite HMR and React Fast Refresh from the canonical
   development context.
2. An anonymous, limited-email, or otherwise read-only `/p/` viewer never
   receives a Vite client,
   React Refresh runtime, HMR socket, private module URL, Session identifier,
   source frame, or development diagnostic.
3. A limited link requires login and an exact current page-email grant. Its viewer
   gets only page read access: no Workbench, Agents annotation, HMR, settings, or
   unrelated Show Page access.
4. A fully public link and page content remain readable without login.
5. A shared loaded page does not update automatically. Refreshing it reads the
   current content after re-evaluating audience access.
6. A failed or unavailable identity lookup selects the shared representation for
   a fully public page but denies a limited page.
7. With a negotiated keyed-context Runtime, private HMR traffic cannot be
   restarted or rebased by an unauthorized shared request.
8. Existing shared-route path confinement, sensitive-path denial, and symlink
   escape protections remain in force for every `/p/` request.

An older Runtime does not gain isolation it cannot represent. Avibe does not
enable the editor redirect for it, and its `/p/` traffic retains the
existing single-context base-switching limitation until the bundled Runtime is
upgraded. Compatibility mode is therefore availability-preserving, not evidence
that requirement 7 has been met.

## Architecture

```text
GET /p/<share>/...
  -> resolve current limited/public share and availability
  -> access_mode == limited?
     -> yes: require share_admission_gate == open, then require current page-bound
             email grant at grant_revision or ShowEditorCapability
     -> no: public read is allowed
  -> top-level navigation + Runtime keyed-context capability?
     -> yes: compute ShowEditorCapability
        -> authorized editor: redirect to /show/<session>/...
        -> every allowed viewer: shared representation
     -> no: shared compatibility representation

GET /show/<session>/...
  -> existing private Show read ACL, evaluated without page-email entitlement
  -> canonical private Vite context
  -> editor-only HMR socket and revocation
```

### Runtime context ownership

A negotiated Runtime owns at most two contexts for a shareable Session:

| Context key | Base | Consumers | Lifetime |
| --- | --- | --- | --- |
| `(session_id, private)` | `/show/<session>/` | Private `/show/` only | Existing private demand/lifecycle |
| `(session_id, shared)` | Current `/p/<share>/` | Every allowed limited/public `/p/` document and subresource | Demand-created; disposable without affecting private HMR |

The private context is canonical. An authorized share-link navigation redirects
before Runtime returns document bytes, so the resulting request is an ordinary
`/show/` request. It does not send a share-specific `x-vibe-show-base`, rewrite
private modules into the share path, or create a second private HMR protocol.

The shared transform context preserves today's no-HMR representation but no
longer owns or replaces the private context. A share rotation may recreate only
the disposable shared context. Shared context startup, failure, or traffic
cannot close the private socket.

Avibe negotiates that ownership once per Runtime process before enabling the
redirect:

```http
GET /capabilities

200 OK
Content-Type: application/json

{"protocol":1,"features":["show-context-key-v1"]}
```

New Avibe supplies one loopback-only protocol envelope on every app-graph request,
including requests made before capability negotiation finishes:

```http
X-Avibe-Show-Protocol: 1
X-Avibe-Show-Context: private | shared
```

A legacy Runtime ignores both unknown headers. A new Runtime treats protocol `1`
as an explicit client declaration and requires a valid context before resolving a
Session or changing Vite ownership. A request with no protocol header is from a
released Avibe client: Runtime ignores any context value, selects the
legacy singleton base-switching path from the existing `x-vibe-show-base`, and does
not claim keyed isolation. An unknown protocol value fails closed. This preserves
old-Avibe/new-Runtime source compatibility without letting a missing header from a
declared new client silently weaken its contract. Avibe strips any
browser-supplied copy of both protocol headers. Runtime health alone, an accepted
app request, a package version, or support for `x-vibe-show-base` is not capability
evidence.

Capability negotiation has three process-scoped outcomes:

| Outcome | Evidence | Cache and request behavior |
| --- | --- | --- |
| `supported` | Well-formed protocol `1` response containing `show-context-key-v1` | Cache for this Runtime process; keyed routing and the editor redirect may run |
| `unsupported` | A definitive legacy response such as `404`, or a well-formed response without the protocol/feature | Cache for this Runtime process; no redirect and `/p/` retains compatibility behavior |
| `transient-unknown` | Connect/timeout failure, `408`, `429`, `5xx`, truncated JSON, or another malformed startup response | Current request stays shared and carries the backward-compatible `shared` context; retry on later requests with jittered exponential backoff capped at 5 seconds |

Only `supported` and definitive `unsupported` are permanent for one Runtime
process. A transient failure records a next-probe time rather than a negative
capability. Stopping or replacing Runtime, or changing its base URL/process
identity, clears every cached outcome and retry deadline.

Context selection is a total Runtime request contract, not a proxy-only header.
Every protocol-`1` operation that can create, read, prewarm, or connect to an app
graph supplies an explicit context: entry and module HTTP requests, the SPA fallback
retry, API handler requests, private HMR proxy setup, startup reconciliation, and
`vibe show update` prewarm. Runtime rejects a missing or invalid context from a
declared protocol-`1` client before resolving a Session or changing Vite ownership;
only a headerless legacy client receives the singleton compatibility path. Shared
prewarm uses `shared`; canonical `/show/` prewarm and HMR use `private`. The
implementation keeps one typed protocol envelope through `ShowRuntimeManager`
rather than allowing individual callers to assemble headers.

The shared context is a read transform surface, not a publication guarantee. It
may read the latest workspace state on navigation, so a fresh request during an
invalid edit can receive the existing sanitized unavailable behavior. This
change does not promise last-known-good artifacts or frontend/API revision
atomicity.

### HTTP routing

For an authorized top-level limited/public entry or SPA navigation, Avibe
redirects the same route suffix and query to `/show/<session>/`. The private route then serves
the existing private bootstrap and canonical private Runtime representation.
There is no shared document whose subresources can switch to private mode.

After the audience gate, every non-redirected `/p/` request uses only the shared
context and existing shared-safe response transforms, regardless of the viewer's
identity class. A legacy Runtime uses the explicitly limited compatibility path
instead.
Exact files and API handlers retain first refusal; route-shaped document misses
retain the current SPA fallback. The implementation must preserve the existing
path policy as an invariant over the whole shared request surface:

- no sensitive segment is readable
- no symlink or `@fs` path escapes the allowed workspace/dependency roots
- no private Session path survives a shared response
- no shared request is forwarded to an arbitrary host file or plugin endpoint

The shared context continues to use inert, versioned `@vite/client` and React
Refresh shims. It exposes neither Runtime diagnostics nor a message channel.

The legacy compatibility path is availability-preserving, not a second
implementation of keyed isolation. It never enables HMR. A viewer with current
`ShowEditorCapability` keeps the existing share-scoped annotation bootstrap and
write token (`canAnnotate=true`); page-email, anonymous, and other read-only viewers
receive `canAnnotate=false`. This capability choice changes only annotation UI and
dispatch, never the Runtime context or module namespace.

Legacy Runtime output is accepted only where the existing shared proxy can prove it
safe without a new Runtime feature. Avibe-authored exact-file and API responses keep
their existing behavior. A successful legacy Runtime response must complete the
existing shared body, URL, and header transforms; if it contains a raw or nested
`/@fs/` reference that requires the new immutable-snapshot registry, transformation
fails closed with a fixed path-free unavailable response. For a Runtime error or
failed transform without valid provenance metadata, Avibe replaces the body and
URL-bearing headers with that same fixed response. It does not guess that an
unclassified error is application-authored. This reduced compatibility surface is
temporary because the capability ships with the bundled Runtime.

Vite's native `/@fs/<absolute-path>` form cannot cross that shared boundary.
When a shared Vite resolve produces an allowed absolute request ID, Runtime retains
its complete transform identity server-side: the resolved pathname, query-qualified
loader mode such as `?raw` or `?worker`, importer identity, and representation type.
It opens the file through a confinement-safe root descriptor: resolution cannot
escape the selected allowed root or traverse a symlink prohibited by the existing
policy, the final descriptor is verified against the allowed-root and
sensitive-segment policy, and bounded bytes are copied into a namespace-owned
immutable snapshot. Runtime compares descriptor metadata before and after the copy
and rejects a file that changes during capture. It then binds the snapshot and
transform identity to a namespace-local virtual module ID; neither that ID nor its
browser handle is derived from or reveals the pathname.

An opaque handle never means "serve the captured source bytes". Registry entries are
typed as either an immutable asset response or an immutable virtual-module request.
Assets may use their captured bytes and verified media type directly. Modules,
stylesheets, and query-qualified loaders run through the same Vite
resolve/load/transform semantics as their original request, but a context-scoped
virtual-module plugin supplies the captured bytes and resolves every discovered
dependency through the same confinement, snapshot, and opaque-handle pipeline. The
query, importer conditions, transformed body, content type, safe response headers,
provenance, and rewritten dependency handles are committed together. A transform
that asks for an uncaptured mutable path, cannot resolve a nested dependency, or
cannot produce a complete browser response fails closed; it cannot fall back to raw
source delivery.

The browser receives only
`/p/<share>/__avibe_asset/<namespace>/<handle>`; Runtime's process-local registry
serves the committed asset or transformed response inside the matching shared
namespace and never reopens the source path for a handle read. Replacing the original
file or any parent with a symlink therefore cannot retarget an existing handle. A
later valid source edit creates a new snapshot and transformed response in a new
graph; a newly unsafe path is rejected before allocation. A browser cannot submit a
raw `/@fs/` path or mint a mapping, handles reveal no path bytes, and share rotation
or Runtime replacement prevents new admission into the old namespace.

Each transformed entry graph owns both a 30-minute idle namespace lease and a
non-renewable two-hour absolute lifetime measured from admission. A successful
document, module, or lazy-asset read may refresh only the idle deadline; anonymous
traffic cannot extend the absolute deadline. The current namespace is protected
from idle reclamation, but not from its absolute deadline or process-wide resource
pressure. Reclamation removes the whole namespace rather than individual handles.
After either deadline a later module or lazy import receives a fixed stale-document
response and the browser must reload; it never receives a handle from another graph.

The Runtime admits namespaces, handles, and snapshot bytes through the same
process-wide weighted budget used for shared Vite contexts, with additional
per-Session limits. Before rejecting admission it reclaims expired namespaces and
then the oldest shared bundle that has no request in flight, even if public reads
kept that bundle recently active. In-flight work is pinned only until its response
finishes. When no shared bundle is reclaimable, new shared admission fails closed
with a sanitized retryable unavailable response; it cannot consume the capacity
reserved for private editor contexts. Snapshot bytes count against the same bounds
and are reclaimed with their namespace. Thus a loaded document normally retains
its complete referenced set during the lease, while recorded old public URLs cannot
pin capacity indefinitely. The same chokepoint covers every emitted URL, including
an absolute path nested inside another Vite URL.

Runtime also labels every loopback response as `application`, `asset`, or
`development-diagnostic` in a stripped response metadata header. Avibe forwards
authored application/API bodies and successful assets, but replaces every
`development-diagnostic` body with a fixed shared error representation while
preserving only the safe status class. Source frames, plugin errors, stack traces,
and local paths remain in a redacted operator log. HTTP error status alone is not
trusted to distinguish an authored API error from a Vite transform failure.
Missing or invalid provenance follows the legacy fail-closed rule above; only an
Avibe-authored response that did not traverse Runtime may bypass Runtime provenance.

The shared proxy also treats response headers as URL output. It always removes
`SourceMap`, `X-SourceMap`, `Content-Location`, and `Refresh`. It parses `Location`
and `Link` only for Runtime-classified application responses, rewrites safe
same-Show targets into the current `/p/` namespace, preserves explicitly external
HTTP(S) application targets, and rejects local-file, private-Session, `@fs`, or
malformed targets. No Runtime URL-bearing header is forwarded by a generic
allowlist. Source-map comments in bodies pass through the same opaque-URL
rewriter or are stripped.

### WebSocket routing

`/show/<session>/__vite_hmr` remains the only HMR endpoint. It requires the
effective editor capability, allowed origin, current remote authorization
revision, and Show Page resource access. Existing authorization and resource
broker tasks close it when those facts stop holding. In-memory broker events are
an optimization, not the durable visibility boundary: while any HMR socket is
open, one coalesced monitor per Session rereads `ShowPageStore` on a cadence no
greater than five seconds and closes every socket if the page becomes `offline`.
A direct CLI mutation therefore takes effect without an IPC event. Changing a
limited/public page to private or rotating its share does not close the canonical
private socket when editor and read access remain valid; the share binding is needed only for the
redirect that selected `/show/`.

That editor requirement applies to HMR, annotation writes, and the share-link
redirect, not to ordinary private document and module reads. `/show/` keeps the
existing owner/organization resource-reader ACL so a read-only collaborator can
load the complete private module graph without receiving an HMR channel. This ACL
has no page-email branch: a `show_page_email` claim never authorizes `/show/`, even
while the page is limited and its shared gate is open. A caller who independently
holds resource-reader authority may use `/show/`; that decision must be recomputed
without the page-email entitlement. A downgrade from editor to such an independently
entitled resource viewer closes HMR but does not manufacture a blank page by denying
modules that viewer is still entitled to read.

`/p/<share>/__vite_hmr` is removed for every viewer. A redirected editor uses the
canonical private socket, while every document that remains at `/p/` contains no
live client.

### Cache boundary

The redirect and limited-access decisions vary by capability, so redirects and
all entry responses use `Cache-Control: private, no-store` and `Vary: Cookie`.
Every limited response, including modules and API handlers, is private/no-store.
Successful
content-hashed vendor assets at capability-independent global URLs may retain
immutable caching. Avibe never forwards browser cookies, authorization headers,
CSRF headers, annotation tokens, or context-selection headers to Runtime.

`/p/` never carries private document bytes, which also makes Service Worker
interception representation-safe. A Show-owned worker under `/p/<share>/` may
continue serving that shared representation and can therefore delay the editor
redirect until a navigation reaches Avibe, but it cannot cache a private document
at the shared URL or control the disjoint `/show/<session>/` scope. Avibe strips
`Service-Worker-Allowed` from shared Runtime responses so authored content cannot
expand a worker beyond the default `/p/<share>/` scope. Client-side mode
replacement is never a security boundary.

A Service Worker or Cache Storage entry already installed in a browser is content
that browser has downloaded; the server cannot erase it. Access-mode changes and
share rotation therefore revoke every request that reaches Avibe immediately,
but cannot promise that an old worker will stop rendering cached shared bytes in
that browser. No private representation is ever cacheable at `/p/`, so this
limitation cannot expose HMR, annotations, Session identifiers, or newer content.
Verification distinguishes network-reached revocation from unavoidable local
cache retention instead of claiming that `ShowPageStore` can control the latter.

## Performance Model

The model adds authorization routing, not a production publication pipeline:

| Case | Cost |
| --- | --- |
| Authorized editor opens `/p/` | Existing session/resource decision, one redirect, canonical Vite transforms, and one private HMR socket |
| Limited exact-email viewer opens `/p/` | One current authorization decision plus shared transforms and shims; no socket or annotation bootstrap |
| Public unprivileged viewer opens `/p/` | Existing shared transforms and shims from an isolated context when negotiated, or the explicit legacy compatibility path; no socket |
| Both modes are active | Up to two Vite contexts for the Session; only the private context maintains HMR clients, and both count against the Runtime-wide budget |
| Agent edit | Private context pushes HMR; shared context does no work until the next allowed `/p/` request or refresh |
| Active private HMR sockets | One coalesced durable availability read per Session on a cadence no greater than five seconds |

The capability decision adds one redirect round trip for an authorized share-link
navigation. A public request without a Workbench cookie immediately stays shared.
A limited request without a current page grant enters the page-bound login flow;
this includes a caller with an otherwise current generic session. The flow reuses
the existing bounded/cached identity resolution and authorization revision, but its
signed handshake is explicitly bound to the server-resolved Show Page and share.

After its one redirect, an authorized `/p/` load should have the same cold and
warm profile as `/show/`.
The negotiated isolated shared context can increase Runtime memory when authorized and
share-link viewers are active simultaneously, but avoids production builds,
artifact storage, SSE fanout, and anonymous sockets. Runtime owns one process-wide
weighted admission controller over private/shared Vite contexts, opaque namespaces,
handles, and snapshot bytes. It enforces hard context-count and memory-cost limits
plus per-Session limits. Finite conservative defaults and a nonzero private-editor
reserve ship with the feature; configuration can tune them only to another finite
value, with increases justified by local Incus measurements. The reserved slice
cannot be consumed by shared traffic; private admission may reclaim any shared
bundle with no request in flight. Shared bundles are oldest-admitted reclaimable
between requests and have the same non-renewable two-hour maximum lifetime as their
namespaces, so a set of active anonymous Sessions cannot pin them forever. If all
eligible capacity is in flight, shared requests receive a bounded sanitized
overload response rather than growing the sidecar. Runtime exposes
admitted/reclaimed/rejected counts and measured memory for release tuning.
Capability probes are process-cached or backoff-limited rather than added to every
request.

## Failure And Revocation Behavior

| Failure | Browser behavior | Operator evidence |
| --- | --- | --- |
| Missing/expired/read-only identity on fully public page | Serve the shared no-HMR representation | Redacted authorization reason |
| Missing/expired identity on limited page | Redirect to login for a top-level navigation; otherwise deny without page bytes | Redacted authorization reason |
| Signed-in user lacks exact limited grant | Return one generic access-denied response; do not reveal whether the address or page exists | Redacted page-bound denial |
| Identity service unavailable | Fully public stays readable; limited fails closed and remains retryable | Authorization availability metric/log |
| Definitive keyed-context capability absence | Do not redirect; serve `/p/` through the reduced legacy no-HMR path; preserve annotations only for a proven editor | Runtime capability metric/log |
| Transient capability negotiation failure | Send the backward-compatible explicit `shared` context, keep the current allowed request shared, and retry after bounded backoff | Redacted probe state and retry metric |
| Private Runtime unavailable after an editor redirect | Existing private sanitized recovery behavior; never fall through to a raw shared-route socket | Private Runtime log |
| Shared context unavailable | Existing sanitized shared unavailable/static fallback | Shared Runtime log without source detail |
| Shared Runtime admission is saturated | Reclaim the oldest non-in-flight shared bundle; if none is reclaimable, return a sanitized retryable unavailable response without consuming the private reserve | Global weighted-budget admission/reclamation metric |
| A shared document outlives its absolute namespace/context lifetime | Return a fixed stale-document response for later module or lazy-asset reads and require reload | Namespace hard-expiry metric without source paths |
| Runtime emits a development diagnostic or unsafe URL header | Preserve only a safe status class/body and filtered headers | Redacted operator-only diagnostic |
| Legacy Runtime omits provenance or emits a graph requiring raw `@fs` paths | Preserve only a fully transformed safe success response; otherwise return a fixed path-free unavailable response | Redacted compatibility refusal reason |
| Limited-list replacement has an ambiguous result | Keep the durable share admission gate closed across retries and restarts until read-after-write reconciliation persists the exact mutation result | Mutation ID, opaque target commitment, and redacted reconciliation state |
| Prepared email operation expires before commit | Reopen an already-limited unchanged audience only after authoritative proof that no commit occurred; an entry from private/public stays limited-and-closed and requires a new owner Apply | Operation expiry, source grant proof, and opaque target commitment |
| Limited email removed | Reject the viewer's next network request by page grant revision; no HMR or annotation channel exists to close | Page-grant revision log |
| Unrelated Instance authorization changes | Refresh generic session freshness without changing any page grant revision or denying an unchanged limited viewer | Instance and page revision metrics kept distinct |
| Limited page becomes private/public while hosted cleanup fails | Reject page-email claims on `/p/` from the local mode commit; `/show/` remains governed only by its independent resource ACL; keep public reads open and retry cleanup without reopening email authority | Durable audience state and cleanup-pending record |
| Audience changes while the page is offline | Keep every route disabled; run the normal coordinator and persist the future audience without temporary publication | Durable audience state and coordinator evidence |
| Editor role revoked but read ACL remains | Close private HMR; retain entitled private document/module reads; next `/p/` navigation stays shared only if its audience read rule succeeds | Existing authorization-revision/resource-revocation log |
| Show Page read access revoked | Close private HMR and reject later private document/module reads; `/p/` is re-evaluated independently | Existing resource-revocation log |
| Share rotates or becomes private | Old network-reached `/p/` reads fail immediately; an entitled canonical private socket remains independent; previously cached bytes remain a client-cache limitation | Durable Show Page state |
| Show Page becomes offline through any process | The coalesced durable-state monitor closes private HMR within five seconds and later reads return offline | Durable Show Page state |

Revocation cannot erase module bytes already downloaded by a previously
authorized editor. Editor-role revocation terminates the bidirectional channel;
Show Page read revocation also prevents future reads. These are the same
unavoidable boundaries as the private `/show/` surface.

## Rejected Alternatives

### Enable `/p/` HMR for every share-link viewer

Limited/public audience grants content read access, not access to Vite custom messages,
plugin listeners, local paths, source frames, or Runtime diagnostics. This would
turn a share link into a development-server capability.

### Gate HMR only in the browser

An anonymous viewer can remove a client-side condition or connect directly.
Authorization must select the document representation and independently guard
every private module and socket request on the server.

### Serve the private representation at the share URL

This makes document and subresource capability checks race with one another and
lets HTTP caches or a share-scoped Service Worker retain private bytes at `/p/`.
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
restart the editor's development context. Separate private/shared ownership is
the minimum isolation needed for reliable HMR.

### Keep email grants beside a separate public switch

This reproduces the current invalid combinations: grants can exist without a
share route, and anonymous publication can bypass the list. Email grants and
anonymous publication are mutually exclusive audience modes, not independent
features.

### Grant limited viewers ordinary Instance viewer access

That would expose Workbench and every resource permitted to an Instance viewer.
The existing `show_page_email` claim is deliberately frozen to one Show Page and
must remain view-only even when the same person has other unrelated access.

### Add anonymous artifact Live Reload

An immutable artifact stream sounds safer than public HMR, but a complete model
must also sandbox build inputs, retain every loaded document's files, preserve
runtime-resolved asset URLs, coordinate live API handlers with frontend
revisions, and bound builds across Sessions. It is a separate publication
feature, not a prerequisite for capability-gated editor HMR.

## Delivery Sequence

1. Freeze `ShowAccess`, the unified access mutation payload, migration fixtures,
   and the read/editor capability matrix as contract files shared by the UI,
   local server, and hosted backend.
2. Replace workspace audience plus public-link switch with the three-option
   select. Render exact emails only for `limited`, and link controls only for
   `limited | public`; keep availability separate.
3. Implement backend prepare/commit email operations, page-scoped grant revisions,
   opaque grant commitments, the non-PII persistent share admission barrier, the
   process-shared audience write lease, idempotent no-op and
   mutation-result/current-grant lookup, and the fail-closed access coordinator.
   Retire the independent visibility and email mutation paths after callers migrate.
4. Make `/p/<share>/` resolve both `limited` and `public`. Add the server-resolved,
   page-bound limited-login handshake, page-specific reauthorization for a generic
   session, mode-scoped exact page-entitlement check on `/p/`, generic denial,
   page-grant revision check, and private/no-store response policy. Remove
   page-email bypass from canonical Show resource authorization so `/show/` accepts
   only its independent owner/organization ACL.
5. Factor annotation authorization into the shared, resource-aware
   `ShowEditorCapability`, explicitly excluding page-email entitlement, and use
   the same result for top-level editor navigation.
6. Add the explicit Runtime capability state machine and one typed protocol
   envelope; split canonical `private` and disposable `shared` context ownership
   without changing the private URL or bootstrap protocol. Migrate every HTTP,
   WebSocket, fallback, and prewarm caller in the same contract change, send the
   protocol/context headers before negotiation completes, and retain the headerless
   singleton path only for released clients.
7. Redirect authorized top-level `/p/` navigations to the equivalent `/show/`
   route. Never route a `/p/` subresource to the private context. Keep every
   remaining `/p/` request on the shared transform/shim path and remove the
   shared-route HMR websocket.
8. Preserve resource-viewer authorization for ordinary private modules while
   requiring editor capability for HMR, annotation writes, and share-link
   redirect selection.
9. Add leased opaque shared file namespaces backed by immutable bounded snapshots,
   the process-wide weighted Runtime admission controller with a private reserve,
   Runtime response provenance, and URL-bearing header filtering so raw `/@fs/`
   paths, mutable-path handles, anonymous resource pinning, and development
   diagnostics cannot cross the shared boundary.
10. Add the coalesced durable availability monitor for private HMR sockets.
11. Add cache, Service Worker scope, network-revocation, mixed-version,
    negotiation retry, total context propagation, concurrency, one-way migration,
    path-swap confinement, and audience-transition contract tests.
12. Run local Incus regression with editor, exact-email, anonymous, denied, and
    revoked viewers open concurrently.

The capability branch rolls out with the bundled Runtime change. Avibe enables
the redirect only after `GET /capabilities` returns protocol `1` and the exact
`show-context-key-v1` feature. A definitive legacy Runtime keeps `/p/` on the
existing no-HMR behavior and its existing single-context limitation; it is never
described as isolated. A transient or malformed startup response fails the current
request closed to shared mode, carries the explicit protocol/context envelope,
and remains retryable. A new Runtime receiving no protocol header preserves the
released Avibe singleton behavior; that headerless request is compatibility, not
negotiation or keyed isolation. An accepted legacy app request is not negotiation,
and the old anonymous shared-route socket is never a compatibility fallback.

### Data migration

The schema migration separates availability from audience and is fail-closed:

| Existing state | Migrated availability | Migrated access mode | Share ID |
| --- | --- | --- | --- |
| `visibility=public` | `active` | `public` | Preserve current ID |
| `visibility=private` with no email grants | `active` | `private` | Clear any dormant ID |
| `visibility=private` with a non-empty exact-email set | `active` after connected reconciliation | `limited` | Reuse a valid dormant ID or allocate one |
| `visibility=offline` | `offline` | `private` | Clear; the old model did not retain a trustworthy prior audience |

Until the device can read the hosted grant set, an old private page stays private
and records migration pending with its source `audience_revision`; it is never
guessed to be limited. The hosted read returns the page-scoped grant revision and
commitment, and reconciliation may commit only with a compare-and-swap proving that
the source revision is still current. The Instance-wide authorization revision is
deliberately absent from this page-state comparison.

Every authoritative audience Apply increments `audience_revision` and deletes any
pending migration in the same local transaction before remote work begins. A stale
worker discards its hosted read and any provisional share allocation when the
compare-and-swap fails; it cannot convert a newer public or private choice to
`limited`. An old public page remains public, and any independently stored email set
is cleared after the local public state is authoritative. Organization resource
policies are not mapped into external audience modes; they remain canonical
`/show/` collaborator policy.

This is an explicit one-way product migration. Avibe's supported updater advances
to newer releases; it does not offer application or schema downgrade. Once the new
access schema is committed, manually installing an older binary against that state
is unsupported and the older binary must not be started. Backup restore remains a
whole-state operation to a release that understands the restored schema, not a
cross-version audience-write protocol. This boundary removes the need for legacy
audience projections, write journals, hosted write fences, and re-upgrade conflict
resolution, none of which correspond to a supported user workflow. The migration
itself remains restartable and idempotent: an interrupted current-version process
resumes from the pending record under the shared writer lease before admitting a
limited route.

## Verification Plan

### Authorization and document contract tests

- prove the settings API and UI expose exactly `private | limited | public`, the
  old public switch is absent, emails render only for `limited`, and `/p/` link
  controls render for `limited | public`
- exercise every audience transition, coordinator failure point, stale revision,
  retry, and double submission; the share admission gate is the only transient
  route barrier, and no intermediate state may grant more read access than the last
  committed or requested target
- enumerate every current-version CLI, Web, migration, and recovery audience
  mutation entry point and prove its store write requires the same process-shared
  lease; race real CLI and Web writer processes in both acquisition orders and prove
  stale preconditions cannot commit
- copy local state containing one-address and few-address limited sets and prove the
  opaque grant commitments cannot validate guessed emails offline; same-set retry
  keeps its revision/commitment while every real set change receives a fresh random
  commitment
- seed every supported identity/resource shape and assert that `canAnnotate` and
  the share-link redirect are produced by the same editor capability
- prove page-email entitlement can satisfy only a limited `/p/` shared read, cannot
  satisfy canonical Show resource access or the editor's resource proof, and cannot
  access `/show/`, settings, Workbench, Agents, annotations, HMR, or any other Show
  Page; an independently authorized resource reader still retains `/show/` access
- prove a real independent page editor still redirects when the same email is
  also listed
- prove anonymous, expired, resource-forbidden, and unverifiable public
  navigations receive only the shared representation, while equivalent limited
  requests enter login or receive a generic denial with no page bytes
- drive the real limited-link login from initial `/p/` resolution through OAuth
  callback and the final shared read: signed state, cookie, and server-side fallback
  bind the same server-resolved page/share; a listed viewer receives the current
  page claim, an existing generic session is reauthorized, and rebound or unlisted
  results receive only the generic denial; extend `AUTH-SETUP-401` in the auth/setup
  catalog and its Show Page email harness to cover this closed loop rather than only
  asserting a browser redirect
- prove query parameters, headers outside the trusted identity boundary,
  annotation write tokens, subresource requests, and missing Fetch Metadata
  cannot trigger the redirect
- prove the redirect preserves the route suffix and query while both redirect and
  shared entry responses are private/no-store and vary on cookies
- prove shared and private documents retain every existing Show bootstrap key;
  page-email and public viewers receive `canAnnotate=false`, a compatibility-path
  editor receives `canAnnotate=true` without HMR, and no shared bootstrap contains a
  Session identifier or private path
- prove migration maps old public, private-with-grants, private-without-grants,
  and offline rows exactly as specified, with disconnected reconciliation staying
  private
- interrupt and restart the one-way migration at every local and hosted boundary;
  only the current schema serves afterward, the migration is idempotent, and an
  older executable is never started against migrated state
- leave an upgrade migration pending, perform a newer audience Apply, then restore
  connectivity; the source-revision compare-and-swap discards the stale migration
  and cannot allocate or reopen a limited audience
- submit a canonical same-set grant target from every audience source mode; prepare
  returns the current commitment/revision, creates no hosted operation, and either
  leaves an open limited gate untouched or atomically completes a mode-only entry
  into limited
- leave limited for public while empty-set cleanup is unavailable; anonymous reads
  remain available, stale page-email claims remain rejected, and cleanup later
  converges without changing the public binding
- change, clear, and rotate an offline page's future audience while hosted service is
  available, unavailable, and recovering; no route becomes reachable until an
  explicit activation
- interrupt every limited-list mutation before backend prepare, after prepare but
  before the local transaction, after the local transaction but before commit, after
  backend commit, after a lost response, and before local revision persistence; the
  local record never contains an email, a preparation never changes authority by
  itself, and the share admission gate stays closed until the exact result reconciles
- expire prepared and committed-operation result copies, then recover from opaque
  operation ID, target commitment, and the authoritative page grant revision;
  reopen only an unchanged already-limited audience after proof that the operation
  did not commit, while an entry from private/public remains limited-and-closed
  until a new owner Apply, without persisting removed/failed email PII
- advance the Instance-wide authorization revision for every unrelated ACL shape and
  prove an unchanged limited page remains readable, then advance that page's grant
  revision and prove every older page-email claim is rejected
- crash a public-to-limited transition at every local and hosted boundary; `/p/`
  remains closed during ambiguity and completion preserves the exact original custom
  or generated `share_id`
- prove a page-email-only cookie is rejected by `/show/` in every audience mode and
  by `/p/` immediately after the local mode leaves `limited`, even while backend
  cleanup is unavailable

### Runtime and proxy tests

- Runtime capability negotiation distinguishes supported, definitive legacy, and
  transient-unknown outcomes; transient failures retry and every outcome resets
  with the Runtime process
- Avibe strips browser protocol/context headers; negotiated private and shared
  contexts have independent keys and lifecycles
- every new-client app-graph call, including SPA fallback, WebSocket setup, startup
  reconciliation, and shared/private prewarm, carries the selected typed protocol
  envelope; Runtime rejects missing/invalid context only when protocol `1` is
  declared
- a transient capability probe still sends protocol `1` plus `shared`, works with
  the new Runtime, and remains accepted by a legacy Runtime that ignores the headers
- a released Avibe client sends neither header to a new Runtime and retains the
  singleton base-switching path; it is never rejected or reported as keyed isolation
- shared requests never change, close, or rebuild the private context
- authorized top-level `/p/` navigations redirect before document bytes to the
  equivalent canonical `/show/` route
- unprivileged documents reference only shared paths, opaque immutable-snapshot
  handles, and immutable shims; raw `/@fs/`, host paths, and Session paths are
  denied
- absolute TSX, CSS, `?raw`, and `?worker` dependencies are captured into virtual
  modules and retain their original Vite transform semantics; recursively emitted
  imports receive handles from the same namespace, and no handle returns raw
  non-browser source as a successful response
- Runtime response provenance preserves authored application errors while every
  development diagnostic receives a fixed shared body; unsafe `SourceMap`,
  `X-SourceMap`, `Content-Location`, `Refresh`, `Location`, and `Link` values are
  stripped or safely rewritten
- a legacy Runtime with no provenance may return only a fully transformed successful
  shared response; an error, failed transform, or raw/nested `@fs` graph receives a
  fixed path-free unavailable response, while a proven editor still retains
  annotations without HMR
- old opaque namespaces survive lazy loads while leased, expire as a whole after
  30 idle minutes or the non-renewable two-hour lifetime, never cross graph/share
  boundaries, and cannot be kept admitted by anonymous reads
- active public traffic across more Sessions than the process-wide context and
  memory budget remains bounded, reclaims only non-in-flight shared bundles, preserves
  the private-editor reserve, and returns sanitized overload responses when nothing
  is reclaimable
- after handle allocation, replace the source or any parent with an out-of-root or
  sensitive symlink; an asset handle serves only its captured bytes, a module handle
  serves only the response transformed from its captured virtual graph, a new
  allocation is denied, and no handle read reopens the pathname
- `/p/<share>/__vite_hmr` rejects every connection
- private document/module requests retain resource-reader ACL; private HMR repeats
  editor/resource/origin checks, closes on authorization-revision or resource
  revocation, and polls durable availability once per active Session so direct CLI
  offline changes close sockets within five seconds
- shared Runtime responses cannot expand a Service Worker beyond the share scope,
  and a shared worker cannot control or cache the `/show/` representation
- network-reached access-mode changes and rotations revoke the old route; a
  separately asserted client-cache case documents that an already installed
  worker may render only previously downloaded shared bytes
- every existing shared-route path-confinement fixture remains denied by the new
  routing branch, including sensitive segments and workspace escapes
- direct CLI share rotation or access mutation invalidates the old shared
  route without touching independently authorized private access

### Browser and regression scenarios

| ID | Scenario | Expected evidence |
| --- | --- | --- |
| `SHOW-LIVE-001` | Authorized editor opens `/p/<share>/` and the Agent edits a component | The navigation redirects to `/show/`; React Fast Refresh preserves component state |
| `SHOW-LIVE-002` | Exact-email viewer opens a limited link and the Agent edits | The viewer stays on `/p/`, has no annotation UI or HMR socket, and sees new content only after refresh |
| `SHOW-LIVE-003` | Anonymous viewer opens the limited link, signs in with an unlisted email, then retries | The handshake stays bound to the server-resolved page and returns to the same `/p/` route, which gives one generic denial with no page bytes or login loop |
| `SHOW-LIVE-004` | The same link changes from limited to fully public | Anonymous refresh becomes readable; listed viewers still receive no HMR or annotations |
| `SHOW-LIVE-005` | Direct `/show/`, redirected editor `/show/`, exact-email `/p/`, and anonymous public `/p/` are open together | Both private documents keep HMR while all `/p/` traffic uses an independent shared context |
| `SHOW-LIVE-006` | Identity resolution fails for public and limited requests | Public remains readable with no HMR; limited fails closed without leaking its audience |
| `SHOW-LIVE-007` | Editor role is revoked while redirected HMR is open but read ACL remains | The socket closes, private modules remain readable, and `/p/` follows only the current audience read rule |
| `SHOW-LIVE-008` | An email is removed while its limited page is open | The next network read is denied through the page grant revision; no development channel ever existed |
| `SHOW-LIVE-009` | A viewer connects directly to `/p/<share>/__vite_hmr` | The connection is rejected regardless of cookies or visibility |
| `SHOW-LIVE-010` | Editor and shared viewers remain open through repeated edits | Context and leased-namespace counts stay bounded, private HMR remains stable, and no shared background build occurs |
| `SHOW-LIVE-011` | A shared document starts while identity is unavailable and identity recovers during module loading | Every module stays on `/p/`; no Vite client or mixed transform graph appears |
| `SHOW-LIVE-012` | A read-only user with independent Show resource ACL opens `/show/<session>/` | The complete private module graph loads, while the HMR socket is denied |
| `SHOW-LIVE-013` | Shared content registers a share-scoped Service Worker, then the editor opens the link | No private bytes are served at `/p/`; any network-reached redirect lands outside the worker scope at `/show/`, and cached old bytes are recorded as a client-cache limitation |
| `SHOW-LIVE-014` | Avibe runs against a Runtime without `show-context-key-v1` | Every `/p/` viewer stays on the compatibility no-HMR path; the existing single-context limitation is explicit and cannot be mistaken for negotiated isolation |
| `SHOW-LIVE-015` | The first capability probe times out while the new Runtime app endpoint is ready | The request carries `shared` and succeeds, a later bounded retry detects support, and the next eligible navigation redirects without restart |
| `SHOW-LIVE-016` | Shared transforms emit nested `/@fs/` TSX, CSS, `?raw`, and `?worker` imports, URL-bearing headers, and Vite errors | Each handle serves a complete browser response transformed from the immutable virtual graph with recursively rewritten same-namespace dependencies; raw source, paths, and development errors never reach the browser |
| `SHOW-LIVE-017` | Startup reconciliation and `vibe show update` prewarm shared and private graphs | Each request carries its typed context; a missing/invalid context fails before creating or rebasing either graph |
| `SHOW-LIVE-018` | Direct CLI changes an active page to offline with private HMR connected | The coalesced durable monitor closes every socket for the Session within five seconds without relying on an in-process event |
| `SHOW-LIVE-019` | Public changes to limited and the cloud write fails | The target limited mode and original binding remain durable with the gate closed; neither anonymous nor stale listed viewers can read until reconciliation succeeds |
| `SHOW-LIVE-020` | Old private/public/offline rows and exact-email grants upgrade | Each page matches the migration table; a disconnected private-with-grants page stays private and pending |
| `SHOW-LIVE-021` | A live limited-list replacement commits remotely, its response is lost, and Avibe restarts | All page-email reads remain closed until the mutation result and revision reconcile; removed viewers never regain access |
| `SHOW-LIVE-022` | Limited changes to private while hosted cleanup is unavailable | A stale page-email cookie is rejected on `/p/`; `/show/` continues to reject page-email-only authority while independent resource collaborators retain canonical access |
| `SHOW-LIVE-023` | A legacy Runtime returns an unclassified transform error and a nested raw `/@fs/` import | Both responses fail closed with fixed path-free output; no host path or diagnostic reaches the browser |
| `SHOW-LIVE-024` | An allowed source is replaced by a sensitive symlink after an opaque handle is issued | The issued handle returns only immutable captured bytes, and the changed path cannot receive a new handle |
| `SHOW-LIVE-025` | A disconnected private-page migration is pending, then the owner chooses public and later private before reconnecting | The newer revision cancels migration; reconnect cannot allocate a slug, commit limited, or restore the old email audience |
| `SHOW-LIVE-026` | Applying a canonically identical limited email set | Backend returns the current commitment/revision, creates no pending operation, and the open gate never closes |
| `SHOW-LIVE-027` | An anonymous client reads every superseded namespace just before each idle deadline | Each namespace still reaches its non-renewable hard expiry, releases all snapshot bytes, and requires stale documents to reload |
| `SHOW-LIVE-028` | Anonymous clients keep shared pages active across more Sessions than the Runtime-wide budget | Context and memory stay under the hard limit, reclaimable shared bundles rotate, private HMR keeps its reserve, and excess shared admission is sanitized |
| `SHOW-LIVE-029` | Limited changes to public while hosted empty-set cleanup remains unavailable | Anonymous reads start from the committed public binding, stale listed claims stay rejected, and cleanup retries do not close the public route |
| `SHOW-LIVE-030` | A released Avibe client starts a new Runtime and sends only `x-vibe-show-base` | Entry, module, and prewarm requests retain the legacy singleton behavior; keyed isolation remains disabled until a protocol-`1` client supplies context |
| `SHOW-LIVE-031` | An already-limited list Apply crashes at every prepare/local/commit boundary and later exceeds operation retention | No local row contains an email, prepare alone never changes grants, committed state reconciles by page commitment/revision, and the unchanged limited audience reopens only after proof that an expired operation did not commit |
| `SHOW-LIVE-032` | An offline page changes from limited to private, then prepares a future public custom slug | No route is admitted while offline; grant cleanup and the future audience converge without temporary activation |
| `SHOW-LIVE-033` | An unrelated Instance ACL change advances the global authorization revision while a limited list is unchanged | Refreshed listed viewers remain readable at the same page grant revision; changing that page's list advances only its grant revision and rejects older claims |
| `SHOW-LIVE-034` | A custom-slug public page crashes at every boundary while changing to limited | Every ambiguous `/p/` read is denied, successful recovery preserves the exact slug, and a proven uncommitted expiry stays limited-and-closed until a new owner Apply |
| `SHOW-LIVE-035` | An authorized editor uses `/p/` with a definitive legacy Runtime | The editor remains on the shared compatibility representation with annotations enabled, no HMR socket, and no private module namespace |
| `SHOW-LIVE-036` | An attacker obtains local state for a limited page with a small email set | The stored random commitment provides no offline email-guess verifier; only the hosted trust boundary can bind it to the canonical set |
| `SHOW-LIVE-037` | A listed viewer with only a generic session opens a limited `/p/` link and completes login | Avibe reauthorizes against the server-resolved page, validates the same page/share on callback, and serves only the shared representation with the current page grant |
| `SHOW-LIVE-038` | A page-email-only viewer copies its signed page ID and requests `/show/<session>/` while the page remains limited | Canonical document, module, API, and HMR requests are denied; the claim remains usable only through the current limited `/p/` gate |

Focused unit/contract tests, Ruff, repository CI, and local Incus browser
regression are required. Green unit tests do not replace simultaneous editor,
exact-email, denied, anonymous, and revoked browser verification.

## Acceptance Gate

The design is implemented when all of the following are true:

- The settings surface has one three-option audience select, no public-link
  switch, conditional email/link controls, and one authoritative Apply action.
- `private`, `limited`, `public`, and orthogonal offline availability persist and
  migrate according to one documented model.
- Both limited and public sharing use `/p/<share>/`; limited requires a current
  exact page grant and fully public remains anonymously readable.
- Page-email authority is view-only and cannot contribute evidence to
  `ShowEditorCapability`, settings, Workbench, Agents annotations, HMR, or another
  Show Page. It authorizes only `/p/` shared reads while current mode is `limited`,
  the durable share admission gate is open, the share still resolves to that page,
  and its signed page grant revision is current. It never contributes to canonical
  `/show/` resource access; the Instance-wide revision is not used as the page-set
  version.
- A limited-link OAuth handshake is created from the server-resolved page/share,
  carries that binding through signed state plus both handshake recovery paths,
  reauthorizes an otherwise generic session, and validates the current binding again
  on callback and before serving bytes.
- Every grant-set mutation is prepared without changing authority, then closes a
  persistent non-PII local share admission gate before commit and reopens it only
  after idempotent read-after-write reconciliation; an ambiguous response, restart,
  or expired operation cannot leave stale grants usable, email targets on disk, or
  a share binding lost. An expired uncommitted entry from private/public remains
  limited-and-closed until a new owner Apply rather than guessing the overwritten
  source audience.
- Every current-version local audience writer uses one stable process-shared write
  lease from its authoritative precondition read through a terminal or durably
  pending state; store writes cannot bypass the lease, and local grant commitments
  are opaque random backend values rather than email-derived hashes.
- Authorized share-link editor navigations redirect to the corresponding private URL
  and get the same Vite HMR and React Fast Refresh behavior as private editors.
- Annotation writes and the share-link editor redirect consume one validated,
  resource-aware editor capability.
- Every allowed non-editor `/p/` viewer receives the shared no-HMR representation
  with no annotations, private identifier, diagnostics, or bidirectional channel;
  disallowed limited viewers receive no page bytes.
- `/show/<session>/__vite_hmr` is the only HMR endpoint and independently
  revalidates authorization; `/p/<share>/__vite_hmr` does not exist.
- A loaded `/p/` document cannot switch representation as identity changes, and
  on a negotiated Runtime shared traffic cannot restart, rebase, or close the
  private Vite context.
- Capability-varying responses cannot be shared across viewers by a cache.
- Shared Service Workers cannot cache private bytes at `/p/` or control `/show/`;
  server-side revocation claims are limited to requests that reach Avibe.
- Ordinary private module reads preserve independently authorized resource-viewer
  access while HMR remains editor-only; page-email-only viewers are never private
  resource viewers.
- Runtime support is accepted only through the explicit keyed-context capability
  handshake and per-request protocol envelope; definitive mixed versions retain the
  existing compatibility limitation, while transient negotiation failures carry
  protocol `1` plus `shared` and retry without permanently disabling the feature.
- Context selection is mandatory at every protocol-`1` Runtime app-graph call site,
  including prewarm and fallback paths, and missing selection fails before Vite
  ownership; a headerless released client retains only the singleton legacy path.
- The legacy Runtime path never enables HMR, preserves annotations only for a proven
  editor, and fails closed for unclassified errors or graphs that need raw absolute
  paths; missing provenance cannot expose a development diagnostic.
- Shared file references use leased context-bound opaque handles backed by bounded
  immutable snapshots captured through confinement-safe opens. Asset handles serve
  captured bytes, while module, stylesheet, and query-qualified handles serve
  committed responses produced through context-scoped virtual-module transforms;
  handle reads never reopen mutable pathnames or return raw module source. Raw
  absolute paths, unsafe URL-bearing response headers, and Runtime development
  diagnostics never reach the browser.
- The access-schema migration is restartable, idempotent, revision-bound, and
  explicitly one-way. Avibe never starts an older executable against migrated state;
  no legacy audience projection or cross-version writer is part of the contract.
- A same-set grant prepare reuses the current commitment/revision without creating a
  pending operation. Leaving limited for public keeps anonymous reads open during
  best-effort grant cleanup, and offline audience changes never admit a route.
- Shared Vite contexts, opaque namespaces, handles, and snapshot bytes share one
  Runtime-wide weighted budget with a private-editor reserve and non-renewable
  lifetimes, so anonymous traffic cannot pin admission indefinitely.
- Direct durable offline mutations close active private HMR within five seconds;
  share rotation alone does not revoke entitled canonical private access.
- Authorization failure preserves fully public availability, fails limited access
  closed, and never selects HMR.
- Existing shared-route source-confinement and sensitive-path protections hold for the
  complete request surface.
- Local Incus measurements show bounded context memory and no material page-load
  regression beyond existing private/shared transform and limited-auth costs.
