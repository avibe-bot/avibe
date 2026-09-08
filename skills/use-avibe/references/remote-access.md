# Avibe Cloud Remote Access

### Remote access (Avibe Cloud)

These endpoints drive the managed `avibe.bot` tunnel that exposes the local Web UI to other devices. They are paired with the `remote_access.vibe_cloud` block under `/config`.

- `GET /api/remote-access/status`
  - returns `enabled`, `paired`, `public_url`, `running` (tunnel up), `pid`, `pid_state`, plus `binary_found` / `binary_path` / `binary_version` for the resolved `cloudflared` executable. Use `running: true` to assert the tunnel is up.
- `POST /api/remote-access/vibe-cloud/pair`
  - payload: `{"pairing_key": "vrp_..."}`
  - exchanges the one-time key for an OIDC client, tunnel token, and persists the full `remote_access.vibe_cloud` block; on success Avibe launches the cloudflared tunnel
- `POST /api/remote-access/start`
  - payload: `{}`
  - starts the cloudflared tunnel using the persisted pairing config
- `POST /api/remote-access/stop`
  - payload: `{}`
  - stops the cloudflared tunnel; configuration is preserved so `start` can resume later
- `POST /api/remote-access/optimize-route`
  - payload: `{}`
  - starts one guarded make-before-break route evaluation; it does not override a pinned protocol
- `GET /api/remote-access/network-interfaces`
  - returns currently assigned, up, non-loopback local source addresses that may be selected for cloudflared
- `POST /api/remote-access/settings`
  - payload: `{"transport_protocol":"auto|quic|http2","auto_recovery":true,"optimization_profile":"stable|balanced|low_latency","edge_ip_version":"4|auto|6","edge_bind_address":""}`
  - applies policy-only changes immediately; Connector-affecting changes start and verify a replacement before draining the previous Connector, and keep the previous persisted settings when the replacement cannot become ready
- `POST /api/remote-access/diagnostics`
  - payload: `{}`
  - returns bounded DNS, TCP/HTTP2, and observed-active-QUIC reachability without exposing probe targets or local interface addresses
- `GET /auth/callback`
  - OIDC redirect target used by avibe.bot during sign-in. Browser-driven; do not call directly from automation.
- `GET /api/session`
  - always returns `200` with an auth-state payload, never `401`. Three shapes: `{"remote": false}` when remote access is not configured for this request, `{"remote": true, "authenticated": false}` when remote access is on but the caller has no valid session cookie, and `{"remote": true, "authenticated": true, "email": "..."}` when signed in. Check `authenticated` to gate behavior; do not poll for HTTP status codes.
- `POST /auth/logout`
  - clears the avibe.bot session cookie on this device. Does not stop the tunnel.

The session cookie is bound to the tunnel session and expires after roughly 24 hours; the server slides the TTL when activity reaches the half-life. Do not invent custom auth headers — rely on the existing cookie issued by `/auth/callback`.

Treat `tunnel_token`, `instance_secret`, `session_secret`, and `client_id` from `remote_access.vibe_cloud` as opaque secrets.

### Pair Avibe Cloud remote access

Goal: connect the local Web UI to `avibe.bot` so it is reachable from another device.

1. The user signs in at `https://avibe.bot`, creates a remote-access bot, and copies the one-time pairing key (format `vrp_...`).
2. Call `POST /remote-access/vibe-cloud/pair` with `{"pairing_key": "vrp_..."}` from the local Web UI origin.
3. Verify with `GET /remote-access/status` — `enabled: true`, `paired: true`, `public_url` populated, `running: true`.
4. Have the user open `public_url` and sign in with the same avibe.bot account.

Alternatively, drive the same flow from the CLI:

```bash
vibe remote                       # guided flow
vibe remote pair vrp_abc123       # paste key directly
vibe remote status --json         # inspect tunnel state
vibe remote stop                  # stop tunnel; keep config
vibe remote start                 # bring tunnel back up
```

Treat the pairing key, tunnel token, instance secret, and session secret as opaque. Never echo them in chat replies.
