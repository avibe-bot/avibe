# Protected Vault sandbox integrity

Status: implemented source contract; publication and runtime rollout require
separate security authorization. This document owns the Avibe consumer boundary;
`vault-sandbox/docs/integrity.md` owns source builds and static release retention.
The historical crypto model and accepted central-host residual remain in
`vault-crypto-sandbox.md` §3/§10. The v2/record/signing contract is unchanged.

## Authority and lifecycle

`vibe/data/vault_sandbox_release.json` is the one packaged trust pin, generated
from a locally reproduced sandbox snapshot. The UI imports it and Python reads it
for the exact `/v/<source-input-sha256>/` frame CSP. The CSP preserves `'self'`
for Show/file frames, but never broadens sandbox access to its entire origin or
a retained older version. Docker explicitly copies this shared JSON before the
UI build. The stable origin/RP is still `https://sandbox.avibe.bot`.

Each new frame and parent-created authorization window independently verifies the
exact manifest SHA-256 and every runtime resource before navigation. Requests use
CORS, omitted credentials, no-store and redirect:error; response URLs must match
exactly. HTTP/CORS/network/parse/identity/hash failures and unavailable Web Crypto
fail closed. No live manifest can become the expected pin. There is no opt-out,
mutable/latest fallback, standard-tier downgrade or successful trust cache.

An approval gesture synchronously reserves only `about:blank`. After its own
verification, the popup navigates to the exact pinned document with routing in a
fragment; the sensitive RPC awaits that verification and checks that its handle
has not been closed/reset. Blocked popup creation retains the existing sandbox
launcher path, not an unverified URL. Reset aborts in-flight fetches and closes
only owned frames/windows/listeners. A stale generation cannot be adopted or
clear a newer singleton. Caller cancellation is retryable `sandbox_reset`, not
an integrity alarm. Ready and handshake must both report the pinned identity;
handshake must explicitly accept the v2 session/origin before ordinary RPC.

The verified sandbox creates its own setup/auth windows only at its compiled
same-release document path, with fragments and matching-build pairing. These
sandbox-owned clicks do **not** repeat parent preflight. Every loader enforces
browser SRI for the single import-free JS bundle and CSS. The runtime refuses
startup (including top-level ceremonies) until its SRI stylesheet loads, with a
ten-second load/error/unload deadline: WebKit can start a module ahead of delayed
CSS even when that stylesheet is in `head`.
The sandbox keeps `connect-src 'none'` and `worker-src 'none'` on honest serving.

**Residual:** the browser does not enforce document SRI on iframe or popup
navigation. A malicious central host can equivocate between verification fetches
and navigation, replace the loader and its headers, and even permit a persistent
Service Worker. This change bounds accidental drift and asset substitution; it
does not eliminate that accepted central-host trust. Build identity is a
compatibility/diagnostic gate, not attestation.

## Release order and compatibility

1. In the sandbox repository, use Node 22.23.4/npm 11.6.2 and its committed lockfile.
   Stage source inputs; `npm run release:stage` builds twice and retains one
   source-derived candidate. `release:guard -- origin/main` rejects existing-byte
   changes/deletions and extra unpublished candidates. `npm run build` reproduces
   the current snapshot twice and assembles all reviewed retained versions.
2. Generate this repository's JSON using
   `npm run release:pin -- /path/to/avibe/vibe/data/vault_sandbox_release.json`.
   Copy the exact retained current version into `ui/e2e/vault-integrity/release/`
   and the hosting config into its sibling `hosting.json`. Never use production
   downloads to choose a pin. The fixture is actual release code with provenance,
   not a mock sandbox or an unmerged v2 wire dependency.
3. Both PRs require exact-head review and CI. Sandbox `main` auto-deploys Vercel
   Production, so merging it needs explicit security/deployment authorization.
   This task creates PRs only, not a deployment or a local runtime update.
4. After separately authorized publication, verify equality of production bytes,
   exact paths/no redirects, manifest, SRI/CSP/CORS, retained older assets and
   legacy root. Only then ship Avibe with the new pin. Early shipment fails
   closed rather than trusting another version.

The compatibility root is frozen from reviewed sandbox #25 source
`415435548e63ae08942c5b26b0844d5504824d6f`, independently reproduced rather than
copied from a host. It still reports `dev` and serves old query routing so existing
parents/keys/records are not stranded. New enforced parents never use it. Old
installs cannot gain this enforcement retroactively. A future deliberate
revocation must fail closed or ship an approved replacement pin, never mutate
an immutable path or silently relax verification. This task removes/revokes no
published release and changes no user's keys.

## Evidence and scope

- `vaultSandboxIntegrity.test.ts`: actual pinned fixture hashes, malformed/stale/
  tampered/missing resources, redirects/origin/CORS/network, unavailable crypto,
  build/handshake mismatches, reset/generation races and popup cleanup.
- `test_vault_rest.py`: the exact packaged version-path CSP, including replacement
  of a broad existing policy, while preserving existing RP delegation.
- UI consumer tests: actionable EN/ZH integrity errors, existing cancellation,
  expiry and permission-denial presentation preserved.
- `npm run test:vault-integrity`: real client plus actual source-built sandbox in
  isolated Chromium/WebKit, parent preopen and sandbox-owned setup/auth, native
  SRI tampering *after* successful preflight, delayed/error/never-loaded CSS,
  wrong-build pairing and cleanup. All external network is blocked; credential
  APIs are denied. Synthetic public metadata only; no approval, signing,
  decryption, real passkey, Vault call or funds.

Offline browser tests do not claim a live deployment, physical iOS/PWA user-
activation acceptance or a real passkey ceremony. Those are separate release
checks after explicit authorization. Existing dependency-audit findings in the
sandbox's development toolchain are not silently fixed by integrity pinning.
