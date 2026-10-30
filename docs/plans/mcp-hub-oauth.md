# MCP Hub OAuth

> Status: spec, owner decisions recorded 2026-10-30; design review (advisor, codex-astra) folded in §11.
> Extends `docs/plans/mcp-hub-mvp.md`.
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
  lands on an unreachable page; paste mode tells the user to copy that address-bar URL
  and paste it into the dialog. This is the Claude Code / Codex `--no-browser` fallback.
- **CLI/IM start** (no browser origin): Avibe Cloud `public_url` when remote access is enabled,
  else paste mode. The CLI always prints the URL and accepts a pasted callback.

Consequences:

- **Origin derivation** reuses `_current_origin()`; no new trust in `Host`/`X-Forwarded-*`.
- **One stored client.** Storage returns the saved DCR client only when its `redirect_uris`
  contains this login's URI; otherwise it returns none, the SDK re-registers, and the new client
  replaces the old one. A redirect change always comes with a new login, so no client list.
- **Callback auth = the pending `state`.** `GET /api/mcp/oauth/callback` is exempt from the UI
  session, like the existing Cloud `/auth/callback` (`vibe/ui_server.py` `_remote_auth_exempt_path`
  and its unauthenticated rate limit): a login started from CLI/IM, or opened on another device,
  has no browser session. The route only forwards `code`/`state`/`iss` over IPC; the runtime
  accepts it only for a live pending login with that exact `state` (SDK-generated, 32 bytes), and
  the PKCE verifier never leaves the runtime. Unknown/expired/used `state` → generic error page.
  Starting, pasting and logging out stay Owner-only.
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
call → shared per-server provider (§5) attaches Bearer per request, refreshes before expiry
     → refresh token rejected → envelope error.type = auth_required, hint `vibe mcp suno --login`
```

- The SDK's grant suspends inside `callback_handler`; it awaits a Future (with
  `anyio.fail_after(600)`) that the callback/paste IPC resolves. Pending logins are a
  `dict[state → Future]` in the **MCP runtime loop** (`core/mcp_hub/runtime.py`; the Future must be
  created on that loop), single-use, one per server (a new start cancels the previous).
- **needs_login fits the publish model** (`_refresh_locked` currently unlists on failure, and
  `summarize`/`list_packages`/prompt read published snapshots): a 401 carrying an OAuth challenge
  publishes a **zero-tool snapshot whose manifest has `auth: "needs_login"`**. Lists show it with a
  login action; `core/mcp_hub/prompt.py` skips it; calls return `auth_required`. A successful
  login republishes normally.

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

Reuse the SDK provider for everything; fix its three restart gaps in a thin subclass.

- `AvibeOAuthProvider(OAuthClientProvider)`, **one instance per server**, shared by every HTTP
  connection of that server (`_client_for` passes it as `auth=` to `create_mcp_http_client`).
  Its `context.lock` already serializes the auth flow, so N concurrent requests cause one refresh;
  a provider is an httpx `Auth`, so the token is attached per request and long-lived Session
  connections pick up refreshed tokens.
- Override `_initialize()` to restore from `oauth.json`: tokens, client, absolute `expiresAt` →
  `context.token_expiry_time`, AS metadata → `context.oauth_metadata`, PRM →
  `context.protected_resource_metadata`. Proactive refresh then uses the real `token_endpoint`
  (SDK refreshes when expired; the SDK's own RFC 6749 §6 carry-forward of `refresh_token`/`scope`
  and save-before-use apply as is).
- `TokenStorage.set_tokens` writes absolute `expiresAt` (from `expires_in`) plus the current AS
  metadata / PRM via `write_atomic` (0600). A write failure is logged and raised; no retry state.
- Override `_handle_refresh_response`: on `400 invalid_grant` / `invalid_client` / `401` from the
  token endpoint, also delete the tokens from `oauth.json` (keep the client), so the next call does
  not hit the token endpoint again; status becomes `needs_login`. Network / `5xx` / `429`: keep tokens.
- Call-time providers have a `redirect_handler` that raises `McpHubError("auth_required")`. A 401
  at call time (revoked or unrefreshable token) therefore ends as `auth_required` with the hint
  `vibe mcp <name> --login`; **no automatic replay of `tools/call`** (a 401 is not guaranteed to
  precede side effects). The pre-dispatch `Session not found` retry in `runtime.py` is unchanged.
- No background timer, no owned refresh task, no write-retry, no token revocation at logout
  (phase 1 `--logout` deletes `oauth.json`), no rejected-token memory, no cross-process lock
  (only the Controller's MCP runtime touches `oauth.json`).
- These overrides touch SDK private members; `mcp` is pinned `<3`, and the hermetic test
  (§10) exercises restart → refresh so an SDK change fails CI rather than users.
- Logs carry status and OAuth `error` code only, never token values.

## 6. Storage

`<avibe-home>/mcp/<name>/oauth.json`, mode 0600, `write_atomic`:

```json
{"schemaVersion": 1, "serverUrl": "https://api.suno.com/mcp",
 "tokens": {"access_token": "…", "refresh_token": "…", "scope": "…", "token_type": "Bearer"},
 "expiresAt": 1793345230,
 "client": {"client_id": "…", "redirect_uris": ["…"], "issuer": "…"},
 "authServerMetadata": {"issuer": "…", "token_endpoint": "…", "…": "…"},
 "resourceMetadata": {"resource": "https://api.suno.com/mcp", "authorization_servers": ["…"]}}
```

`tokens` / `client` / metadata are the SDK models' own JSON (`OAuthToken`,
`OAuthClientInformationFull`, `OAuthMetadata`, `ProtectedResourceMetadata`).

- Not in `definition.json` or `revisions/`; never printed by list/help/API.
- Ignored (treated as absent) when `serverUrl` differs from the definition URL, so a changed
  native URL never receives the old bearer token.
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
`{authorizeUrl, mode}`; `GET /api/mcp/oauth/callback` (session-exempt, §3); `POST /api/mcp/{name}/oauth/complete`
`{callbackUrl}`; `POST /api/mcp/{name}/oauth/logout`. Summary gains `auth: none|needs_login|ok`.

UI: needs-login rows show a login action. Direct mode navigates the current tab to the authorize URL; the callback returns to the MCP page. Paste mode shows a small dialog with an authorize button and callback URL input. Strings via i18n.

Definition: accept Claude-format `oauth: {clientId?, scopes?}` on HTTP entries; other `oauth`
keys ignored (not rejected), per native extras policy.

## 9. Out of scope (phase 1)

Pre-registered secrets and fixed callback port, CIMD, token revocation at logout, keychain/Vault storage, OAuth for SSE,
client-credentials grant, step-up UX beyond the SDK default, borrowing native clients' tokens.

## 10. Acceptance

1. Add Suno → listed as needs-login; not in the Agent prompt index.
2. Log in from localhost UI → completes without paste; tools published; a call succeeds.
3. Log in from Avibe Cloud UI → direct callback on the Cloud origin completes.
4. Log in from `http://<LAN IP>:5123` → paste mode; pasting the address-bar URL completes.
5. `vibe mcp suno --login` / `--login-complete` work from CLI and from an Agent in IM.
6. Expired access token refreshes transparently, including after a Controller restart and on a
   Session connection held open past expiry; rejected refresh token → tokens deleted, status
   `needs_login`, call `auth_required`; token-endpoint outage → tokens kept.
7. N concurrent calls across Sessions on an expired token cause exactly one token-endpoint request;
   a rotated refresh token is persisted before use. A call-time 401 never replays `tools/call`.
8. `--logout` / `--remove` delete `oauth.json`; tokens never appear in logs, list, help, or API.
9. Tests: hermetic loopback AS + MCP fixture covering 401→login→call, redirect-mode selection
   table, per-redirect client reuse, pasted-URL validation, refresh-failure mapping, restart-then-refresh, session-exempt callback state check.

## 11. Design review (2026-10-30)

advisor and codex-astra both returned approve-with-cuts. Applied:

- Reuse the SDK provider for refresh via `_initialize` / `_handle_refresh_response` overrides
  instead of a separate token manager (advisor; codex-astra had judged a manager necessary only
  because the SDK does not restore expiry/metadata, which the override solves).
- Cut: owned refresh task, write-failure retry, logout revocation, compare-before-retire,
  rejected-token memory, 401 resend, per-redirect client list.
- Fixed: callback must be session-exempt and bound by `state` (codex-astra); pending logins live on
  the MCP runtime loop and needs_login is a zero-tool published snapshot (advisor).
- Kept against one cut proposal: `serverUrl` binding (codex-astra) — one comparison prevents
  sending a bearer token to a changed URL.

