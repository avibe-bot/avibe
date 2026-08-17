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

The three modes are closed:

- `private`: `/p` is disabled. A prior share binding may remain stored but is inactive.
- `limited`: a new `/p` navigation needs verified identity and current local membership.
- `public`: a new `/p` navigation allows anonymous access.

Limited and public preserve the same share binding unless the owner explicitly
replaces it. `Apply` uses `expected_revision`; it has no hosted operation and no
durable mutation receipt. After a lost response, the client reads current state.

The Backend returns only a short-lived RS256 identity assertion. That assertion
does not contain page, share, membership, role, or resource-access claims. The
local identity session proves identity only. Page admission remains a fresh local
decision at each new top-level navigation or manual refresh.

After admission, the opaque document capability remains valid for that loaded
document and its subresources until the tab, document, or Runtime namespace ends.
Audience changes affect the next navigation or refresh. They do not poll, push a
refresh, revoke loaded subresources, or close the loaded guest page.

`/p` always uses shared Runtime context and never exposes HMR, annotations,
Avibe cookies or storage, local APIs, session IDs, or source paths. Shared code
runs in a sandboxed opaque-origin iframe. Worker, SharedWorker, and Service Worker
are unsupported in version 1. Protected shared resources use an opaque document
capability and credentialless CORS; they never accept ambient identity or cookies.
Private editor edits never create, build, or rebase a shared graph.

`/show` uses private Runtime context. Resource-editor authority is checked when
an HMR/annotation connection is admitted. Permission changes do not require an
active polling or forced-close protocol; later connections authorize again.

## Evidence Boundary

The focused tests validate all three schemas and examples, exact interface fields,
closed vocabularies, local-only membership, identity-only claims, Runtime protocol
constants, keyed-context fail-closed behavior, admission lifetime, worker denial,
and cache boundaries.

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
