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

## Registration identity compatibility

The registration validation introduced in sandbox #36 remains in the pinned
successor: it validates supplied WebAuthn registration `user.id` encodings before
credential creation. The daemon's per-instance registration ID travels through
the existing `authzCreationOptions`
field; it is separate from `vault_user_handle` scope/AAD. Legacy registration IDs
remain accepted and omitted options retain the historical compatibility path.
No credential, RP, root metadata, unlock/sign policy or protocol migration is
introduced. The previous immutable e7be runtime already transports valid new IDs;
the successor adds validation, not a capability gate. The owning identity contract
is in `vault-crypto-sandbox.md` and `vault-sandbox/docs/integrity.md`. Reproduction,
fixtures and public hosting equality do not prove real provider coexistence,
passkey ceremonies or transaction acceptance.

## RC13 display-only consumer selection

The packaged pin and paired release fixture select sandbox #37's immutable build
`2e88ccc707cd0eeeedacf77e87ae80b8e8a14dc769aab6eaf736d7860835e514`,
from merged source `6edd2e1406008bc9a4665c82d706d630bdc5d464` (tree-equal
reviewed head `7970b211a9c2219f5202977a09e5c58e55563443`). The official
source-derived pin is shared by the UI verifier and daemon CSP; live publication
is checked for equality, never used to choose the authority.

This selects the reviewed Bitcoin payment, EVM/EIP-712 and common signing-card
presentation changes, plus the exact System `AdvanceNonceAccount` decoder.
The nonce review shows only the nonce account and authority encoded in the
message; it does not verify the stored nonce value, current on-chain authority,
execution success or expiry. Other valid undecoded instructions remain raw with
a blind-signing notice. Transfer/nonce summaries do not resolve lookup tables or
invent chain state. Bitcoin requester-selected network labels and EVM chain
names are not network proof, and typed-data warnings are not contract-behavior
verification. See the sandbox README for the owning display/decoder contract.

No policy v1.1 authorization, protocol, key-custody, signing bounds, fallback or
capability expansion is included. Source/fixture checks do not establish a real
ceremony or protected transaction acceptance. The fresh pin requires its own
exact-head approval before merge; installation and final protected acceptance
remain separately authorized cohort gates.

## Signing-policy turn-off consumer selection (contract v1.1)

The packaged pin and paired release fixture select sandbox #39's immutable build
`fc013b755a005b6b48dc750c429c46e9ee1401532d944c671207f85e371e7b7a`,
from merged source `7e2fb00c687532cd2e40ded5f797c9ff8b680213` (tree-equal
reviewed head `db76ed326b4cf40f741bb54ad071eb9c05cc82c8`). As before, the pin is
derived from source and the live publication is only checked for equality.

This build adds one optional operation, `authorizeSigningPolicyDowngrade`: the
sandbox shows the exact server-issued context in its own top-level window and
returns a fresh WebAuthn assertion over it. The Web consumer calls it only when
the pinned build lists it in its capabilities (never in the globally required
operations), opens the window in the click, and refuses to continue without it;
the daemon verifies the assertion and applies only the bound `always_ask`
true-to-false change. Turning ask-before-signing on, every other operation and the
protected-record, root and key-custody formats are unchanged. A Passkey set up
before public-key registrations were stored cannot authorize this and is not
re-registered; there is no repair path. Source/fixture checks do not establish a
real ceremony; the joint browser exercise is a separate gate.

## Protected-HD secure-window release selection

The current packaged pin and paired release fixture select sandbox #43's build
`f861216ffb5d366cc962fac8651da691434194c5b5459af8e249df0b956e1fa5`,
from merged commit `8f1ac62b11ab6a133e7874b0520aeed1d00c44d4` (tree-equal
reviewed head `df64d4361454b3875c7d0d2c52fa1384f6d1f801`, source-input snapshot
from `ece87517d341a682cf6c5037f67b4285943ec253`). The owning release tool derives
the complete pin from that reviewed Git tree and retained snapshot. Deployment
observations only confirm equality to those bytes; they never select authority.
The same JSON remains the Web verifier and Python frame-CSP authority.

This selects the reviewed secure-window/HD runtime while leaving the parent
consumer scope unchanged: the pin does not implement protected-HD API/storage,
creation UI, purpose selection or reconciliation. Those are separate changes,
not prerequisites copied into this pin-only update. It adds no protected Bitcoin
Legacy/Native input support. Existing ordinary operations, signing-policy v1.1,
RP ID/origin, root metadata, credential and encrypted-record formats retain their
existing contracts; no migration, custody fallback or feature flag is introduced.
The frozen compatibility root is unchanged and earlier versioned releases remain
retained by the sandbox owner.

Focused source/fixture checks and publication equality do not prove a real
passkey ceremony or the composed HD flow. Authorized composed regression remains
a separate gate before the coordinated merge/rollout decision. This pin-only
change does not authorize installation, a service restart or feature enablement.

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

## Protected Solana backend integration boundary (held Avibe slice)

The Avibe Python API/service/storage path accepts the sandbox-generated opaque
protected Ed25519 record without opening it. The record name is the outer DB
identity; value-free `wrap_meta.record_meta` must be exactly a keypair with
curve `ed25519` and the lower-case 32-byte public key matching the request's
pinned metadata. Python does **not** authenticate the envelope's AAD, derive a
DEK, generate/import a seed, call `avault`, or treat a standard keypair spec or
blind-box import as a protected fallback. The sandbox authenticates the name
through the existing v2 AAD at decrypt/key use.

Protected pending signing is intentionally narrow. The request must use
`scheme: "ed25519"` and the exact non-empty, lower-case, even-hex
`{kind: "solana-transaction-v1", messageHex}` context matching outer
`message_hex`, bounded to 1 MiB before traversal or copies. The sandbox remains
the authority for canonical legacy/v0 Solana message parsing, packet bounds,
and required-signer checks; Python does not duplicate that decoder. The
persisted request is the sole binding source, including its server-derived
`signing_key` snapshot (record ID and public key); replacing either refuses
completion rather than adopting a new key. Caller delivery cannot populate that
snapshot. The signed v2 release is exactly
`{name, scheme, messageHex, publicKey, signingContext}` and every copy must
match the persisted record, message, scheme, and full context. Completion
accepts one 64-byte Ed25519 signature with absent/null `recovery_id` only after
expiry/state/replay and cryptographic verification against the persisted
message and record public key.

This backend slice does not add a UV assertion or claim that a returned public
signature proves a particular passkey ceremony. Fresh R3 UV, protected key use,
whole-operation challenge, sandbox parsing, and the authenticated v2 AAD remain
sandbox-owned. A valid signature is public proof over message and key only.
**A historical valid signature can settle another new,
unexpired pending request for identical key/message/context**, without a fresh
UV proof. This is explicitly exercised by the backend test; replaying the same
completed request still fails. The signature is not evidence that the envelope
was decrypted, the sandbox parser ran, or this particular ceremony happened.
The existing API's request-state and binding checks still
prevent a changed key/message/context/scheme, expired request, replayed request,
missing context, malformed message encoding, empty/oversize protected message,
or invalid signature from settling successfully.

Merging the backend slice while the currently packaged sandbox pin is installed
exposes structurally checked protected-record ingestion, creation of fully bound
pending requests, and **completion with an externally supplied valid public
signature**, subject to the checks above. The old pin still rejects Ed25519 and
`solana-transaction-v1` before protected key use, so it cannot produce a new
protected signature; the old UI cannot create or execute the protected Solana
flow. The pin is not an API-completion attestation gate. No dynamic capability/
version field, server feature flag, mutable resource fallback, pin change,
deployment, or downgrade is introduced. The
approved Avibe package plus its exact reviewed sandbox pin is the feature cohort.
A later rollout must ship the compatible sandbox parser/key-use release, refresh
the package pin, and have the protected UI consume the exact request/release
wire before user-facing support is enabled.

The held backend tests cover real Python API/service/SQLite paths for valid and
invalid protected metadata, no protected `avault` custody call, exact pending
binding and release shape, one valid completion, changed binding/expiry/replay,
missing context, empty/oversize input, invalid signatures, standard empty and
150-byte regressions, and unchanged legacy secp behavior. They do not claim
fresh WebAuthn UV or final sandbox parser/key-use acceptance; those belong to
the sandbox final-head review and coordinated release acceptance. The
`use-avibe-vault` Skill still does not advertise protected Solana support; Vault
request/provide keypair creation remains unsupported. This backend-only change
does not alter standard generation/import or legacy secp256k1 envelope/AAD bytes.
