# Issues 1433 and 1434: seamless session and Push authorization

Status: #1433 implementation complete; #1434 requires a separate follow-up.

Implementation snapshot (2026-08-15):

- Backend prerequisite `avibe-backend` PR #229 is merged, with `main` and `dev`
  synchronized at `a3f8e4b`; its Production deployment must be verified before
  the App rollout.
- App #1433 foundation, migration, shared resolver, non-blocking scheduled
  Organization refresh, transport enforcement, frontend recovery, logout
  isolation, and `AUTH-SETUP-402` / `AUTH-SETUP-403` coverage are implemented
  on `fix-issue-1433-seamless-authorization`.
- The branch was rebased onto `master` commit `5822a49e`, including upstream
  PR #1441 as unchanged base behavior. Current evidence is 836 related Python
  tests and all 2,759 frontend tests passing, plus Ruff and the production UI
  build.
- Upstream `master` independently merged PR #1441 for #1434 while this work was
  in progress. Rebase and compatibility review must keep that existing Push
  delivery change out of the #1433 diff, then identify any remaining #1434
  gaps in a separate follow-up.
- Incus and real iOS PWA checks remain integration evidence after backend #229
  is deployed; this branch must not merge before that deployment is verified.

Review circuit-breaker decision (2026-08-15):

- Codex findings-bearing heads were `ad8c339743` (6 findings), `298691f973`
  (2 findings), and `8b8f24b2f5` (3 findings). The third head triggered a
  whole-model review before further edits.
- The remaining transport finding class came from treating current authority as
  the complete live-session gate. A live SSE or WebSocket must first revalidate
  the accepted entry proof against the latest config: remote access is enabled,
  the original host is still configured, and the original cookie still parses
  to the same identity under the current session secret. Only then may it call
  the shared current-authorization resolver. One helper owns this check for all
  private SSE and WebSocket loops.
- The unavailable cold-load finding belongs to the existing frontend recovery
  state machine, not a new retry mechanism. AuthGuard reports the initial state
  to `remoteAuth`, which owns the bounded probe and manual retry timer.
- Application WebSocket close codes are observable only after the handshake is
  accepted. Initial Terminal and Show Runtime authorization failures therefore
  accept and immediately close without starting either protected service; other
  origin and generic policy rejections retain their existing handshake behavior.
- This review decision does not expand #1434 Push delivery, persistence, or
  backend contracts.

Review follow-up decision (2026-08-15):

- A new cookie's opaque authorization reference is deliberately not a signed
  scope hint. If that referenced row is missing, the resolver must not guess an
  Instance scope and call the backchannel: it fails closed as
  `invalid_identity` with `authorization_record_missing`, so normal login can
  create a fresh correctly scoped row. Legacy inline-claims cookies without a
  reference keep their compatibility migration path. A storage read failure is
  `unavailable`, not a fake logout.
- Private Show event replay revalidates current authorization once before each
  persisted batch, not before every event. A batch is bounded at 500 events;
  subsequent batches and the live loop retain their existing checks, preserving
  revocation while keeping replay work proportional to batch count.
- These corrections stay inside #1433's identity/current-authorization and
  long-lived transport boundaries. They do not add cookie fields, persistence
  types, retry machinery, or any of #1434's remaining Push behavior.

- [#1433](https://github.com/avibe-bot/avibe/issues/1433): restore the Personal
  session experience and separate Personal/Organization authorization policy.
- [#1434](https://github.com/avibe-bot/avibe/issues/1434): stop normal Web Push
  from depending on prompt-time claims snapshots.

## Root cause and scope

Both bugs come from treating one short-lived browser claims snapshot as four
different things: browser identity, current authorization, live-connection
lifetime, and Push authority. Fix the boundary once, then make HTTP, SSE,
WebSocket, and Push consume the same current-authorization result.

Keep the architecture small:

- one paired-device authorization query in `avibe-backend`;
- one shared resolver in the App;
- one evolved `remote_access_authorizations` store;
- no refresh-token lifecycle, routine browser `prompt=none`, second auth store,
  or durable Push queue.

Confirmed decisions:

- Organization control-plane outage grace is 6 hours.
- After 6 hours, identity stays authenticated while protected operations return
  `authorization_unavailable`; an outage never redirects to OAuth.
- Push may send a fully redacted fallback when authorization is unknown.
  Confirmed revocation or Project denial sends nothing.

## User experience contract

1. Sign in once, then use Avibe without routine authorization prompts.
2. Personal activity slides a 24-hour local session beyond both 12 and 24 hours.
3. Organization refresh and revision recovery happen through the device
   backchannel without opening a browser sheet or losing the requested route.
4. Only invalid/expired browser identity starts interactive login.
5. Confirmed access removal stops protected HTTP, SSE, WebSocket, and Push.
6. Temporary backend failure is an unavailable state, never a fake logout.
7. Explicit logout closes that browser's live connections and disables that
   device's Push subscription without signing out the user's other devices.

Expiry of the hosted avibe.bot browser session does not end an otherwise valid
local session. It matters only when interactive identity recovery is actually
needed.

## Policy contract

Define independently owned values rather than global constants plus call-site
exceptions.

| Behavior | Personal | Organization |
| --- | --- | --- |
| Identity TTL | 24 hours | 24 hours initially, independently named |
| Renewal | Slide after half-life on an accepted request | Independent setting; no browser OAuth just because renewal is due |
| Routine auth refresh | None in browser | 12-hour device backchannel initially |
| Revision | Background invalidation hint, never a request gate | Prompt poll; mismatch triggers one backchannel refresh |
| Revision outage | Keep last-known authority | Keep last-known authority for 6 hours |
| After grace | Continue under Personal policy | Authenticated but protected operations unavailable |
| Confirmed removal | Revoke | Revoke |

Personal request authorization must not evaluate Organization membership/group
logic, revision freshness, or revision snapshot age. A successfully observed
global revision change may schedule a background refresh, but failure of that
hint never blocks Personal access.

Unknown `instance_kind` must not default to Personal. First backfill from runtime
status or the new auth response; until then, retain the existing strict policy.
The poller must reload config/policy inside its loop so backfill takes effect
without restart.

## Backend prerequisite

Add this low-privilege paired-runtime endpoint in `avibe-backend`, following the
existing `user-token` device-secret boundary:

```http
POST /api/v1/instances/:instanceId/authorization-context
X-Vibe-Device-Secret: <paired device secret>

{
  "sub": "hosted-user-id",
  "email": "user@example.com",
  "show_page_id": "optional-show-page-id"
}
```

The endpoint:

- authenticates the Instance/device secret;
- calls `authorizeInteractiveInstanceAccess()`;
- returns only current Instance claims, `authorization_revision`, and
  `instance_kind`, or `access_denied`;
- preserves `show_page_email` as Show Page-only authority;
- shares one claim-projection helper with OIDC issuance so role, source,
  Organization, group, Show Page, and revision fields cannot drift;
- updates device last-seen consistently with the existing broker route;
- does not mint Cloud tokens or expose Organization directory data.

The App may send identity only from a signed cookie or trusted App-owned auth
record. Message `author_id`, arbitrary request bodies, and untrusted metadata
are never identity sources.

Contract tests cover Personal, Organization/group, public interactive access,
Show Page-only access, denial, invalid identity, and invalid device secret.

## App foundation for #1433

### Identity and current authorization

Split `parse_session_cookie()` into:

- identity parsing: size, signature, Instance binding, subject, identity expiry;
- authorization resolution: local record, selected policy, optional backchannel
  refresh;
- transport mapping: HTTP/SSE/WebSocket response behavior.

Use one result contract:

```text
resolve_current_authorization(identity, optional_scope)
    -> current(context, refreshed=false|true)
    -> revoked
    -> unavailable
```

Invalid/expired identity is separate from these outcomes. Concurrent callers for
the same `instance + subject + scope` share one bounded, process-local refresh so
API, SSE, WebSocket, and Push cannot create an auth storm.

Keep a synchronous core for middleware/Push and an async wrapper for SSE/WS that
runs blocking device I/O through the existing threadpool. Do not block the ASGI
loop or add `asyncio.run()` to request paths.

### Persistence and migration

Evolve `remote_access_authorizations`; do not add a competing claims table.
Add nullable columns:

```text
email
scope_kind           # instance | show_page for new rows
scope_ref            # instance id | show page id for new rows
authorization_state  # current | revoked for authoritative results
last_checked_at      # last successful hosted check
updated_at
```

Keep the authorization revision in `claims_json`; do not duplicate every claim
into columns. New records upsert through a partial unique index on:

```text
instance_id + subject + scope_kind + scope_ref
```

Instance and Show Page scopes never overwrite each other. Legacy random-ref rows
keep `scope_kind IS NULL` and their current `expires_at` behavior. New scoped
records are durable for Push, so `expires_at` becomes nullable for them. New
login/renewal silently writes the scoped shape; old cookies/rows load until
normal expiry.

`unavailable` is computed and never overwrites last-known claims as if they were
revoked. The 6-hour window starts at the last successful authoritative contact,
not local traffic. A revision poll extends a record only if its revision still
matches; mismatch must refresh or revoke it.

Malformed optional rows disable only that feature/row with a warning. Startup
and trusted local access must continue. Unpairing or changing Instance identity
invalidates records for the old Instance.

### Cookie renewal

Use a renewal path distinct from initial OIDC cookie creation:

- renew to a fresh 24-hour expiry after half-life on an accepted request;
- preserve `claims_issued_at` on a pure cookie slide;
- update it only after authoritative authorization refresh;
- preserve a browser-session id across renewal so logout can close that
  browser's connections;
- seed missing session ids on legacy-cookie renewal;
- update the local revision snapshot directly from successful OIDC claims
  instead of making a second hosted request.

The resolver uses the durable record's current claims/check time, so a Push-side
refresh does not become stale merely because a browser cookie was not reissued.

### Transport and frontend integration

Replace direct cookie/revision checks in middleware, `/api/session`,
`/api/cloud/token`, Workbench SSE, Show Page SSE, Show Runtime HMR, Terminal WS,
and any remaining direct consumer.

HTTP mapping:

- invalid/expired identity: `401 remote_access_login_required`;
- confirmed revocation: `403 remote_access_revoked`;
- unavailable after grace: `503 remote_access_authorization_unavailable`;
- current/cached within policy: continue.

`/api/session` returns `authenticated: true` plus `authorization_state` while
unavailable. Static assets, health, session diagnostics, and logout remain
reachable, but protected data does not.

Long-lived connections independently enforce identity expiry, confirmed
revocation, Organization grace expiry, Project/Show Page ACL change, and logout.
Before closing, emit distinct outcomes: WS `4401` login required, `4403` revoked,
and `4503` temporarily unavailable, with equivalent SSE terminal events.

Extend the existing `ui/src/lib/remoteAuth.ts` as the one frontend recovery
owner for API, SSE, and WS:

- login required preserves route and the existing iOS PWA user-gesture rule;
- revoked shows terminal access denied without OAuth retry;
- unavailable keeps the shell stable and retries with bounded backoff;
- current clears the recoverable state.

Remove Terminal's independent `4401 -> window.location.reload()`. Logout
publishes browser-session invalidation before deleting the cookie and accepts
the current Push device id/endpoint so only that subscription is disabled.

## Normal Push for #1434

New authenticated user-message metadata stores trusted `user_key` values only,
not `_web_push_authorization_contexts` snapshots. At delivery:

1. Resolve trusted owner user keys using existing authenticated-ingress rules.
2. Resolve current authorization through the shared resolver.
3. Re-check current local Project ACL with that context.
4. Select only that owner's enabled subscriptions.
5. Send full, redacted, or nothing according to the disposition.

Legacy full-context metadata remains compatibility input. It may bootstrap
subject/email/scope, but aged claims are not accepted as current Organization
authority. Never trust `author_id` or queued/untrusted metadata as owner.

Dispositions:

```text
sent
sent_redacted
revoked
project_denied
authorization_unavailable
no_owner
no_subscription
provider_failure
```

- Current and Project-visible: full payload.
- Revision changed: refresh once, then decide.
- Unavailable: one short retry, then redacted fallback.
- Revoked/Project denied: no delivery.
- Provider `404/410`: retain subscription-disable behavior.
- No durable retry queue.

Fixed redacted payload:

```text
Title: Avibe
Body: New activity is available
Action: Open Avibe
URL: /inbox
```

The outbound payload must contain no message text, Project/session name, badge,
message/session id, resource tag, or resource deep link.

Keep `/api/web-push/test` transport-only. Add nullable subscription diagnostics:

```text
last_normal_disposition
last_normal_attempt_at
last_normal_message_id
```

Expose the selected subscription's current auth state and last normal
disposition through `/api/web-push/status`, without claims, content, other users'
endpoints, or credentials.

## Compatibility and rollout

- Old cookies, random auth rows, and messages with full Push contexts remain
  readable and silently migrate on safe success.
- New messages remain schema-readable without full claims.
- Endpoint `404`, timeout, or malformed response means unavailable, never revoked
  or startup failure.
- Deploy the backend endpoint before App changes. In a mixed-version window,
  Personal keeps last-known policy; Organization uses 6-hour grace, then
  unavailable rather than OAuth.

Use three PRs:

1. `avibe-backend`: endpoint, shared projection, contract tests. Small-medium,
   about 1-2 engineering days.
2. `avibe-app` for #1433: policies, migration, resolver, cookie, transports,
   logout, frontend recovery, auth scenario coverage. Large, about 4-6 days.
3. `avibe-app` for #1434: metadata change, Push resolver/ACL, fallback,
   dispositions/status, regression tests. Medium, about 2-3 days.

Allow 1-2 additional days for cross-repo review, deployment, Incus regression,
and real iOS PWA checks. The total is large because #1433 crosses all transports,
not because it introduces a large framework.

## Verification

Automated coverage must prove:

- Personal sliding use beyond 12/24 hours and preserved `claims_issued_at`;
- Personal policy isolation from Organization groups/revision liveness;
- Organization silent scheduled/mismatch refresh, 6-hour grace, recovery, and
  authenticated-but-unavailable after grace;
- confirmed removal and logout across HTTP, SSE, WS, and Push;
- unknown-kind backfill changes live policy without restart;
- Instance versus Show Page scope isolation and current Project ACL narrowing;
- `/api/cloud/token` uses the shared result without coupling to hosted browser
  session liveness;
- Personal normal Push after the old 12-hour cutoff;
- unavailable Push sends only the fixed redacted payload after one retry;
- revoked/Project-denied Push sends nothing;
- provider `404/410`, legacy metadata/rows, and normal-path diagnostics.

Update `tests/scenarios/auth_setup/catalog.yaml` and its closed-loop scenario
harness for the browser flows. Use test-owned config/SQLite/network stubs only.
Run focused tests and Ruff, plus the UI build for frontend changes.

After review passes, update the local Incus `master` environment through the
runner and verify HTTP, SSE, Terminal/Show Runtime WS, logout, Personal sliding,
Organization outage/revocation, and normal/test Push diagnostics. Record real
iOS PWA notification behavior as the remaining manual evidence.
