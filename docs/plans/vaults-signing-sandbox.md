# Vaults protected-tier signing sandbox

**Goal.** Let protected-tier keys (sealed under the browser VMK) sign/decrypt
**without exposing the raw private key to the main app's JS context** — the
"browser signing sandbox" deferred as §15.1 #5 / task #14. Owner (2026-06-29)
asked to stop deferring it and land it alongside the create-form work (#701).

## What already exists (do NOT rebuild)
- **Browser crypto** (`ui/src/lib/vaultCrypto.ts`, #674): `openProtected`,
  `unwrapVmk`, `unwrapProtectedDek`, `signProtectedDigest`, `releaseProtectedDek`,
  passkey-PRF + password VMK unwrap. The crypto to unseal + sign is done.
- **Backend sign-request flow** (`vibe/api.py` `vault_sign`, `storage/vault_service.py`):
  protected keypair → `create_sign_request` → browser returns a signature →
  `complete_sign_request`. The request/return plumbing is done.
- So protected signing is *functionally* reachable today by calling
  `signProtectedDigest` in the page. **The missing piece is isolation**, nothing else.

## The actual hard part: isolation vs passkey origin-binding
The sandbox's only job is **origin isolation** — run the unseal+sign in a context
the main page cannot read, so a compromised/XSS'd main app (or injected content)
can't scrape the key while it's unwrapped. The collision:

- **Real isolation needs a distinct origin.** Today everything is one origin
  (single FastAPI app; Show Pages are same-origin iframes). There is no second
  origin to put the sandbox in.
- **passkey-PRF is bound to the origin's RP ID.** A passkey created on the main
  origin **cannot be used from a different-origin sandbox** unless that origin is
  an authorized *related origin* of the main RP (WebAuthn L3 Related Origin
  Requests, via `/.well-known/webauthn`). Password unlock has no such binding.

So the isolation strength is forked by how we get the second origin. Key fact
that reshapes this: **WebAuthn RP ID ignores port and scheme** — a passkey with
RP ID `localhost` works across `localhost:5123` and `localhost:5999` (different
*origins*, same RP ID). So a **second local port** gives real origin isolation
*and* keeps passkey working, with **zero cross-repo infra**.

| Option | Second origin | Isolation | Passkey signing | Reaches via tunnel? | Cost |
|---|---|---|---|---|---|
| **A. Opaque iframe** (`sandbox="allow-scripts" srcdoc`) | opaque | real vs main page | **password only** (no RP ID in opaque origin) | n/a | frontend only |
| **B-local. 2nd daemon port** | `localhost:PORT2` | real vs main page | **password + passkey** (RP ID `localhost` is port-agnostic) | **no** — localhost-direct only | daemon + frontend, no cross-repo |
| **B-full. 2nd tunnel subdomain + `/.well-known/webauthn`** | real subdomain | real vs main page | **password + passkey** | **yes** | cross-repo: daemon + cloudflared + avibe-bot-backend |

## Recommendation — sequence B-local → B-full (skip A)
- **Start B-local now.** It delivers the real security promise (isolation +
  passkey) for localhost-direct use with **no cross-repo dependency**, so it
  unblocks immediately and proves the sandbox end-to-end. This is the bulk of the
  work (sandbox app, postMessage client, create + consumer wiring) and it's all
  reusable by B-full.
- **B-full is the same sandbox app reached over the tunnel** — it only adds a
  second subdomain routed to the daemon + a `/.well-known/webauthn` related-origins
  entry so the tunnel passkey RP authorizes the sandbox origin. That's the one
  cross-repo piece (avibe-bot-backend + cloudflared); it lands as a follow-on so
  remote (IM/phone) protected signing works too.
- **Skip A** — it can't isolate the passkey path, which is the secure default.

Scope guardrail: **WalletConnect / hardware wallets stay the deferred `external`
provider** (DESIGN §, plan §15.1) — not part of this. This builds the *local*
browser-signing sandbox only.

## Division of work
- **Codex (backend / infra):**
  - *B-local (now):* serve the sandbox app on a **second daemon port** (its own
    locked-down origin, strict CSP, no main-app assets); reuse the existing
    `vault_sign` request/`complete_sign_request` flow. Own PR + Codex review loop.
  - *B-full (follow-on):* a second tunnel subdomain → daemon + a
    `/.well-known/webauthn` related-origins entry (avibe-bot-backend + cloudflared).
- **Me (frontend):** the sandbox app (origin-checked postMessage `{record,
  wrap_meta, digest, scheme}` → in-sandbox unlock → `signProtectedDigest` → return
  only the signature), the main-app client (hidden iframe + request/response), and
  wiring into (a) protected signing-key creation (enable the disabled tier) and
  (b) the consumer-surface protected sign path. Reuse vaultCrypto + the design's
  dedicated passkey dialog (⑥/⑦).

## Owner checkpoint (non-blocking)
B-local needs **no** owner decision — it's daemon + frontend only — so it starts
now. The only thing to confirm is **B-full**: it adds a second tunnel subdomain +
WebAuthn related-origins, which touches **avibe-bot-backend + cloudflared**.
Confirm that's acceptable and Codex picks up the tunnel track so remote (IM/phone)
protected signing works; otherwise B-local stands and tunnel-protected signing
waits. Lands as its own PR alongside #701.
