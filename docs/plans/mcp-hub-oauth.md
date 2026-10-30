# MCP Hub OAuth

> Status: spec, owner decisions recorded 2026-10-30. Extends `docs/plans/mcp-hub-mvp.md`.
> Reference server: `{"mcpServers": {"suno": {"url": "https://api.suno.com/mcp"}}}`.

## 1. Background

MCP Hub (`docs/MCP_HUB.md`) treats an HTTP 401 as an ordinary refresh failure, so an
OAuth-protected server such as Suno is never listed. The pinned SDK (`mcp>=2.3.0,<3`)
already ships `mcp.client.auth.OAuthClientProvider` (discovery, DCR/CIMD, PKCE, refresh,
SEP-2352 issuer binding); Avibe supplies only `TokenStorage`, `redirect_handler` and
`callback_handler`.

Probed Suno (2026-10-30):

- `POST /mcp` → `401`, `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/mcp"`.
- PRM → AS `https://studio-api-prod.suno.com/api/v2/external/oauth`; scopes include `credits:spend`, `offline_access`.
- AS: PKCE S256, `token_endpoint_auth_method: none`, `refresh_token`, DCR and CIMD.
- DCR redirect acceptance: `https://<any>/…` ✅, `http://127.0.0.1:<port>/…` ✅, `http://localhost/…` ✅,
  `http://192.168.1.20:5123/…` ❌, `http://myhost.lan:5123/…` ❌ (`invalid_redirect_uri`).
  This is the OAuth 2.1 rule (plain HTTP only on loopback) and must be assumed for every AS.

Prior art: Claude Code (401/403 → "needs authentication", `/mcp` or `claude mcp login`, random
loopback port, keychain/credentials file, paste-the-callback fallback), Codex (`codex mcp login`,
`--no-browser`, `mcp_oauth_callback_url` for remote ingress, file/keyring store), OpenCode
(`mcp-auth.json`; known bug: concurrent flows overwrite tokens and re-register clients),
Claude.ai (hosted HTTPS callback registered through DCR).

## 2. Decisions (owner, 2026-10-30)

1. **Needs-login is a visible state.** Page and `vibe mcp --list` show it; the Agent prompt
   index omits it (no tools yet). A published server whose token later fails stays listed;
   calls return `auth_required`.
2. **Scopes:** request what the SDK selects by spec (challenge → PRM `scopes_supported`), like
   Claude Code / Codex. Optional `oauth.scopes` narrows it.
3. **Token storage:** a 0600 file aligned with Claude Code's credentials-file behavior, not Vault.
4. **Pre-registered clients deferred.** Phase 1 accepts Claude-format `oauth.clientId` and
   `oauth.scopes` only; no fixed callback port.
5. **DCR only.** No CIMD in phase 1 (each instance has a different origin; one hosted metadata
   document cannot list them).

## 3. Callback: the Web UI may be remote

The redirect URI must be registered (DCR), sent at authorize, and sent again at token
exchange, byte-identical. Avibe's UI is reached through several origins, and only some are
legal redirect targets:

| How the browser reaches Avibe | Origin (`vibe/ui_server.py` `_current_origin()`) | Legal redirect? | Mode |
|---|---|---|---|
| Same machine, `http://127.0.0.1:5123` / `localhost` | loopback | ✅ | direct |
| SSH tunnel to loopback | loopback (on the browser's machine) | ✅ | direct |
| Avibe Cloud `public_url` | `https://…` | ✅ | direct |
| Reverse proxy listed in `ui.trusted_public_origins` | `https://…` | ✅ | direct |
| Same, but proxy serves plain HTTP | `http://<host>` | ❌ | paste |
| LAN IP / hostname `http://192.168.x.x:5123` | `http://<lan>` | ❌ | paste |

**Rule.** A login gets one redirect URI chosen at start and kept with the pending login:

- **direct** — origin is `https`, or `http` on a loopback host (`127.0.0.1`, `localhost`, `[::1]`):
  `redirect_uri = <origin>/api/mcp/oauth/callback`. The AS redirects the browser there and the
  login completes without user action.
- **paste** — anything else: `redirect_uri = http://127.0.0.1:<ui.setup_port>/api/mcp/oauth/callback`.
  If the browser happens to run on the Avibe host it completes directly. Otherwise the browser
  lands on an unreachable page; the login dialog tells the user to copy that address-bar URL
  and paste it into the dialog. This is the Claude Code / Codex `--no-browser` fallback.
- **CLI/IM start** (no browser origin): Avibe Cloud `public_url` when remote access is enabled,
  else paste mode. The CLI always prints the URL and accepts a pasted callback.

Consequences:

- **Origin derivation** reuses `_current_origin()`; no new trust in `Host`/`X-Forwarded-*`.
- **Client registration is per redirect URI.** Storage returns the saved DCR client only when its
  `redirect_uris` contains this login's URI; otherwise the SDK re-registers. Switching between
  localhost and Cloud therefore works without manual reset; the token set is shared.
- **Callback auth.** `/api/mcp/oauth/callback` stays behind the normal UI session (no exemption).
  The Cloud session cookie is `SameSite=Lax`, so the AS's top-level GET redirect carries it. The
  `state` value is the binding to a pending login; unknown/expired/used `state` is rejected.
- **The pasted URL is validated** against the pending login: same `state`, path
  `/api/mcp/oauth/callback`; `code`, `state`, `iss` are extracted from its query. Host/port are not
  required to match (the user may paste from another device).
- Regression/Incus: environments reached through Cloud use direct mode.

## 4. Flow (Suno)

```text
add {"suno":{"url":"https://api.suno.com/mcp"}}
  → refresh: 401 + resource_metadata → status needs_login (definition saved, not "failed")
Owner: page "Log in" | `vibe mcp suno --login`
  → controller: new pending login {state, redirect_uri, mode}, SDK provider runs discovery + DCR,
    redirect_handler yields the authorize URL → returned to caller
browser authorizes → AS redirects to redirect_uri?code&state&iss
  → direct: UI route → IPC → controller resolves the pending login
  → paste:  dialog / `vibe mcp suno --login-complete '<url>'` → same IPC
  → SDK exchanges token → storage saves → refresh → tools published
call → per-server token manager (§5) supplies a fresh Bearer per HTTP request
     → refresh token rejected → envelope error.type = auth_required, hint `vibe mcp suno --login`
```

- The SDK's grant suspends inside `callback_handler`; it awaits a Future that the callback/paste
  IPC resolves. Pending logins are in controller memory, single-use, 10-minute TTL, one per
  server (a new start cancels the previous).
- The SDK `OAuthClientProvider` is used **only for interactive login** (discovery, DCR, PKCE,
  code exchange). Calls and refresh never go through it; see §5.

## 5. Token refresh

### 5.1 Prior art (read from source, 2026-10-30)

| | Claude Code 2.1.314 (bundled JS) | Codex (`openai/codex@d7b77a5`, `codex-rs/rmcp-client`, rmcp `e5698cb`) | OpenCode (`anomalyco/opencode@f29e281`, `packages/core/src/mcp/oauth.ts`) |
|---|---|---|---|
| Persisted expiry | absolute `expiresAt` | absolute `expires_at` (ms) | absolute `expires`, converted back to `expires_in` on load |
| AS metadata after restart | persisted `discoveryState` (AS URL + metadata) | re-resolved at connection start | SDK discovery state |
| When | proactive inside `tokens()` when <= 300 s left | before every client operation, 30 s skew | SDK |
| In-process dedupe | one `_refreshInProgress` promise | `RefreshCredentialLock` | single-flight keyed by token URL + client_id + refresh_token |
| Cross-process | `mcp-refresh-<key>.lock` (stale 60 s), then re-read for a winner's fresh tokens | `mcp-oauth-locks/<sha>.lock`, then authoritative re-read | none |
| Request 401 | needs-auth; remembers the rejected access token and stops sending it | rmcp: one silent refresh, retry once only if the token changed, else challenge; Codex then returns "Authentication required" without replaying | SDK; `UnauthorizedError` → needs_auth |
| Refresh rejected | needs-auth (`mcp_oauth_token_refresh_failed`) | retire refresh token **only if it still matches the one presented** | delete credential **only if it still holds the presented refresh token** |
| Transient failure | keep and return current tokens | propagate, retry later | SDK |
| Caller cancellation | — | refresh runs in an owned task so cancellation cannot lose a rotated token; 45 s timeout | — |
| Background timer | no | no | no |
| Logout | revokes at the AS revocation endpoint | — | — |

The Python SDK's own provider is not usable as-is across restarts (`mcp` 2.3.0
`client/auth/oauth2.py`): expiry lives only in memory; a 401 triggers full re-authorization
instead of a refresh; without in-memory AS metadata, refresh falls back to `<server origin>/token`
(Suno: `https://api.suno.com/token` → 404; real endpoint
`https://studio-api-prod.suno.com/api/v2/external/oauth/token/`). Claude Code and Codex both
wrap their SDKs for the same reasons.

### 5.2 Avibe design

The SDK `OAuthClientProvider` is used only for interactive login. A per-server **token manager**
in the MCP runtime owns refresh, following Claude Code where the three clients differ.

- **Persisted inputs** (§6): absolute `expiresAt`, refresh token, the issuing `clientId`, AS
  `issuer` + `tokenEndpoint` (+ `revocationEndpoint`) and RFC 8707 `resource` captured at login
  (Claude Code persists discovery state too). Refresh needs no rediscovery.
- **Single writer, no file lock.** Claude Code and Codex need cross-process lockfiles because
  many CLI processes share one store. In Avibe only the Controller's MCP runtime reads or writes
  `oauth.json` (CLI/UI go through IPC), so refresh, login completion and logout serialize on one
  `asyncio.Lock` per server on the runtime loop. Waiters re-check expiry after acquiring the lock,
  so N concurrent callers cause one token request.
- **Per request.** HTTP connections attach an `httpx.Auth` that asks the manager for the token on
  every request, so a Session connection held open past the access-token lifetime keeps working.
- **Proactive, 300 s skew** (Claude Code): refresh when `expiresAt - now <= 300 s`. No
  `expiresAt` → use the token until a 401. No background timer (none of the three has one).
- **Owned refresh task** (Codex): the refresh + persist runs as its own task with a 45 s timeout;
  a caller timing out or being cancelled never discards a rotated refresh token.
- **Rotation.** Persist (`write_atomic`) before the new access token is used. A response without
  `refresh_token` / `scope` keeps the previous one (RFC 6749 §6). If the write fails, keep the new
  set in memory, log, and retry the write on the next use.
- **Request 401** (rmcp): refresh once under the lock; resend once only if that produced a
  different access token; otherwise `auth_required`. The server rejected the request before
  running it, so the resend is safe for `tools/call`. A rejected access token is not sent again.
- **Failure classes.**
  - Token endpoint `400 invalid_grant` / `invalid_client` / `401` → refresh token dead. Drop it
    **only if `oauth.json` still holds the refresh token that was presented** (Codex/OpenCode
    compare-and-retire), keep the client, status `needs_login`, call returns `auth_required`.
  - Network error, `5xx`, `429`, timeout → transient. Keep tokens; if the access token has not
    actually expired yet, use it (Claude Code); otherwise fail `connection`, retry on next use.
  - Expired and no refresh token (no `offline_access` grant) → `auth_required`.
- **Logout** revokes refresh then access token at `revocationEndpoint` when advertised
  (best effort, Claude Code), then deletes `oauth.json`.
- Logs carry status and OAuth `error` code only, never token values.

## 6. Storage

`<avibe-home>/mcp/<name>/oauth.json`, mode 0600, `write_atomic`:

```json
{"schemaVersion": 1, "serverUrl": "https://api.suno.com/mcp",
 "resource": "https://api.suno.com/mcp",
 "authServer": {"issuer": "…", "tokenEndpoint": "…", "revocationEndpoint": "…"},
 "tokens": {"accessToken": "…", "refreshToken": "…", "expiresAt": 1793345230, "scope": "…",
            "clientId": "…"},
 "clients": [{"clientId": "…", "clientSecret": null, "tokenEndpointAuthMethod": "none",
              "redirectUris": ["…"], "issuer": "…"}]}
```

- `clients` holds one DCR registration per redirect URI (§3). A refresh token is bound to the
  client that obtained it, so refresh authenticates as `tokens.clientId`, not the latest client.
- Not in `definition.json` or `revisions/`; never printed by list/help/API.
- Ignored (treated as absent) when `serverUrl` differs from the definition URL.
- Deleted on `--remove` / hide, and by `--logout`.
- Native-source OAuth servers get their own Avibe login; native token stores are not read.

## 7. Precedence

A definition with an explicit `Authorization` header never uses OAuth. Otherwise HTTP servers
attach the OAuth provider; a server that never returns 401 never triggers it.

## 8. Surfaces

CLI (Owner for login/logout, like refresh/remove):

```bash
vibe mcp suno --login                        # prints authorize URL (+ paste hint), returns
vibe mcp suno --login-complete '<callback URL>'
vibe mcp suno --logout
```

API: `POST /api/mcp/{name}/oauth/start` (body carries nothing; origin from request) →
`{authorizeUrl, mode}`; `GET /api/mcp/oauth/callback`; `POST /api/mcp/{name}/oauth/complete`
`{callbackUrl}`; `POST /api/mcp/{name}/oauth/logout`. Summary gains `auth: none|needs_login|ok`.

UI: needs-login rows show a login action; the login dialog opens the authorize URL in a new tab,
shows the paste box in paste mode, and polls/refreshes on completion. Strings via i18n.

Definition: accept Claude-format `oauth: {clientId?, scopes?}` on HTTP entries; other `oauth`
keys ignored (not rejected), per native extras policy.

## 9. Out of scope (phase 1)

Pre-registered secrets and fixed callback port, CIMD, keychain/Vault storage, OAuth for SSE,
client-credentials grant, step-up UX beyond the SDK default, borrowing native clients' tokens.

## 10. Acceptance

1. Add Suno → listed as needs-login; not in the Agent prompt index.
2. Log in from localhost UI → completes without paste; tools published; a call succeeds.
3. Log in from Avibe Cloud UI → direct callback on the Cloud origin completes.
4. Log in from `http://<LAN IP>:5123` → paste mode; pasting the address-bar URL completes.
5. `vibe mcp suno --login` / `--login-complete` work from CLI and from an Agent in IM.
6. Expired access token refreshes transparently, including after a Controller restart and on a
   Session connection held open past expiry; revoked refresh token → `auth_required`, status
   `needs_login`; token-endpoint outage → `connection`, tokens kept.
7. N concurrent calls across Sessions on an expired token cause exactly one token-endpoint request;
   a rotated refresh token is persisted before use.
8. `--logout` / `--remove` delete `oauth.json`; tokens never appear in logs, list, help, or API.
9. Tests: hermetic loopback AS + MCP fixture covering 401→login→call, redirect-mode selection
   table, per-redirect client reuse, pasted-URL validation, refresh-failure mapping, restart-then-refresh, 401-resend-once.
