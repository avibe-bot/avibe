# Editable signing confirmation policy

## Scope and trust boundary

Ordinary standard/protected local keypairs and standard HD accounts share the
ask-before-signing policy. Raising confirmation is ordinary metadata. Lowering
requires a fresh assertion by an existing Vault Passkey. First setup is
trust-on-first-use through the existing setup UX, without another owner anchor.

This guards ordinary or accidental lowering through normal API/CLI calls. It is
not isolation from a hostile process with the same OS user's shell/database
permissions. Standard confirmation protects against mistakes; funds belong in
the protected tier. Protected custody/signing still requires its existing browser
cryptography regardless of the flag. This action authorizes no signature or key
use. Signing retains capture-time linearization: changes affect subsequent
captures, not a custody operation already underway.

The enable-only stage is already on the base branch. The dedicated authorization
and durable-setup successor described here requires independent source review,
CI and authorized integration before shipment. Its sandbox version/publication
and Avibe pin are separate gates; this is not part of the RC13 display-only pin.
An old sandbox must leave lowering unavailable, with no fallback RPC.

## Tier parity

Owner rule, 2026-11-14. New signing capabilities (chains, address formats,
wallet types) are designed and shipped for the standard and protected tiers
together by default. If they must split, the plan states why and names the
version that ships the protected follow-up. The UI never shows an address the
protected key cannot spend.

A capability is done on the protected tier only with all of:

- typed sandbox decode and review, bound to the signed digest;
- daemon-side validation of the same request before key use;
- an independent adversarial review;
- a sandbox release, its Avibe pin and real acceptance on that pinned release.

Current gaps, target 3.2.7:

- Protected Bitcoin SegWit (P2WPKH) and Legacy (P2PKH) address types (owner
  decision 2026-11-14). Standard keys sign all three types; protected keys sign
  Taproot only, and Web offers protected keys only Taproot.
- The protected seed-phrase wallet (protected HD), contract in
  `vault-protected-hd-wallets.md`.

The frozen v1.1 downgrade context below carries all four secp addresses as
identity facts, and the typed popup lists them, including SegWit and Legacy
addresses of a protected key. The 3.2.7 address-type work must close this:
enable those types or stop displaying them for protected keys.

## Metadata editing

- `PATCH /api/vault/secrets/{name}` accepts top-level `always_ask: boolean`
  only for ordinary null/local keypair signers, standard or protected.
- HD uses `PATCH /api/vault/secrets/{name}/accounts/{account}`. The flag applies
  to the enabled shared index across all chains, never to an individual chain.
- False→true and unchanged saves remain available. Unproved true→false returns
  HTTP409 `vault_policy_authz_required`. Mixed metadata/downgrade requests fail
  atomically, including their description, name, tags and audit.
- Full `policy` updates cannot alter internal keys. Omission/null preserves them.
  Static secrets and row-wide HD policy do not acquire an ordinary-key editor.
- `_signing_policy_revision` is storage-owned: ordinary policy JSON or the HD
  account object. Absent means `legacy`; each real transition mints a random
  128-bit lowercase hex value. Unchanged saves retain it. Direct storage create
  rejects caller injection, not merely HTTP.

## Frozen v1.1 wire

Pure public validation/identity lives in `storage/vault_policy.py`; independent
cross-language vectors live in
`tests/fixtures/vault_signing/policy-downgrade-v1.json`. Changing producer and
consumer bytes requires a numbered contract revision and joint acknowledgement.

1. `POST /api/vault/secrets/{name}/signing-policy/challenge` accepts exactly
   `{always_ask:false,account?:integer}`. Account is required/enabled0..19 for HD
   and omitted for ordinary keys. It returns `{ok:true,context}` only when the
   current flag is true and eligible factors are available.
2. The successfully pinned sandbox must advertise
   `authorizeSigningPolicyDowngrade` in its operation capabilities. The RPC
   accepts exactly `{context}` and returns `{challengeId,assertion}`. The
   assertion is the actual serialized credential, not `confirmed`, a grant,
   an unlocked session or PRF output. No `deleteAuthzAssertion` fallback.
3. `POST /api/vault/secrets/{name}/signing-policy` accepts exactly
   `{context,authorization:{kind:"webauthn",challenge_id,factor_id,assertion}}`.
   It changes only the bound true→false flag. No metadata or bypass boolean.

The context has exact keys:

- `v:1`, `purpose:"vault-signing-policy-downgrade"`, server operation
  `challengeId`, 32-byte canonical padded-base64 `nonce`, and exact
  millisecond-UTC `expiresAt` with a120-second server lifetime;
- configured `rpId` and `origin`, never values taken from a request context;
- `target:{secretId,name,protection,signerKind,account,accountName,addresses,
  identitySha256}`; ordinary account/name are null, unnamed HD name is null;
- `change:{field:"always_ask",from:true,to:false,revision}`;
- all eligible factor descriptors `{factorId,credentialId}`, unique and sorted
  by ASCII factor ID. A registry credential outside the supported bound is an
  explicit refusal, not a reason to silently omit that factor.

Reject unknown/missing fields, noninteger numbers, booleans as integers, lone
surrogates and contexts over65,536 canonical UTF-8 bytes. IDs are1..80 ASCII
letters/digits/underscore/hyphen; credential IDs decode to1..1023 bytes and must
round-trip exactly as padded standard base64. HD names are null or1..80 Unicode
scalars. Do not truncate, trim or localize stored context facts.

`addresses` always has `eth`, `btc_legacy`, `btc_segwit`, `btc_taproot`, `solana`.
HD sets only EVM/Taproot/Solana; ordinary secp sets the four secp addresses;
ordinary Ed25519 sets only Solana. Nonnull values are canonical EIP55, mainnet
Base58Check P2PKH, lowercase mainnet Bech32 P2WPKH, lowercase mainnet Bech32m
P2TR, or canonical32-byte Solana Base58 respectively. Sandbox validates those
encodings; it does not possess the identity snapshot to independently derive
address↔key correspondence.

Identity hashes exact stored ciphertext/nonce/wrap-meta strings and updated_at,
tier/kind/signer, and the validated public keys for this target/account. Ordinary
secp casing is normalized; unknown metadata, description, usage and last signer
are excluded. Envelope revision can invalidate a context after sibling HD edits;
usage alone does not. No missing key or empty default identity is accepted.

Canonical JSON recursively sorts keys by UTF-16 units, retains array order, uses
lossless JSON scalar encoding and no Unicode normalization. The challenge is:

```text
SHA256(UTF8("avibe:vault:signing-policy-downgrade:v1\n" + canonical(context)))
```

The prefix has an actual LF. Storage operation is
`vault_signing_policy_downgrade_v1`; the existing verifier stores
`SHA256(challengeBytes)`, not a second context encoding.

Sandbox checks own origin/RP, expiry before render, immediately before the
credential call and before returning it. Remaining lifetime<=0 is expired;
>150000ms is clock skew; browser timeout is min(120000,floor(remainingMs)) with
no positive floor. Server expiry is final. UV/UP and the frozen allowlist are
required; this operation neither unlocks a VMK nor requests PRF.

The legacy arbitrary-challenge assertion RPC means an assertion cannot prove
which popup displayed consent. Domain separation binds the server operation,
not a hardware-attested UI path. The honest client must use the named, typed,
same-origin/same-build popup with a valid visible surface and fixed action text.

## Storage verification and atomicity

Completion reserves the writer before reading target/challenge/factor state.
The same transition owner serves ordinary metadata, HD metadata and verified
completion; it verifies the supplied proof itself rather than accepting a
caller-selected authorization result.

Checks include management ACL, route/name/immutable ID, kind/tier/enabled account,
rederived entire target/identity, current true and revision, context schema and
digest, operation/expiry/unconsumed challenge, frozen/current factor membership,
credential bytes, active status, configured RP/origin, existing user-handle
compatibility, signature, UP/UV and counter. Validity is checked again after
verification so crossing expiry cannot commit a result.

Flag/revision, factor counter, one-shot consumption and success audit commit
together. A savepoint also protects callers that catch a failed completion
inside their outer transaction. There is no browser/custody call under the
lock and no success response before outer commit. Two simultaneous completions,
two challenges for one revision, ABA, deletion/recreation and rotation cannot
reuse stale proof. Later factor enrollment cannot enlarge a frozen allowlist.
No restoration, factor reset or credential-replacement feature is added.

Errors: malformed request/context400; missing proof409 `authz_required`; invalid
proof403 `authz_invalid`; expired/used409 `authz_expired`/`authz_used`; stale409
`state_changed`; never-enrolled409 `setup_required`; legacy409
`legacy_registration_missing`; unusable factor/root409 `factor_unavailable`;
unsupported/oversized context409 `context_unsupported`. These codes have the
`vault_policy_` prefix and localized English/Chinese messages.

## Shared setup lifecycle

`POST /api/vault/setup` takes `{wrap_meta,authz_factor_registration}` and commits
existing sandbox setup independently of the first protected item. It and old
first-protected-create use one first-write reservation/transaction owner.
Registration is verified, and its credential bytes must match a submitted
Passkey copy. Root, public factor, consumed registration and audit commit
together. Different racing candidates lose; a byte-equivalent durable result
may be acknowledged again but cannot overwrite or re-enroll.

`state_meta["vault.setup.v1"]` stores an opaque base wrap-meta and creation
metadata only. No VMK, PRF output or plaintext is stored. Projection removes
only record-specific scheme/record_meta/wrapped_dek/dek_nonce; base/AAD fields
including encrypted root metadata are preserved. Existing record envelopes are
never rewritten. Root discovery prefers durable state, with legacy protected
records as a read-only fallback. No newest-record overwrite of a durable root.

The actual sandbox seal can continue returning `establishingVmk:true` after setup
commits. Web must reconcile acknowledgement with that payload: consumed first
registration is not new authorization. Server reuse accepts only the same root
and compatible active factor; supplied registration must match the committed
result. A new same-credential root-attachment flow is not silently invented.

Item deletion retains the shared setup and factors; deletion permissions remain
ordinary. A valid legacy root is preserved before deletion without declaring
an absent registration enrolled (`source:legacy-record`, null factor). Invalid
old envelope metadata is not rewritten. Full forget/reset is outside scope.

State handling:

- Never-enrolled/no-root: existing setup, durable commit, then a fresh policy
  challenge. Setup itself is not consent to lower.
- Active factor/no root: policy UV works without VMK/PRF. Protected root attachment
  is specifically unavailable; do not create another credential.
- Root with missing registration: preserve existing custody/create/delete access,
  refuse new lowering. An ID/wrapped VMK cannot reconstruct a public COSE key.
- Disabled history, wrong RP or corrupt factor: no reset/re-enrollment shortcut.
- Additional authorization factors still need current-factor authorization. They
  are not automatically VMK unlock copies.

This is an updated-runtime guarantee. Rolling back to older binaries that allow
lowering or reset factors on last-item deletion does not preserve the new gate.

## Evidence and remaining integration gates

- Public context/identity/address vectors and strict schema/bounds tests.
- Real synthetic WebAuthn assertions for ordinary/HD lowering, replay, malformed
  proof, wrong purpose/target/factor/RP/origin, UP/UV, counter and user handle.
- Writer races, stale/ABA/rotation, frozen allowlist and audit-failure rollback.
- Durable empty setup, repeat acknowledgement, exact-root later create, racing
  standalone/first-create candidates, legacy preservation and disabled-history
  refusal. Existing ordinary metadata/custody behavior stays covered.
- `AUTH-SETUP-167` closes the management-HTTP journey from setup-required through
  durable setup, reload, policy proof, raising and last-item deletion. The test
  uses synthetic ceremonies, not browser consent.

Required separate acceptance: actual typed popup/assertion integration, visual
review, pinned successor capability/refusal tests, physical Passkey and device
clock behavior, independent adversarial implementation review, exact-head
Codex/CI, owner merge/publication/pin decisions, and the wider HD recovery/browser/
funded/published-binary gates. No tests authorize real credentials or funds.
