# Custom Hostname Heartbeat Plan

## Background

Avibe Cloud can route both the managed instance hostname and active custom
hostnames through the same Cloudflare tunnel. The local UI currently recognizes
only the managed `public_url` host and always uses the paired OAuth
`redirect_uri`, so custom-host traffic is rejected or redirected away from the
originating hostname.

## Goal

- Consume the heartbeat response's `active_hostnames` snapshot without breaking
  compatibility with older backends.
- Authorize each exact active hostname through the existing remote-access
  authentication and origin checks.
- Keep OAuth authorization, code exchange, and the final browser navigation on
  the hostname that started the flow.

## Solution

1. Normalize successful heartbeat snapshots, replace a process-local cache under
   a lock, and atomically persist the snapshot with its instance id and receive
   timestamp under Avibe's state directory.
2. Resolve allowed remote hosts as the valid `public_url` hostname plus the
   current instance's cached snapshot. Reuse that exact-match decision for HTTP,
   CSRF origin calculation, and authenticated WebSocket paths.
3. Select `https://{request_host}/auth/callback` only for an allowed host. Store
   it in both OAuth handshake representations and pass that same value to the
   token exchange after callback validation.
4. Cover normalization, legacy responses, replacement/removal, persisted
   startup behavior, host rejection, redirect selection, and exchange binding.

## Validation

- `tests/test_remote_access_vibe_cloud.py` and
  `tests/test_ui_remote_access_auth.py`: 251 passed.
- Adjacent mutation, Show Runtime, and terminal modules: 239 passed.
- Ruff checks for all changed Python files: passed.
