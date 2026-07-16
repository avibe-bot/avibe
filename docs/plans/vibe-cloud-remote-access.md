# Vibe Cloud Remote Access Plan

## Summary

Vibe Remote should move from a "bring your own Cloudflare" setup flow to a
managed remote-access product:

- Vibe Cloud provisions the public hostname and Cloudflare Tunnel.
- Vibe Cloud acts as the OAuth/OIDC authorization service.
- User traffic does not proxy through Vibe Cloud after login.
- Cloudflare Tunnel carries browser traffic directly to the user's local Vibe
  Remote Web UI.
- The local Vibe Remote Web UI enforces its own authenticated cookie session
  when remote access is enabled.

This plan intentionally does not use Cloudflare Access for the default managed
provider. Cloudflare Access remains useful for the advanced BYO Cloudflare mode,
but its per-user pricing model is not a good fit for a public Vibe-hosted
remote-access product.

## Goals

- Let a user expose a local Vibe Remote admin UI at `https://<slug>.avibe.bot`
  without manually configuring Cloudflare.
- Keep Vibe Cloud out of the data path after OAuth authorization.
- Require authentication for every remotely accessed Web UI request.
- Keep tunnel credentials, OAuth clients, sessions, and allow-lists revocable.
- Preserve a BYO Cloudflare provider for advanced users who want full ownership.

## Non-Goals

- Vibe Cloud does not proxy all Vibe Remote admin traffic.
- The first managed version does not support customer-owned custom domains.
- The first managed version does not provide Cloudflare Access policies.
- The first managed version does not expose arbitrary local services; it only
  exposes the Vibe Remote Web UI origin.

## High-Level Architecture

```text
Browser
  -> https://<slug>.avibe.bot
  -> Cloudflare edge
  -> Cloudflare Tunnel
  -> cloudflared on the user's machine
  -> http://127.0.0.1:<ui_port> Vibe Remote Web UI
```

Vibe Cloud remains the control plane:

```text
Vibe Cloud
  - user accounts
  - remote-access instances
  - allow-lists
  - OAuth/OIDC issuer
  - pairing keys
  - Cloudflare Tunnel provisioning
  - tunnel token rotation/revocation
```

The local Vibe Remote instance remains the enforcement point for the admin UI:

```text
Vibe Remote local Web UI
  - remote-access mode switch
  - pairing-key redemption
  - cloudflared install/start/stop lifecycle
  - OAuth callback handler
  - ID token verification against Vibe Cloud JWKS
  - local HttpOnly cookie session
  - CSRF protection for state-changing API calls
```

## Access Flow

### 1. Instance Creation

1. The user signs in to `avibe.bot`.
2. The user creates a Remote Access instance.
3. Vibe Cloud allocates:
   - `instance_id`
   - `client_id`
   - a public hostname such as `alex-dev.avibe.bot`
   - an internal tunnel hostname, if needed by the provisioning layer
   - a one-time pairing key
4. Vibe Cloud provisions Cloudflare:
   - creates a remote-managed Tunnel
   - creates a DNS route for `alex-dev.avibe.bot`
   - stores the tunnel token encrypted at rest
5. The user configures an allow-list for the instance:
   - individual emails
   - optionally email domains in later versions
   - optionally third-party identity providers in later versions

### 2. Pairing

1. The user opens the local Vibe Remote Web UI.
2. The user selects `Remote Access -> Vibe Cloud`.
3. The user pastes the pairing key.
4. Vibe Remote calls Vibe Cloud:

```http
POST /api/v1/pairing/redeem
Content-Type: application/json

{
  "pairing_key": "vrp_...",
  "device_name": "alex-macbook",
  "local_version": "x.y.z"
}
```

5. Vibe Cloud returns the local runtime configuration:

```json
{
  "instance_id": "inst_...",
  "client_id": "vr_client_...",
  "issuer": "https://avibe.bot",
  "authorization_endpoint": "https://avibe.bot/oauth/authorize",
  "token_endpoint": "https://avibe.bot/oauth/token",
  "jwks_uri": "https://avibe.bot/oauth/jwks.json",
  "public_url": "https://alex-dev.avibe.bot",
  "redirect_uri": "https://alex-dev.avibe.bot/auth/callback",
  "tunnel_token": "eyJh...",
  "instance_secret": "vrs_..."
}
```

6. Vibe Remote stores the config locally with sensitive fields encrypted where
   platform support exists.
7. Vibe Remote installs or resolves `cloudflared`.
8. Vibe Remote starts:

```bash
cloudflared tunnel --no-autoupdate run --token <tunnel_token>
```

### 3. Browser Login

1. A browser opens `https://alex-dev.avibe.bot`.
2. Traffic goes directly through Cloudflare Tunnel to the local Web UI.
3. The local Web UI checks for a valid remote-access session cookie.
4. If no valid cookie exists, it redirects to Vibe Cloud OAuth:

```text
https://avibe.bot/oauth/authorize
  ?client_id=vr_client_...
  &redirect_uri=https%3A%2F%2Falex-dev.avibe.bot%2Fauth%2Fcallback
  &response_type=code
  &scope=openid%20email
  &state=<random>
  &nonce=<random>
  &code_challenge=<pkce_challenge>
  &code_challenge_method=S256
```

5. Vibe Cloud authenticates the user and checks the instance allow-list.
6. If allowed, Vibe Cloud redirects back:

```text
https://alex-dev.avibe.bot/auth/callback?code=<code>&state=<state>
```

7. The local Web UI exchanges the code:

```http
POST https://avibe.bot/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&client_id=vr_client_...
&code=<code>
&redirect_uri=https%3A%2F%2Falex-dev.avibe.bot%2Fauth%2Fcallback
&code_verifier=<pkce_verifier>
```

8. Vibe Cloud returns an ID token and optional access token.
9. The local Web UI validates the ID token:
   - `iss == https://avibe.bot`
   - `aud == client_id`
   - `sub` is present
   - `email_verified == true`
   - `email` matches the authorized identity
   - `nonce` matches the login attempt
   - `exp`, `iat`, and key id are valid
10. The local Web UI creates its own local session cookie:

```http
Set-Cookie: __Host-vibe_remote_session=<opaque>; Path=/; Secure; HttpOnly; SameSite=Lax
```

11. Subsequent browser requests go directly to the local Web UI through the
    Tunnel and are authorized by the local session cookie.

## Security Model

### Trust Boundaries

- Vibe Cloud is trusted for identity, allow-list decisions, tunnel provisioning,
  and signing OIDC ID tokens.
- Cloudflare is trusted for Tunnel connectivity and TLS termination for
  `*.avibe.bot`.
- The local Vibe Remote instance is trusted to enforce admin UI sessions.
- Browsers and arbitrary request headers are not trusted.

### Vibe Cloud Keys

Vibe Cloud owns the OIDC signing private keys. It publishes public keys through
JWKS:

```text
GET https://avibe.bot/oauth/jwks.json
```

The local Vibe Remote instance must never store the Vibe Cloud OIDC private key.
It only stores JWKS cache data and local secrets needed for its own session
management.

### Local Keys

Each paired local instance stores:

- `instance_id`
- `client_id`
- `instance_secret` or a future asymmetric device credential
- `tunnel_token`
- JWKS cache
- local session signing/encryption key

The local session key only protects the local cookie. It must not be used to
sign Vibe Cloud identity tokens.

### Cookie Enforcement

When managed remote access is enabled:

- all Web UI routes and API routes require a valid local session cookie unless
  they are explicitly part of the auth callback or health path
- state-changing API calls require CSRF protection
- the cookie must be `Secure`, `HttpOnly`, host-only, and `SameSite=Lax`
- the cookie must not use `Domain=.avibe.bot`
- disabling remote access clears active remote sessions

### Revocation

Revocation must be available at multiple levels:

- revoke a single local device pairing
- revoke and rotate an instance secret
- rotate the Cloudflare tunnel token
- disable the instance and stop accepting OAuth authorizations
- delete the instance and remove Cloudflare Tunnel/DNS resources

## Data Model

### Vibe Cloud

```text
users
  id
  email
  email_verified
  name
  created_at
  updated_at

remote_access_instances
  id
  owner_user_id
  slug
  public_hostname
  oauth_client_id
  status                    # pending | active | disabled | deleted
  tunnel_id
  tunnel_token_ciphertext
  tunnel_token_version
  cloudflare_account_id
  cloudflare_zone_id
  created_at
  updated_at
  disabled_at

remote_access_allowlist_entries
  id
  instance_id
  kind                      # email | email_domain
  value
  created_at
  updated_at

remote_access_pairing_keys
  id
  instance_id
  key_hash
  expires_at
  consumed_at
  consumed_by_device_id
  created_at

remote_access_devices
  id
  instance_id
  device_name
  device_secret_hash
  last_seen_at
  revoked_at
  created_at

oauth_authorization_codes
  id
  code_hash
  instance_id
  client_id
  user_id
  redirect_uri
  code_challenge
  code_challenge_method
  nonce
  expires_at
  consumed_at
  created_at

audit_events
  id
  actor_user_id
  instance_id
  event_type
  ip_address
  user_agent
  metadata_json
  created_at
```

### Vibe Remote Local Config

```json
{
  "remote_access": {
    "provider": "vibe_cloud",
    "vibe_cloud": {
      "enabled": true,
      "instance_id": "inst_...",
      "client_id": "vr_client_...",
      "issuer": "https://avibe.bot",
      "public_url": "https://alex-dev.avibe.bot",
      "redirect_uri": "https://alex-dev.avibe.bot/auth/callback",
      "tunnel_token": "<secret>",
      "instance_secret": "<secret>",
      "jwks_cache": {
        "keys": [],
        "expires_at": "..."
      }
    },
    "cloudflare": {
      "enabled": false
    }
  }
}
```

The existing BYO Cloudflare config should stay available as a separate provider.

## API Contracts

### Create Instance

```http
POST /api/v1/instances
Authorization: Bearer <vibe_cloud_user_session>
Content-Type: application/json

{
  "slug": "alex-dev"
}
```

Response:

```json
{
  "instance_id": "inst_...",
  "public_url": "https://alex-dev.avibe.bot",
  "pairing_key": "vrp_...",
  "status": "pending"
}
```

### Update Allow-List

```http
PUT /api/v1/instances/{instance_id}/allowlist
Authorization: Bearer <vibe_cloud_user_session>
Content-Type: application/json

{
  "entries": [
    {"kind": "email", "value": "alex@example.com"}
  ]
}
```

### Redeem Pairing Key

```http
POST /api/v1/pairing/redeem
Content-Type: application/json

{
  "pairing_key": "vrp_...",
  "device_name": "alex-macbook",
  "local_version": "x.y.z"
}
```

### OAuth Authorization

```http
GET /oauth/authorize?...standard OIDC authorization-code parameters...
```

### OAuth Token

```http
POST /oauth/token
Content-Type: application/x-www-form-urlencoded
```

### JWKS

```http
GET /oauth/jwks.json
```

## Implementation Phases

### Phase 1: Planning and Backend Skeleton

- Create the private `avibe-bot/avibe-backend` repository.
- Add backend architecture plan.
- Implement API skeleton, data models, config, and tests.
- Implement OIDC discovery and JWKS endpoints.
- Implement pairing-key creation and redemption with local persistence.
- Stub Cloudflare provisioning behind a provider interface.

### Phase 2: Cloudflare Provisioning

- Add Cloudflare API client.
- Create remote-managed Tunnel.
- Create DNS CNAME route for `<slug>.avibe.bot`.
- Store tunnel token encrypted at rest.
- Add deletion, disable, and token rotation flows.

### Phase 3: Local Vibe Remote Integration

- Add `vibe_cloud` remote-access provider.
- Add pairing-key UI flow.
- Add remote auth middleware for local Web UI/API.
- Add OAuth callback handler.
- Add local session cookie and CSRF enforcement.
- Reuse the existing cloudflared install/start/stop lifecycle.

### Phase 4: Beta Hardening

- Add audit logs.
- Add rate limits for OAuth, pairing, and token exchange.
- Add admin controls for revoke/disable/delete.
- Add observability and structured errors.
- Add abuse controls and instance quotas.

## Open Questions

- Which hosted auth stack should Vibe Cloud use for its own user login:
  first-party auth, Auth0, Clerk, or another OIDC provider?
- Should the initial backend use Postgres only, or allow SQLite for local
  development?
- Should paired devices use symmetric `client_secret` first, or start with
  asymmetric device credentials?
- Should Vibe Cloud host all OAuth UI, or delegate login UI to a managed IdP and
  keep only OIDC issuance and allow-list decisions?
- How should we bill and rate-limit `*.avibe.bot` remote instances?
