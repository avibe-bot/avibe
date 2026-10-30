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
call → shared provider adds Bearer, refreshes before expiry
     → refresh rejected → envelope error.type = auth_required, hint `vibe mcp suno --login`
```

- The SDK's grant suspends inside `callback_handler`; it awaits a Future that the callback/paste
  IPC resolves. Pending logins are in controller memory, single-use, 10-minute TTL, one per
  server (a new start cancels the previous).
- Call-time connections use a provider whose `redirect_handler` raises immediately, so a 401 at
  call time never blocks waiting for a person; it maps to `auth_required`.
- One provider + one storage per server is shared by all connections, so concurrent refreshes
  cannot rotate the refresh token out from under each other.

## 5. Storage

`<avibe-home>/mcp/<name>/oauth.json`, mode 0600, `write_atomic`:

```json
{"schemaVersion": 1, "serverUrl": "https://api.suno.com/mcp",
 "tokens": {"access_token": "…", "refresh_token": "…", "expires_at": 0, "scope": "…"},
 "client": {"client_id": "…", "redirect_uris": ["…"], "issuer": "…"}}
```

- Not in `definition.json` or `revisions/`; never printed by list/help/API.
- Ignored (treated as absent) when `serverUrl` differs from the definition URL.
- Deleted on `--remove` / hide, and by `--logout`.
- Native-source OAuth servers get their own Avibe login; native token stores are not read.

## 6. Precedence

A definition with an explicit `Authorization` header never uses OAuth. Otherwise HTTP servers
attach the OAuth provider; a server that never returns 401 never triggers it.

## 7. Surfaces

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

## 8. Out of scope (phase 1)

Pre-registered secrets and fixed callback port, CIMD, keychain/Vault storage, OAuth for SSE,
client-credentials grant, step-up UX beyond the SDK default, borrowing native clients' tokens.

## 9. Acceptance

1. Add Suno → listed as needs-login; not in the Agent prompt index.
2. Log in from localhost UI → completes without paste; tools published; a call succeeds.
3. Log in from Avibe Cloud UI → direct callback on the Cloud origin completes.
4. Log in from `http://<LAN IP>:5123` → paste mode; pasting the address-bar URL completes.
5. `vibe mcp suno --login` / `--login-complete` work from CLI and from an Agent in IM.
6. Expired access token refreshes transparently; revoked refresh token → `auth_required`.
7. Concurrent calls across sessions never lose the token (single refresh).
8. `--logout` / `--remove` delete `oauth.json`; tokens never appear in logs, list, help, or API.
9. Tests: hermetic loopback AS + MCP fixture covering 401→login→call, redirect-mode selection
   table, per-redirect client reuse, pasted-URL validation, refresh-failure mapping.
