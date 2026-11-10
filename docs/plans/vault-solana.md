# Solana support in Avibe Vault

**Status:** Proposed for 3.2.6; design-first review requested.
**Date:** 2026-11-10
**Owners:** Vaults workstream
**Related:** `avault/docs/DESIGN.md`, `docs/plans/avault-custody-core.md`,
`docs/plans/vault-protected-bitcoin.md`, `docs/plans/vault-taproot-signing.md`.

## 1. Decision in one paragraph

Add Solana as a new local signing-key family backed by Ed25519. Standard-tier
keys are generated, imported, encrypted, and signed inside `avault`; Avibe and
the browser see only the sealed envelope plus public metadata. Solana addresses
are the base58 encoding of the 32-byte Ed25519 public key. `vibe vault sign`
uses an explicit `--message-hex` input for Solana's exact serialized message
bytes; `--digest` is rejected for `ed25519` with a hint, so the existing digest
contract cannot be accidentally overloaded. Protected Solana key creation and
signing are out for v1: without a verifiable transaction context the approval
surface must say “not supported yet” rather than authorize blind bytes.

## 2. Scope and invariants

### In scope for v1

- Ed25519 key generation and import in `avault`.
- Standard-tier, non-exportable Solana signing keys.
- Public Solana address derivation and display.
- `ed25519` exact-byte signing through the existing Vault signing path.
- UI chain-family selection, standard/protected gating, and address lists.
- Test vectors and negative tests at the Rust, Python, and UI boundaries.

### Invariants

- `avault` is the only component that handles the Ed25519 seed and performs
  Ed25519 signing. It returns a sealed envelope, public metadata, or a
  signature; it never returns a seed/private key or a decrypt verb.
- Standard creation/import uses the existing browser-to-avault blind-box
  boundary. Python relays the opaque blind box and stores only the resulting
  ciphertext envelope and value-free metadata.
- Signing verifies the returned signature against the pinned public key before
  marking a request successful or writing a success audit event.
- No test key is funded. Tests use published vectors or deterministic,
  publicly-known devnet-only material and never broadcast a transaction.

## 3. Key material and import contract

### 3.1 Canonical stored metadata

The existing `public_meta.signing_public_key` object becomes curve-aware:

```json
{
  "curve": "ed25519",
  "chain": "solana",
  "public_key": "<64 lowercase hex characters>"
}
```

The 32-byte public key remains canonical hex in metadata, matching the existing
secp256k1 metadata convention. The public address is derived from those bytes and
is surfaced separately as:

```json
{"solana": "<base58 public key>"}
```

Existing secp256k1 rows remain unchanged. Their chain-agnostic EVM/Bitcoin
addresses and schemes continue to work as they do today.

### 3.2 Generation

Add an `avault` keypair operation equivalent to:

```text
avault keypair generate --name NAME --curve ed25519
```

The command creates a fresh 32-byte Ed25519 seed, derives the 32-byte public key,
seals the seed under the standard machine-rooted envelope bound to `NAME`, and
returns only:

```json
{
  "envelope": {"ciphertext": "...", "nonce": "...", "wrap_meta": "..."},
  "curve": "ed25519",
  "public_key": "<64 lowercase hex characters>"
}
```

Avibe adds `chain: solana` and the derived address to `public_meta`; the seed
never crosses the avault process boundary.

### 3.3 Import

Add a stdin-only operation equivalent to:

```text
avault keypair import --name NAME --curve ed25519 \
  --format solana-cli-json|base58-secret --blind-box
```

The browser seals the user input to avault's public key with a distinct
operation-bound blind-box context. Avibe relays that box; avault opens and
validates it, derives the public key, seals the seed, and returns the same
value-free result as generation.

V1 accepts these wallet export shapes:

1. **Solana CLI keypair JSON:** exactly 64 integers in the range `0..255`. The
   first 32 bytes are the seed and the final 32 bytes must equal the derived
   public key.
2. **Base58 secret key:** a base58 string decoding to exactly 64 bytes with the
   same seed/public-key consistency check. This covers wallet exports that use
   the Solana secret-key byte array encoded as base58.

V1 does **not** accept mnemonics, derivation paths, JSON objects with extra
fields, arbitrary 32-byte seeds in the product import form, PEM/PKCS#8, or
private-key export files from other curves. The Rust tests may use a raw
32-byte seed fixture internally, but that is not a user-facing import format.
Malformed encodings, wrong lengths, and a mismatched trailing public key fail
before an envelope is written.

There is no export operation for a Solana key. Losing a keypair secret is
intentionally irreversible, as with other non-exportable Vault signing keys.

## 4. Address display

A Solana address is exactly the base58 encoding of the 32-byte Ed25519 public
key. It has no mainnet/devnet/testnet prefix and no checksum variant. The UI
labels it **Solana** and provides the existing copy affordance. Address display
is public-only and may be performed by Avibe after avault returns the public key;
it does not weaken custody.

The address list becomes curve-aware:

- secp256k1: existing ETH, BTC SegWit, Taproot, and legacy rows.
- Ed25519/Solana: one Solana row only.

Signing approval cards show only addresses truthful for the requested scheme.
For `ed25519`, that is the Solana address; an Ed25519 key is never presented as
an EVM or Bitcoin identity.

## 5. Signing contract

### 5.1 Scheme and exact bytes

Add the scheme identifier:

```text
ed25519
```

For existing secp256k1 schemes, `--digest` remains exactly 32 bytes of hex and
is passed to the selected prehash/signing primitive as today. For `ed25519`,
`--message-hex` is required and contains the exact serialized Solana message
bytes encoded as lowercase hex. avault signs those bytes directly with pure
Ed25519; it does not hash, truncate, pad, or reinterpret them. Passing
`--digest` with `ed25519` is rejected with a hint to use `--message-hex`. The
accepted message size is bounded (v1: 1 MiB at the CLI/core boundary) to prevent
accidental unbounded memory use. Solana transaction callers pass the serialized
Solana message, not a transaction signature, wire packet, or a hash of the
message.

The CLI help and the public plan must call this out prominently:

```text
vibe vault sign NAME --scheme ed25519 --message-hex MESSAGE_HEX
# For ed25519, --message-hex is the exact message bytes; it is not a digest.
```

The existing Python/API `digest` field remains reserved for 32-byte digest
schemes. The Solana request shape adds a `message_hex` field and persists the
exact message bytes only as request data needed to settle the same operation;
it never changes the meaning of existing schemes.

### 5.2 Result and verification

The result is a 64-byte Ed25519 signature encoded as lowercase hex and contains
no recovery id. Avibe verifies it against the stored Ed25519 public key before
returning it or settling a request. Invalid public-key curve/scheme pairings,
wrong message encoding, malformed signatures, or a key envelope that does not
contain 32 seed bytes fail closed.

Standard approval (`always_ask`) may still show the exact message hex and the
Solana address. That is a byte-level approval, not a claim that Avibe decoded a
transaction.

## 6. Trust tiers and protected signing

### Standard

Supported in v1. The machine-rooted key is usable headlessly, and avault signs
inside its short-lived CLI process or resident-agent signing path. This is the
right tier for automation and devnet-only agents. The machine-compromise limits
are the existing standard-tier limits: it protects at rest and keeps seeds out
of Python/LLM state, but it does not protect a running compromised same-user
machine.

### Protected

Protected Solana key creation and signing are **not supported in v1**. The
protected browser protocol must not accept a raw Ed25519 message/digest and
render an approval card that cannot honestly identify fee payer, recipients,
lamports, programs, and other transaction effects. Until a trusted Solana
message decoder and signed operation context exist, the API rejects protected
Solana signing before creating a request or prompting for a passkey, with a
localized “protected Solana signing is not supported yet” error.

The UI therefore disables the Protected tier when Solana is selected and explains
why. Existing protected secp256k1 flows are unchanged. There is no fallback from
protected Solana to standard signing and no blind-digest protected path.

## 7. UI shape

- Add a **Chain** picker to the keypair builder:
  - **EVM / Bitcoin** — existing secp256k1 path and address list.
  - **Solana** — Ed25519 path, Solana address list, standard tier only.
- Keep the selected chain/curve in the non-secret public metadata so editing a
  key cannot silently change its interpretation.
- Solana standard generation/import calls the Avibe endpoint backed by avault;
  the browser receives only the envelope and public metadata. The existing
  secp256k1 compatibility path is not redesigned in this increment.
- The Vault list, detail view, search terms, approval card, and signing-address
  component render the Solana row and chain label without exposing raw public
  keys or private material.
- The signing form and CLI surface use `ed25519` as the scheme. Protected Solana
  controls are visibly unavailable rather than failing after submission.

## 8. Tests and acceptance

### avault Rust PR

- RFC 8032 Ed25519 known-answer vectors, including exact message bytes and the
  empty-message case.
- Generated keypair round-trip: public key and signature verify; private bytes
  never occur in JSON output or stdout.
- Solana CLI JSON import and base58 64-byte import, including trailing-public-key
  mismatch, malformed JSON, invalid byte values, invalid base58, and length cases.
- Blind-box import context is name/curve bound and cannot be replayed as a static
  secret or another keypair.
- CLI accepts messages longer than 32 bytes for `ed25519` and proves the exact
  bytes are signed; secp256k1 schemes retain the 32-byte digest limit.
- `cargo fmt`, `cargo test`, and `cargo clippy --all-targets --all-features`.

### Avibe integration PR

- Public metadata validation accepts only the expected curve/key encoding and
  derives the expected base58 Solana address.
- `ed25519` scheme is accepted only for `curve: ed25519`; secp256k1 schemes are
  rejected for Solana keys and `ed25519` is rejected for secp256k1 keys.
- Exact-message hex validation through `--message-hex`, signature verification,
  request settlement, and value-free audit are covered at API/CLI boundaries.
- `--digest` with `ed25519` is rejected before request creation and returns a
  hint to use `--message-hex`.
- Protected Solana requests fail before request creation, notification, or
  passkey/sandbox work.
- Standard generation/import relay tests prove Python handles only blind boxes,
  ciphertext envelopes, and public metadata.

### UI PR

- Chain picker selects the correct curve path and clears stale key material when
  switching chains.
- Solana generation/import shows only the Solana address; EVM/Bitcoin rows remain
  unchanged.
- Protected Solana is unavailable with an explanatory localized message.
- Approval cards show the truthful address for `ed25519`, and list/detail/search
  surfaces render the chain label and base58 address.
- `npm run build` and focused Vault component tests pass.

## 9. Explicit non-goals for v1

- Protected Solana signing or protected Solana transaction approvals.
- Solana message/transaction parsing in the browser or Python.
- SPL token/account parsing beyond future display-only metadata.
- Durable nonces, address lookup tables, versioned transactions, lookup-table
  fetching, priority fees, compute-budget interpretation, or program-specific
  decoding.
- Transaction construction, fee estimation, broadcast, confirmation, or wallet
  connectivity.
- Mnemonic/derivation-path import, hardware-wallet/WalletConnect signing, or
  export/recovery flows.
- A second Ed25519 variant such as Ed25519ph or Ed25519ctx; v1 is pure Ed25519
  over caller-supplied bytes.

## 10. Resolved product decisions (2026-11-10)

1. Protected Solana is entirely out of v1, including protected key creation.
   The UI disables that tier with a short reason; there is no protected
   blind-digest fallback.
2. The two user-facing import formats are sufficient for 3.2.6: Solana CLI
   64-byte JSON array and base58-encoded 64-byte secret key. Mnemonics and
   derivation paths remain out.
3. Solana signing uses an explicit `--message-hex` flag. `--digest` is refused
   with `ed25519` so exact message bytes cannot be confused with a digest.

Implementation follows in separate avault, Avibe integration, and UI PRs. Each
PR is security-sensitive and waits for owner approval before merge; nothing
merges before stable 3.2.5 is published.
