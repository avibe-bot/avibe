# Remote Session Lifetime · Plan

> **Status**: Approved by the owner on 2026-11-04 (all five points in section 3).
> **Owner**: cyhhao

## 1. Background

Reaching an instance through avibe.bot needs re-authorization about once a day.
These are the lifetimes in use today (read on `master`, 2026-11-04):

| Credential | Lifetime | Source |
| --- | --- | --- |
| Instance session cookie `__Host-vibe_remote_session` | 24 h, renewed on a successful authenticated request once less than half remains | `vibe/remote_access.py` (`PERSONAL_SESSION_TTL_SECONDS`, `session_needs_renewal`), `vibe/ui_server.py` |
| Organization sessions | The same 24 h. `ORGANIZATION_SESSION_TTL_SECONDS` is defined but never used. Claims refresh every 12 h, with a 6 h outage grace. | `vibe/remote_access.py` |
| avibe.bot session `avibe_session` | 24 h, re-signed on every request while the Supabase session is valid | backend `middleware.ts`, `lib/settings.ts` |
| Supabase session | 1 h access token with refresh; cookie 400 days; no session time-box in the local config. The production setting is unverified. | backend `supabase/config.toml` |
| OAuth device cookie `__Host-vibe_oauth_device` | 180 days | `vibe/ui_server.py` |
| OAuth handshake, auth code, continuation | 5 min, 5 min, 10 min | instance and backend |

What a reader sees as a result:

- Because the cookie renews only past its half-life, the idle tolerance is
  between 12 and 24 hours.
- When the instance cookie lapses, the instance sends the reader to avibe.bot.
  There the consent page appears every time: there is no remembered grant.
- An iPhone Home Screen web app also needs a tap to start OAuth.
- So a daily user taps through two screens most days, and an occasional user
  every visit.

The cookie is not the main revocation control:

- Sign-out revokes the browser session.
- Revoking a member's access propagates through the authorization revision sync
  (15 s) and the organization claims refresh (12 h).

The cookie's lifetime is therefore mostly exposure if a device or a cookie is
lost.

## 2. Goal

A reader who uses Avibe at least once a month stays signed in. Re-consent at
avibe.bot is needed at most every 90 days, or after sign-out or revocation.
Revocation and sign-out remain immediate and durable.

## 3. Decisions (owner, 2026-11-04)

1. **Personal instances**: the session lapses after **30 days idle**. Any
   successful authenticated request renews it once the cookie was issued more
   than **1 day** ago, so the idle tolerance is a steady ~30 days.
2. **Absolute cap**: **90 days** from the sign-in that created the session. A
   renewal never extends past it, and after it the reader passes through
   avibe.bot again.
3. **Organization instances**: **7 days idle**, with the same daily renewal and
   the same 90-day cap. The 12 h claims refresh and the 6 h outage grace stay
   as they are.
4. **Remembered consent at avibe.bot**: a reader who has consented to an
   instance and is still authorized for it is not shown the consent page again.
   Consent is remembered for at most 90 days, matching the cap. This is an
   authentication change and needs a security review.
5. **Boot from the last session** (Web UI Phase 3, #3228) follows the session's
   real expiry instead of a fixed 24 hours.

## 4. Design

### 4.1 Instance (avibe)

- **Session claims**: add `auth_time`, the sign-in time, set at OAuth code
  exchange and carried unchanged through renewals.
  - A cookie without it is a pre-upgrade session; treat its `claims_issued_at`
    (else `iat`) as `auth_time`, so upgrading signs no one out.
  - `exp = min(now + idle_ttl, auth_time + 90 d)`, where `idle_ttl` is 30 d
    for personal and 7 d for organization instances (the instance kind is in
    the Vibe Cloud configuration).
- **Renewal**: `session_needs_renewal` becomes "issued more than 1 day ago and
  not already at the cap". The cookie's `max_age` is the remaining lifetime of
  that `exp`, never a fixed constant. A top-level page load (Fetch Metadata
  `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`) renews once the
  cookie is more than 1 hour old, so every app launch stores a cookie WebKit
  does not cap (see Risks).
- **Sign-out revocation must survive restarts.** `_REVOKED_BROWSER_SESSIONS`
  is in memory today. With 24 h cookies the window after a restart was at most
  a day; with 30–90 day cookies it is not acceptable.
  - Persist revoked browser session ids with their `exp` in the instance's
    SQLite state and prune expired rows.
  - Alternatively, mark the session's scoped authorization record revoked, if
    that record is already per sign-in and checked on every request; the
    implementation decides which holds and documents why.
- **`/api/session`** returns `session_expires_at` (Unix seconds, the cookie's
  current `exp`) for an authenticated remote session.
- **Everything that reads `SESSION_TTL_SECONDS`** keeps working. That covers
  revoked-session pruning, WebSocket session checks, the cookie set on renewal
  and at callback, and Show Page identity.

### 4.2 avibe.bot (avibe-bot-backend)

- **Store a consent grant** per (user, instance) when the reader approves the
  consent page, with `created_at` and `expires_at = created_at + 90 d`.
- **Skip the consent page** on `/oauth/consent` when a grant exists and has not
  expired, and `authorizeInteractiveInstanceAccess` still authorizes the
  reader. The authorization code is issued exactly as an approval would issue
  it. The access-denied page is unchanged.
- **Delete grants** when:
  - the reader's access to the instance ends (authorized-user removal,
    organization membership end);
  - the instance is deleted or re-paired;
  - the reader signs out of avibe.bot from the console, if there is a
    sign-out-everywhere action.
- **Security**: product and security review before merge, as required by
  `avibe-bot-backend/AGENTS.md`. A schema change goes through committed
  `drizzle/` migrations.

### 4.3 Web UI

- `ui/src/lib/bootSession.ts` keeps the boot record until the server-reported
  `session_expires_at` when the session carries one. Otherwise (local loopback,
  or an older instance) it uses 24 h, as today.

## 5. Risks

- **Safari tracking prevention may cap a renewed cookie at 7 days.** For a
  same-site subresource (a fetch) whose response has no CNAME, WebKit compares
  the response's IP address with the one the page itself loaded from and caps
  the cookies it sets to 7 days when fewer than half the bits match (16 for
  IPv4, 64 for IPv6), or the families differ (`NetworkTaskCocoa.mm`,
  `setCookieTransformForFirstPartyRequest`). It never applies to a top-level
  navigation. `*-app.avibe.bot` resolves to two unrelated Cloudflare addresses,
  so a renewal on a fetch over the other one can be capped. The sign-in
  callback is a top-level navigation, and a page load renews the cookie after
  an hour, so a capped cookie survives only while the app stays open without a
  reload. The worst case is a sign-in after 7 idle days instead of 30.
- **A lost device keeps access for up to the idle limit.** The mitigations are
  sign-out on another device, owner revocation, and the 90-day cap. The
  console's authorized-users page should keep showing who has access.
- **Supabase production settings are unverified.** A session time-box there
  would still bound avibe.bot sign-in; the owner checks the dashboard.

## 6. Validation

- **Instance unit tests with an injected clock**:
  - idle expiry at 30 d personal and 7 d organization, one second before and
    after;
  - daily renewal;
  - the 90-day cap holding through renewals;
  - a pre-upgrade cookie staying valid and gaining `auth_time`;
  - sign-out revocation holding across a simulated restart;
  - `session_expires_at` matching the cookie.
- **Scenario catalog**: new `tests/scenarios/auth_setup/catalog.yaml` rows with
  closed-loop cases for idle expiry, the cap, and durable sign-out.
- **Backend tests**:
  - the grant is created on approval, skips the page on the next authorize, is
    ignored once expired, and is deleted on access removal and on instance
    deletion;
  - the code issued on a skip is identical to an approved one.
- **After merge**: a local Incus regression (sign-in, a renewal, sign-out on one
  browser and reuse of the old cookie after a service restart), and an iPhone
  check of the 7-day cookie risk.

## 7. Delivery

- Instance and Web UI changes in this repository, one PR. The boot-record
  change lands in #3228 if it is still open, otherwise with this PR.
- avibe.bot remembered consent in `avibe-bot-backend`, one PR, after its
  security review.
- Neither PR depends on the other.
