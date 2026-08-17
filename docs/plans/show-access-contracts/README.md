# Show Access Boundary Contracts

Version 1 freezes three cross-repository boundaries for the first Unified Show
Access release. Each JSON file is a self-contained Draft 2020-12 JSON Schema
with machine-readable ownership, interface, policy, and example metadata.

| Contract | Authority | Producer -> consumer |
| --- | --- | --- |
| [`show-access.json`](show-access.json) | Local Avibe only | Sharing UI -> local HTTP -> controller stable writer -> local store |
| [`identity-auth.json`](identity-auth.json) | Paired Backend identity proof only | Local login -> Backend authorize -> fixed local `form_post` callback |
| [`runtime-containment.json`](runtime-containment.json) | Avibe admission, Runtime isolation | Avibe trusted proxy -> Runtime protocol 1 private/shared contexts |

## Product Boundary

`ShowAccess` contains the complete local audience state: `access_mode`, stable
`share_id`, monotonic `revision`, and the normalized exact-email set. One local
stable writer replaces these fields atomically. The Backend never receives or
evaluates the email set.

Email syntax is deliberately pragmatic ASCII: exactly one `@`, a dot-atom local
part without leading, trailing, or consecutive dots, and lowercase DNS-style
domain labels without leading or trailing hyphens.

The three modes are closed:

- `private`: `/p` is disabled. A prior share binding may remain stored but is inactive.
- `limited`: a new `/p` navigation needs verified identity and current local membership.
- `public`: a new `/p` navigation allows anonymous access.

Limited and public preserve the same share binding unless the owner explicitly
replaces it. `Apply` uses `expected_revision`; it has no hosted operation and no
durable mutation receipt. After a lost response, the client reads current state.

The Backend returns only a short-lived RS256 identity assertion. A known signing
key is cached for at most 300 seconds; an older cached key is fetched once from
the paired issuer before validation, and fetch failure asks the visitor to retry.
That assertion
does not contain page, share, membership, role, or resource-access claims. The
local identity session is one host-only `__Host-` cookie containing 32 random
bytes plus a local record keyed only by the token's SHA-256 digest. It lasts a
fixed 30 days without sliding refresh and proves identity only. Page admission
remains a fresh local decision at each new top-level navigation or manual refresh.
The pending-flow cookie is scoped to `/auth/show-identity`, so local login start
and callback see the same opaque browser value. Login start signs that value's
SHA-256 digest into the 300-second state and stores no pending-flow record. A
newer start overwrites the browser cookie, so an older callback fails while
separate browser cookie jars remain independent. HTTP requests carry only the
cookie name/value pair; attributes exist only on `Set-Cookie` projections.

After admission, the opaque document capability remains valid for that loaded
document and its subresources while its Runtime namespace and document handle
exist. Audience changes and elapsed time do not expire it. They do not poll, push
a refresh, revoke loaded subresources, or close the loaded guest page. Namespace
loss is limited to Runtime restart, explicit operational shutdown, or genuine
pressure under the fixed process-wide resource budget.

`/p` always uses shared Runtime context and never exposes HMR, annotations,
Avibe cookies or storage, local APIs, session IDs, or source paths. Shared code
runs in a sandboxed opaque-origin iframe. Worker, SharedWorker, and Service Worker
are unsupported in version 1. Protected shared resources use an opaque document
capability and credentialless CORS; they never accept ambient identity or cookies.
Private editor edits never create, build, or rebase a shared graph. Shared graph
keys include the internal source Session ID, but that value never crosses the
browser boundary. Every protected resource uses one versioned opaque path with
URL-safe namespace, document, and capability segments. The document root ends in
`/`; opaque assets use `asset/`, page APIs use `api/`, and nested-route reloads use
`history/` with the same safe relative-path grammar, including the existing `.`
and `@` route characters. The transformed fallback document establishes the
capability root as its base URL, so relative APIs and assets do not inherit the
nested history path. Page-API requests carry only method, safe path, optional
normalized content type, and an optional base64 body bounded to 1 MiB; ambient
headers are excluded. Nested imports are rewritten under the same prefix and
capability segments are redacted from access logs. A limited admission always
includes a nonempty verified subject. Shared capture returns either a capability
or one fixed sanitized failure result.

`/show` uses private Runtime context. Resource-editor authority is checked when
an HMR/annotation connection is admitted. Permission changes do not require an
active polling or forced-close protocol; later connections authorize again.

## Evidence Boundary

The focused tests validate all three schemas and examples, exact interface fields,
closed vocabularies, local-only membership, identity-only claims and session
record, Runtime protocol constants, keyed-context fail-closed behavior, admission
lifetime, worker denial, and cache boundaries. `AUTH-SETUP-401` supplies a small
closed-loop contract scenario through the stub Backend authorize and `form_post`
boundaries, signed-state/cookie/assertion checks, digest-only local session, and a
later membership-rechecked navigation.

This repository does not yet prove production Avibe, Backend, Runtime, browser,
or Incus conformance. Those consumers must implement the same contract version
and supply integration, browser, security, and release evidence in their delivery
lanes. Runtime capability advertisement additionally requires the reviewed,
smoke-tested, and bundled Runtime SHAs to match.

## Deliberate Omissions

There is no production data to preserve. Version 1 therefore defines no hosted
email grant, legacy migration, compatibility bridge, prepare/commit operation,
cleanup/reconciliation workflow, mutation receipt, worker broker, permission
revocation monitor, or namespace pin/reclaim state machine. Obsolete hosted table,
endpoint, proxy, and `show_page_email` authorization code is retired directly by
future implementation lanes.
