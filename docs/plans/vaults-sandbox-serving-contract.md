# Vaults protected signing sandbox serving contract

This is the B-local serving contract for the protected-tier Vault signing
sandbox. It intentionally does not add tunnel, cloudflared, or
`/.well-known/webauthn` behavior; those are B-full follow-ups.

## Origin

- Main app: `http://localhost:5123` by default (`ui.setup_port`).
- Sandbox app: `http://localhost:5124` by default (`ui.vault_sandbox_port`).
- The daemon binds the sandbox listener to loopback only. A different port is a
  different web origin, while WebAuthn RP ID `localhost` remains port-agnostic.
- Passkey-capable B-local flows should open/embed using `localhost`, not raw
  `127.0.0.1`, because browsers reject IP-address RP IDs for this path.

## Build Contract

- Sandbox source root: `ui/sandbox/`.
- Vite config: `ui/vite.sandbox.config.ts`.
- Build command: `cd ui && npm run build:sandbox`.
- Build output: `ui/dist-sandbox/`.
- The packaged wheel/sdist includes `ui/dist-sandbox` as `vibe/ui/dist-sandbox`,
  alongside the main app's `ui/dist`.

The backend currently ships a minimal placeholder sandbox entry so the build and
packaging contract is real before the gatekeeper lands the frontend app. The
frontend owner should replace `ui/sandbox/src/main.tsx` with the postMessage
signing sandbox and keep the output path and build script unchanged.

## Serving and CSP

The sandbox origin serves only the sandbox bundle. It does not expose main-app
assets, routes, or `/api/*`. The main app remains the only caller of
`POST /api/vault/sign`; the sandbox receives `{record, wrap_meta, digest,
scheme}` through postMessage and returns only the signature through postMessage.

Sandbox CSP:

```text
default-src 'none';
script-src 'self' 'wasm-unsafe-eval';
style-src 'self';
img-src 'self' data:;
font-src 'self';
media-src 'none';
connect-src 'none';
worker-src 'self';
child-src 'none';
frame-src 'none';
manifest-src 'none';
object-src 'none';
base-uri 'none';
form-action 'none';
navigate-to 'none';
frame-ancestors http://localhost:5123
```

`'wasm-unsafe-eval'` is present for the browser-side KDF/crypto wasm path. There
is no inline script permission and no network egress (`connect-src 'none'`).
