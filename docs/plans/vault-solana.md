# Solana support in Avibe Vault

**Status:** Standard tier proposed for 3.2.6; protected-tier Solana (native SOL
first, then SPL tokens) is a post-3.2.6 design, not an implementation
authorization.
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
contract cannot be accidentally overloaded. The standard tier is the 3.2.6
scope. A later protected tier must use the cross-origin sandbox to decode those
same message bytes before approval; it must never turn an opaque Ed25519 digest
into a misleading approval.

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

## 6. Current 3.2.6 protected boundary

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

## 7. Protected-tier Solana design (next step; design only)

This section is a proposal for the next protected Solana slice. It does not
change avault PR #43, the current standard-tier contract, or the owner's pending
approval of that PR. Protected Solana is not “standard Ed25519 plus a browser
prompt”: the signer, parser, approval text, and passkey binding must all agree
on the exact operation.

### 7.1 Recommendation and non-negotiable invariants

I recommend a **native-SOL protected slice first**:

- support legacy messages and v0 messages with no address-table lookups;
- decode System Program transfers, a narrow Compute Budget subset, and Memo;
- deliver in two steps on one parser, wire kind, and key path: **step 1 native
  SOL**, then **step 2 classic SPL Token transfers** (§7.5). Tokens are a
  required, planned step (owner decision, 2026-11-10), not an optional
  extension. Token-2022 and address lookup tables stay refused until their own
  later steps;
- reject every unknown instruction or program with a specific reason; do not
  render “unrecognized program” and let the user approve it;
- require one signer, the Vault Ed25519 key as the fee payer, and the exact
  message bytes that the sandbox parses to be the bytes signed;
- preserve fresh R3 passkey approval, expiry, replay protection, signed release
  binding, and fail-closed behavior. There is no protected-to-standard fallback.

Both steps show only facts that the signed bytes carry, or that the invoked
program enforces when the transaction runs. Neither step needs a network call
from the sandbox, a requester-supplied label, or a state snapshot.

### 7.2 Wire context and exact-byte binding

The user-facing context is named **`solana-transaction`**; the versioned wire
kind is:

```typescript
{
  kind: "solana-transaction-v1",
  message: string // lowercase hex, exact serialized legacy or v0 message
}
```

The value is the serialized Solana **message**, not a signed transaction,
signature, packet, or hash. The protected message limit is 1,167 bytes for the
one-signature slice: one 64-byte Ed25519 signature plus the compact signature
count must still fit Solana's 1,232-byte transaction packet limit. Lowercase
hex, no `0x`, no whitespace, and no trailing bytes are required. The parser
bounds every short-vector count and allocation before reading it and re-encodes
the parsed message byte-for-byte; any non-canonical short-vector encoding is
rejected.

For protected `ed25519`, the signed daemon release becomes:

```typescript
{
  name,
  scheme: "ed25519",
  publicKey: "<64 lowercase hex characters>",
  signingContext: {
    kind: "solana-transaction-v1",
    message: "<the same lowercase message hex>"
  }
}
```

There is deliberately **no `digest` field** in this release. The context already
contains the exact signed bytes, and the existing challenge hashes the complete
signed release. Using `digest = SHA256(message)` would put a value that is not
signed in a field whose name invites the wrong mental model. The three-way
pairing is mandatory at every boundary:

```text
record curve = ed25519
  <=> scheme = ed25519
  <=> signingContext.kind = solana-transaction-v1
```

A generic `avault-agent-operation` context must not reach an Ed25519 key, and a
Solana context must not reach a secp256k1 key. The sandbox parses and displays
the context from `message` immediately before approval and again at key use;
Python validates shape and equality but never supplies display facts that can
override the sandbox.

The CLI keeps the standard exact-message input and adds the protected context
binding:

```text
vibe vault sign NAME --scheme ed25519 \\
  --message-hex MESSAGE_HEX \\
  --signing-context-json '{"kind":"solana-transaction-v1","message":"MESSAGE_HEX"}' \\
  --command "sign Solana transaction"
```

For a protected request, `--signing-context-json` is required and its message
must equal `--message-hex` byte-for-byte. A missing context, a mismatched message,
`--digest`, or any generic context is rejected before a request, notification,
passkey prompt, or signing side effect is created. Standard `ed25519` signing
continues to accept `--message-hex` without a transaction context.

### 7.3 Strict legacy/v0 message grammar

The sandbox parser accepts only the following message forms:

- legacy messages; or
- v0 (`0x80`) messages with **zero** address-table lookups. Any other version,
  including a future version, is refused;
- exactly one required signature; account index 0 is the writable fee payer and
  must equal the persisted Ed25519 public key;
- no duplicate account keys, no out-of-range program/account indices, no
  unreferenced signer, no signer program ID, and no trailing bytes;
- the canonical header, static account list, recent blockhash, compact
  instruction list, and (for v0) lookup-list suffix must all be consumed exactly.

The first System instruction that names a durable nonce, including
`AdvanceNonceAccount`, is not a special safe case: it is rejected as an
unsupported nonce instruction. The protected slice does not sign durable bearer
transactions. Approval may be slow enough for a recent blockhash to expire;
the caller must rebuild and request approval again rather than use a durable
nonce workaround.

The approval window shows, all derived from the parsed bytes:

- message version (`legacy` or `v0`), fee-payer Solana address, and recent
  blockhash in base58, with a statement that the message does not encode a
  network/cluster;
- every instruction in original order, including every transfer and memo;
- exact lamports and integer-formatted SOL for each System transfer, plus the
  total leaving the fee payer;
- explicit compute-unit limit and price when present, and the derived maximum
  priority fee when both are present;
- that the base transaction fee is not encoded in the message and may vary by
  cluster/runtime. It must not be presented as a verified exact fee.

There is no claim that a displayed `recentBlockhash` proves mainnet, devnet, or
any other cluster. Solana messages do not carry a chain ID, and identical
addresses exist across clusters. `connect-src 'none'` remains intact; the
sandbox does not silently query an RPC endpoint during approval.

### 7.4 Recognized instruction policy

**System Program — `Transfer` only.** Decode the canonical 12-byte instruction
(`Transfer` discriminator plus little-endian u64 lamports), require the source
account to be the Vault fee payer, and display the destination account and exact
lamports. All other System instructions are refused with a reason, including
account creation, assignment, nonce operations, and `TransferWithSeed`.

**Compute Budget — narrow subset.** Recognize only `SetComputeUnitLimit` and
`SetComputeUnitPrice`. Accept at most one of each, display the exact requested
values, and require a price to be accompanied by an explicit limit so the
maximum priority fee is bounded and visible. Enforce the runtime maximum for the
limit and a product fee ceiling for the price/limit product. Refuse
`RequestHeapFrame`, `SetLoadedAccountsDataSizeLimit`, duplicate variants, and
malformed data in the first slice. The price is in micro-lamports per requested
compute unit; show the derived product without floating-point rounding.

**Memo — UTF-8 only.** Recognize the pinned legacy Memo program IDs and accept
only valid UTF-8 instruction data. Display the exact text with escaped control
characters and the byte length. A non-UTF-8 memo is refused rather than shown as
an ambiguous replacement-character string.

**Step 2: Associated Token Account Program, `CreateIdempotent` only.** Data is
exactly `[1]`. Exactly six accounts: the funding account (must be the Vault fee
payer), the associated token account `A`, the wallet `W`, the mint `M`, the
System Program, and the classic Token Program. `Create`, `RecoverNested`, a
different funding account, or the Token-2022 program ID is refused.

**Step 2: Token Program, `TransferChecked` only.** Data is exactly 10 bytes
(tag 12, little-endian u64 amount, u8 decimals). Exactly four accounts: source
`S`, mint `M`, destination `A`, and the authority, which must be the Vault key.
Plain `Transfer`, multisig authorities, and every other Token instruction
(`Approve`, `SetAuthority`, `CloseAccount`, `Burn`, `MintTo`, and the rest) are
refused by name.

**Unknown programs and instructions.** Refuse them with a stable reason such as
“program X instruction Y is not supported by protected Solana approval.” Do not
show an “unrecognized program” row with an approve button. That would be a
blind-signing surface under a more reassuring label.

### 7.5 Token transfers (step 2): the transaction proves the recipient

Token transfers are in scope. The first token step covers the classic SPL Token
program, which owns the USDC and USDT mints on mainnet. It needs no outside
trust.

**Accepted shape.** A token send is two instructions in one message:

1. `CreateIdempotent(funding = Vault key, A, W, M, System, Token)`
2. `TransferChecked(S, M, A, authority = Vault key, amount, decimals)`

Every `TransferChecked` destination must be the account `A` of exactly one
earlier `CreateIdempotent` with the same mint and token program, and every
`CreateIdempotent` must be used by a later `TransferChecked`. The wallet `W`
comes from the message bytes; the signing context stays `{kind, message}`.

**What is proven, and by what.**

| Fact shown | Proven by |
| --- | --- |
| Mint `M`, amount in base units, decimals | `TransferChecked` data and accounts; the Token program fails the transfer on a mint or decimals mismatch |
| `A` is the associated token account address of `W` for `M` | The ATA program recomputes the address from (`W`, token program, `M`) and fails on a mismatch |
| `W` is the token-account authority of `A`, and `A` holds `M`, when the transfer runs | `CreateIdempotent` fails if an existing `A` has another token-account authority or mint; otherwise it creates `A` with authority `W`. No instruction this grammar accepts can change that authority before the transfer |
| The Vault key is the owner or delegate of `S` | The Token program's authority check |

These are execution-time checks by programs whose deployed mainnet authorities
must be verified as part of release acceptance: the ATA program must be under a
non-upgradeable loader and the classic Token program must have no upgrade
authority. Source tags alone do not establish the deployed binaries. A failed
check fails the whole transaction: the fee is charged and no tokens move.

**Why derivation alone is not enough.** A classic token account's token-account
authority can be reassigned with `SetAuthority`; the classic program accepts but
ignores `InitializeImmutableOwner`. A wallet `W` that signed such a
reassignment, by choice or under phishing, leaves the address derived for `W`
under another key's control. A bare `TransferChecked` to that address, displayed
as “to W”, would be false. `CreateIdempotent` in the same transaction turns that
case into a failed transaction, so the window never needs the weaker wording.

**Why no state witness.** An authenticated RPC or attestor would report the
owner as of an earlier slot; the program check holds at execution. A witness
would also add a hosted service to the signing path, a new pinned trust key,
recipient disclosure to that service, an availability dependency, and an
exception to `connect-src 'none'`. It remains the route for facts the bytes
cannot carry: address lookup tables, non-associated destination accounts, and
Token-2022 mint configuration.

**Cost the bytes do not carry.** When `A` does not exist, the Vault key pays its
storage deposit. The amount is a network parameter and is not encoded in the
message. The approval states that a possible storage deposit will be charged,
without presenting a fixed figure that was not verified for the execution
cluster; the same rule applies to the base transaction fee.

**Display, all sandbox-derived.** For each token send: the amount in token
units and in base units with decimals; the full mint address; the destination
wallet `W` and its token account `A`; the source token account `S`; the
statement that the transaction fails unless `W` is the token-account authority
of `A` for this mint when it runs; and the possible storage deposit. Amount
For example, `send 10 USDC to W` shows `10 USDC` (`10,000,000` base units,
6 decimals), the full USDC mint address, wallet `W`, derived token account `A`,
source account `S`, the execution-time ownership guarantee, and a possible
storage deposit; it does not invent a network or exact fee. The full mint address is always shown. A pinned table compiled
into the sandbox names well-known mainnet mints, initially USDC
(`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) and USDT
(`Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB`), both 6 decimals and both
classic Token Program mints. A name appears only on an exact address match; a
pinned mint with different decimals is refused. Any other mint is labelled
“unlisted token” and shown by address. The requester never supplies a name.
Each entry is re-verified against the issuer's published address when it is
added; a new entry needs a new sandbox release.

**Refused in step 2.** Plain `Transfer` (no mint or decimals in the bytes); a
`TransferChecked` without its `CreateIdempotent`, which covers raw and
non-associated destination accounts; multisig authorities; every other Token
and ATA instruction; and every instruction addressed to the Token-2022 program.

**Token-2022 (later, narrower).** No Token-2022 transfer is accepted in step 2.
The program has an upgrade authority, and mint configuration can add a transfer
fee, a transfer hook, or a display multiplier. A later step can stay state-free
for a pinned mint list: `TransferCheckedWithFee` carries the fee and the program
fails on a mismatch, so the net amount is provable; with exactly four accounts
and no other program in the message, a hook program receives only the four
transfer accounts, read-only. That second claim must be demonstrated on a local
validator before it is relied on. Display multipliers (`ScaledUiAmount`,
`InterestBearing`) are not in the bytes, so such a step shows base units and
decimals only. Confidential transfers stay refused.

### 7.6 v0 address lookup tables

V0 messages with a non-empty lookup-table list are refused in this version with
“address lookup tables are not supported by protected Solana approval.” The
sandbox cannot resolve an ALT offline, and silently asking the parent to resolve
it would make the approval facts parent-controlled.

Supporting ALTs safely later requires the exact lookup-table account bytes from a
trusted source, verification that the account is owned by the Address Lookup
Table Program, correct table address/index ordering, writable/read-only loaded
address sets, deactivation/extension state, and a commitment/freshness witness
that is meaningful for the same cluster as the recent blockhash. The resulting
state witness must be included in the signed context or authenticated directly
by the sandbox. This belongs to a later state-witness design, not to steps 1 and 2.

### 7.7 Protected key creation and import

Protected Ed25519 key creation does **not** go through avault or HPKE today.
Existing protected keypair generation happens inside the cross-origin sandbox:
the sandbox creates the seed, seals it under the VMK with AES-GCM/AAD, and keeps
key use behind the fresh passkey path. HPKE is used by the standard browser to
avault flow and protected static-secret delivery, not by protected keypair
creation or signing. The standard avault PR remains unchanged and continues to
refuse protected Solana operations.

The sandbox changes for protected generation are:

- add `curve: "ed25519"` to the protected record metadata and AAD only for new
  Ed25519 records; omit it for existing secp256k1 records so their AAD remains
  byte-identical;
- generate a 32-byte Ed25519 seed, derive and self-check the 32-byte public key,
  and expose only canonical public metadata plus the base58 Solana address;
- add an Ed25519 exact-message signing branch with self-verification and an
  explicit refusal in the digest branch;
- add the strict parser and the `solana-transaction-v1` context to the signed
  release allowlist, while preserving the existing R3, replay, expiry, binding,
  and challenge checks.

Protected import is **not** in the recommended first slice. It is not present for
protected secp256k1 today, and adding a parent-typed secret would put the import
value in the main-app origin once, weakening the reason for the cross-origin
sandbox. The safer follow-up is a sandbox-owned input window where the user
enters the Solana CLI 64-byte key or base58 64-byte secret, the sandbox validates
seed/public-key consistency, and only then seals it under the VMK. That requires
new sandbox UI and acceptance tests; it still does not require avault or HPKE.
If import is mandatory in the first protected release, the owner must choose
between that stronger input window and the cheaper but weaker parent-value path.

### 7.8 Avibe protocol, Web UI, and CLI work

**Avibe service and Python.** Add the context kind to the strict context table;
validate lowercase message hex and the one-signature grammar at the request
boundary; persist the exact context; compare it again at completion; and bind
record curve, scheme, public key, and context kind before creating a pending
request. Python may reject malformed requests early, but the sandbox remains the
source of truth for displayed transaction facts. Add an Ed25519 public-key
verifier and do not reuse the 32-byte digest validator for this path.

**Web approval UI.** The parent Vault card remains minimal and directs the user
to the secure approval window. Once the matching sandbox release is pinned, the
Solana key builder may select Protected without silently changing to Standard.
The secure window renders the parsed fee payer, blockhash, instruction rows,
amounts, compute budget, memo, and the no-network caveat. Unknown programs,
unsupported token state, ALT use, malformed messages, or a stale/mismatched pin
stop before a passkey prompt. The display is localized in English and Chinese.

**CLI.** `--signing-context-json` is required for protected `ed25519`; its exact
message must match `--message-hex`. The CLI must retain the existing error for
`--digest` with a hint to use `--message-hex`, and must not offer a generic
operation context as a workaround. Help text should call the input “exact
serialized Solana message bytes,” not “digest.”

### 7.9 Acceptance and release gates

The implementation needs distinct cross-language and end-to-end evidence:

- identical parser/policy fixtures in TypeScript and Python for legacy, v0 with
  zero lookups, every accepted instruction, every refusal, durable nonce,
  duplicate keys, nonminimal short-vectors, truncation, trailing bytes, unknown
  programs, oversized messages, and all three-way scheme/context pairings;
- step 2 fixtures in both languages: the accepted two-instruction shape; every
  refusal in §7.5; pairing errors (missing, duplicate, unused, reordered, wrong
  mint or program); pinned-name match and decimals mismatch; u64 amount edges;
- step 2 on a local validator running the mainnet ATA and Token program
  binaries: a send to a new wallet creates `A` and moves the tokens; a send to an
  `A` whose token-account authority was reassigned fails with no token movement;
  wrong mint and wrong decimals fail;
- RFC 8032 Ed25519 vectors plus exact Solana message fixtures, including a
  self-transfer and multiple native transfers. The signature must verify against
  the parsed fee payer key and never be verified against a digest alias;
- actual CLI/API requests reaching the actual sandbox parser, with no pending
  request or passkey side effect on invalid input; expiry, cancellation, replay,
  context mutation, wrong key, and stale pin all fail closed;
- an isolated `solana-test-validator` or equivalent fixture runner verifies that
  accepted synthetic transactions serialize and sign as expected. It must use
  disposable local/devnet-only material and no funded user key; this is not a
  claim that the approval proves cluster membership;
- source-derived sandbox release pin, SRI, build-hash handshake, and production
  deployment are separate gates. Sandbox `main` deployment needs its own
  authorization; merging this docs PR or an implementation PR does not deploy
  the protected signer.

### 7.10 PR split, estimate, and release recommendation

The smallest implementation split is:

1. **Docs PR (this change).** Freeze the contract, refusal policy, token/ALT
   boundary, and rollout decision.
2. **vault-sandbox PR.** Ed25519 protected generation/signing, curve-aware AAD,
   strict native-SOL parser/display, signed-context pairing, fixtures, and
   isolated browser tests. No avault change.
3. **Avibe integration PR.** Context validation and lifecycle binding, public
   metadata/address derivation, CLI `--signing-context-json`, signature
   verification, and sandbox release pin update after the reviewed sandbox
   release exists.
4. **Web UI PR.** Protected Solana key builder state, secure-window handoff,
   localized approval/list/detail surfaces, and real isolated-browser acceptance.
5. **Token step.** One vault-sandbox PR (ATA and Token instruction recognition,
   pairing rule, display, pinned names) and one Avibe PR (Python mirror, shared
   fixtures, pin update). It reuses the step 1 parser, wire kind, key path, and
   approval window.
6. **Later, separately decided.** Sandbox-owned protected import; a narrow
   Token-2022 step; a state witness for address lookup tables.

Rough engineering estimate, assuming the current sandbox protocol and release
pin machinery remain reusable:

- step 1, native SOL: **16–23 engineer-days** across sandbox, Avibe, UI,
  fixtures, review fixes, and isolated acceptance;
- step 2, classic SPL tokens: **+7–10 days** on top of step 1;
- sandbox-owned protected import: **+4–6 days**;
- narrow Token-2022 step: **+4–6 days**;
- state witness (lookup tables, non-associated destinations): not estimated
  here; it includes a hosted service and its operation.

This should **not** be squeezed into the standard 3.2.6 cut. It can follow
closely as the next release's first feature, but the recommended target is the
next release (for example 3.2.7): the sandbox source release, immutable pin,
production deployment, passkey acceptance, cross-repository exact-head review,
and security-boundary approval are independent gates. Native SOL ships first and
classic SPL tokens follow as the next step; both come after 3.2.6. Whether they
share one sandbox release is an owner decision (§7.11).

### 7.11 Questions for the owner

1. Is the token shape accepted: a token send must carry `CreateIdempotent` for
   its destination, and the approval states a possible storage deposit whose
   amount is set by the network and is not in the message?
2. Token names: a pinned USDC/USDT table in the sandbox with every other
   classic mint shown as “unlisted token”, or only pinned mints accepted, or
   addresses only?
3. Should native SOL and tokens ship in one sandbox release (one deployment and
   approval cycle, later) or two (native SOL sooner)? Is any Token-2022 token
   needed soon?

### 7.12 Sources of truth

Protocol references used for this design:

- Solana transactions and packet limits:
  <https://solana.com/docs/core/transactions>
- Legacy/v0 message grammar and address-table lookups:
  <https://solana.com/docs/core/transactions/versioned-transactions>
- Compute Budget instruction data and limits:
  <https://solana.com/docs/core/fees/compute-budget>
- SPL transfer semantics and token-account ownership:
  <https://solana.com/docs/tokens/basics/transfer-tokens> and
  <https://solana.com/docs/tokens/basics/create-token-account>
- Associated token account creation and the idempotent owner check:
  <https://github.com/solana-program/associated-token-account/blob/program%40v8.0.0/program/src/processor.rs>
- Classic Token `TransferChecked`, `SetAuthority`, and the ignored
  `InitializeImmutableOwner`:
  <https://github.com/solana-program/token/blob/program%40v9.0.0/program/src/processor.rs>
- Token-2022 fee check and hook invocation:
  <https://github.com/solana-program/token-2022/blob/program%40v10.0.0/program/src/processor.rs>
- Token-2022 immutable owners, transfer hooks, and transfer fees:
  <https://solana.com/docs/tokens/extensions/immutable-owner>,
  <https://solana.com/docs/tokens/extensions/transfer-hook>, and
  <https://solana.com/docs/tokens/extensions/transfer-fees>

Avibe implementation anchors are `vault-sandbox/src/signRequest.ts`,
`src/signingContext.ts`, `src/vaultCrypto.ts`, and `src/main.ts`, plus
`docs/plans/vault-sandbox-integrity.md` and
`docs/plans/vault-protected-bitcoin.md`. The protected signer is the sandbox;
`avault/docs/DESIGN.md` and avault PR #43 remain the standard-tier custody
contract and are not changed by this design.

## 8. UI shape

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
- The signing form and CLI surface use `ed25519` as the scheme. In the current
  3.2.6 standard delivery, Protected Solana controls are visibly unavailable;
  the next protected release may enable them only after the matching sandbox
  pin and deployment gates pass.

## 9. Tests and acceptance

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

## 10. Explicit non-goals for the current 3.2.6 standard tier

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

## 11. Resolved product decisions for the current standard tier (2026-11-10)

1. Protected Solana is entirely out of the current 3.2.6 standard delivery,
   including protected key creation. The UI disables that tier until the
   protected design above is implemented and its sandbox pin is deployed; there
   is no protected blind-digest fallback.
2. The two user-facing import formats are sufficient for 3.2.6: Solana CLI
   64-byte JSON array and base58-encoded 64-byte secret key. Mnemonics and
   derivation paths remain out.
3. Solana signing uses an explicit `--message-hex` flag. `--digest` is refused
   with `ed25519` so exact message bytes cannot be confused with a digest.

Implementation follows in separate avault, Avibe integration, and UI PRs. Each
PR is security-sensitive and waits for owner approval before merge; nothing
merges before stable 3.2.5 is published.
