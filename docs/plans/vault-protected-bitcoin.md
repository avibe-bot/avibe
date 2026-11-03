# Protected Bitcoin BIP86 transaction approval (V1)

Status: implementation contract; not deployed. This extends the existing v2
`sign`/signed-`release` protocol without changing encrypted records or the
standard-tier Rust signer. The actual protected signer is the cross-origin
vault-sandbox browser runtime; Python never receives a secret scalar or VMK.

## Scope and compatibility

Support a bounded Bitcoin payment: one BIP86 key-path input, one or two outputs,
SIGHASH_DEFAULT, no script tree or annex. Preserve existing keys and addresses.
Raw BIP340 remains untweaked; protected legacy/SegWit-v0 inputs remain unsupported.
No keypair-provision feature, PSBT import, coin selection, broadcast API, multisig,
multi-input signing, signing grants, storage migration or new crypto dependencies.

The existing signed release stays exactly
`{name, scheme, digest, publicKey, signingContext}`. The complete context below
is signed, frozen, and checked before approval and again before key use. Existing
Avibe producers and sandbox consumers reject the new context/scheme pairing
until upgraded; never fall back to a generic operation or raw digest.

## Frozen context

```typescript
{
  kind: "bitcoin-taproot-keypath-v1",
  network: "bitcoin" | "testnet" | "signet" | "regtest",
  unsignedTransaction: string, // lowercase raw non-witness transaction hex
  prevout: {
    amountSats: number,         // integer 1..2_100_000_000_000_000
    scriptPubKey: string       // lowercase 5120 + 32-byte BIP86 output key Q
  },
  digestAlgorithm: "bip341-sighash-default-keypath",
  digest: string               // exactly 64 lowercase hex digits, no 0x
}
```

Both object levels have exact required-field allowlists. Booleans are not
integers. No alternate amount, fee, change label, inputIndex, sighash, network
proof, Merkle root, tree or annex fields are accepted. The scheme must be
`schnorr-secp256k1-bip341-keypath` if and only if this Bitcoin context is used in
a protected request. EVM contexts remain ECDSA-only; raw BIP340 may still sign
properly domain-separated agent operations.

### Why a supplied prevout, not a full parent transaction

BIP341 SIGHASH_DEFAULT commits to every input amount and scriptPubKey. This V1
has exactly one input. A false amount/script produces an invalid signature for
the real coin, not a valid spend with hidden higher fees. Independently recompute
the digest, including these fields; bind the script to Q. Prove this property by
passing a false-amount signature to Core and observing rejection. A full parent
transaction would add a general-purpose/witness parser without proving chain
membership, so it is not in the signed context. The transaction-building caller
may independently verify full funding transactions and chain state.

### Exact transaction grammar and limits

- At most 137 bytes / 274 hex characters; lowercase hex only, no whitespace or 0x.
- Version exactly 2; no SegWit marker/flag or witness; exactly one input.
- Non-null outpoint, empty scriptSig, sequence one of 0xfffffffd/0xfffffffe/0xffffffff.
- One or two outputs; locktime exactly 0; no trailing bytes.
- Only exact P2PKH, P2SH, P2WPKH, P2WSH and P2TR output templates.
- Counts and supported script lengths fit single-byte CompactSize. Reject
  extended/nonminimal encodings rather than creating a generic Bitcoin parser.
- Each amount and aggregate is within Bitcoin's 21-million-coin money range.
  JS arithmetic for amounts/sums uses BigInt; Python uses int.
- V1 output dust floors (an explicit product policy, not live relay discovery):
  P2PKH 546, P2SH 540, P2WPKH 294, P2WSH/P2TR 330 sats.
- Fee must be positive. Signed weight is `4 * unsigned_size + 68`, vsize is its
  ceiling divided by 4 (one 64-byte signature, no hash-type byte).
- Hard-reject fee rates above 10,000 sat/vB as an explicit V1 safety ceiling.
  Render a prominent derived warning for fees above 100 sat/vB or 2% of input.
  Do comparisons exactly; formatting/rounding must not change policy decisions.

## Key ownership and honest display

1. Persisted valid compressed P is required before creating an approval.
2. Derive empty-tree BIP86 Q locally; prevout script must equal `5120 || Q.x`.
3. An output is a same-key return only when its script exactly equals that Q
   script, with at most one such output. Show every output, never hide change.
4. Reject outputs to this same key's advertised P2PKH/P2WPKH addresses (and its
   computable P2SH-P2WPKH form) because this protected workflow cannot spend them.
   Other standard-script outputs are recipients, not caller-labeled change.
5. Inside the trusted sandbox window derive full addresses, outpoint, input and
   output amounts, fee, vsize/rate, net value leaving this key and unsigned txid
   from parsed bytes. Show exact sats and integer-formatted BTC, no truncation.
6. Display the explicit network as a **requester's network selection**, not a
   verified chain or a cryptographically test-only transaction. A raw Bitcoin
   transaction and its signature do not commit to network/chain membership;
   never soften the funds warning just because the label is Signet/testnet.
7. Say "signals RBF" / "does not signal RBF", not "cannot be replaced".
8. Existing daemon display is surrounding context only. It must not override
   parsed recipients, fee, network warning or same-key-return classification.
9. Localize new display/warnings in English and Chinese. The Avibe parent card
   remains minimal and directs review to the secure window; no third codec.

## Validation and secret use

The shared Avibe service owner validates this typed context and Q before it can
persist a sign request, including direct service and CLI callers. API input also
validates shape/digest. Revalidate persisted Bitcoin context at protected
completion and preserve atomic single-settlement checks. The returned signature
must verify under Q using the existing Avibe public verifier.

The sandbox independently parses and hashes using existing SHA256/noble
primitives, checks Q and immutable signed release bindings, preserves fresh R3
UV, cancellation, expiry and replay checks, then signs at the actual protected
use boundary. Normalize the secret for even-Y P, apply TapTweak(P.x), check
`t < n`, permit t=0, reject zero tweaked secret, sign with fresh aux randomness,
and self-verify against the independently derived Q. Clear owned key/tweak/aux
byte buffers on all exits. JS BigInts/engine copies are not reliably zeroizable;
never claim constant-time or guaranteed erasure. Do not move protected secret
material into the parent app or Python or add a dormant alternate signing path.

## Acceptance (distinct observable boundaries)

- Independent Core-derived public fixtures cover P/Q parity combinations,
  self-transfer, recipient+return and all accepted output script encodings.
  Official BIP341/BIP86 vectors separately anchor primitive tweak/hash behavior;
  multi-input official vectors do not widen this V1 transaction grammar.
- Parser/policy mutation tables reject nonminimal/truncated/oversized/trailing
  encodings, scriptSig/witness, wrong version/locktime/sequence, bad amounts,
  output templates, key ownership, missing/extra fields and scheme pairings.
- API, CLI and direct service invalid cases create no pending request or signing
  side effect; valid context persists and produces the expected signed release.
- Actual parent hook/client wire reaches the actual sandbox strict parser, and
  the actual main.ts RPC returns a signature only after fresh-UV authorization.
  Cancellation, wrong binding/key, expired request, mutation and replay fail.
- Actual sandbox-signed synthetic transactions pass isolated Core
  testmempoolaccept; false prevout amounts/scripts, raw-P signatures and tampering
  fail. These are offline simulated-ceremony tests, not physical passkey E2E.
- Real user Signet/hardware-passkey testing is a later gate on the user's newly
  created regression key, after compatible code is available at the correct
  sandbox origin. Do not fund it as a test prerequisite before support is ready.

## Delivery and rollout

Avibe starts from current origin/master; sandbox changes extend existing PR25's
branch on origin/main rather than stack a new PR on an unmerged hardening PR.
One implementation owner serializes both repos against this contract; no
parallel competing schema changes. They use existing default-branch v2 wire
shapes and explicitly declare the new inner-kind rollout dependency.

Review/CI must cover each exact head; merge and shared deployment remain separate
authorizations. PR25's legacy binding hardening still requires compatible Avibe
producers before deployment. Confirm production auto-deploy behavior, installed
producer compatibility and the actual sandbox build before promoting anything.
Do not change `VAULT_SANDBOX_ORIGIN`, existing integrity policy/pins, RP ID, or
route the user's passkey to a preview to make a branch work. The present parent
has pre-launch manifest enforcement disabled; this task neither relies on that
as an integrity guarantee nor changes it. A locally isolated synthetic browser
can test ceremonies, but does not substitute for the user's real passkey.

Do not claim protected BTC deposits are safely spendable on installed versions
before the matching sandbox is deployed. New warnings must describe precisely
which workflow/build is required and which legacy inputs are still unsupported.
