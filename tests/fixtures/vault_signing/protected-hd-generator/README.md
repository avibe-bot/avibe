# Protected-HD public contract vectors

**PROPOSED, not a wire-freeze ACK or product acceptance. All material is public.
NEVER FUND, import into a real wallet, or reuse these keys.**

These fixtures freeze bytes at boundaries that did not previously have protected-HD
coverage. They do not implement the feature, exercise a real Passkey, or sign/broadcast.

## Files and purpose

- `../protected-hd-fingerprint-vectors.json` is the backend owner's exact 18-vector
  file, byte-for-byte SHA256
  `ec59b255b57229ebbe4485e28649e7ad42aa1237ef3af5d9b9214caf7e051949`.
  It pins the shipped `storage/vault_service.py:hd_signing_binding` string-array
  fingerprint, including Python `ensure_ascii=True`, surrogate code units, DEL,
  escaping, raw wrap-string order/format and one-byte changes. Codec-only invalid
  envelopes and lone surrogates do **not** authorize these values in real inputs.
- `../protected-hd-codec-vectors.json` pins all five entropy lengths, the fixed
  marker/seed/encrypted passphrase-required bit, the complete20×5 public table
  under `bitcoin-address-types-v1` (EVM, threeBTC, Solana),
  exact typed-record v2 AAD text and bytes, and deterministic AES-256-GCM of both
  payload and wrapped DEK with that same AAD. The original and reordered raw wrap
  strings have identical parsed metadata but different signing fingerprints.
- `generate_vectors.py` uses the **existing** `hd-corpus-generator` locked oracle
  environment: Python 3.13.11, bip_utils, embit and independent SLIP10 checks.
  Entropy is explicitly the public deterministic byte sequence `00..length-1`.
  Seed generation is checked across bip_utils, embit and stdlib PBKDF2; account
  derivation uses the existing independent oracle, not product derivation code.
  Its EVM uncompressed oracle output is encoded as a compressed point for the
  protected table; Bitcoin `bitcoin` keys are BIP86 **internal** compressed
  points, with `bitcoin_p2pkh` at BIP44 and `bitcoin_p2wpkh` at BIP84, all at
  hardened-account0/public-address-index-i. Added BIP44/84 public keys and
  P2PKH/P2WPKH addresses are separately cross-checked in bip_utils and embit.
  Two official CC0 BIP84 receiving examples pin public keys/addresses; source
  URL and check date are in provenance. These extra keys are PUBLIC PRECOMMIT
  ONLY, not authorization for non-Taproot protected display/signing.
  PyCryptodome produces the deterministic AEAD bytes.
- `verify_vectors.mjs` independently checks reference bytes with Node built-ins:
  PBKDF2, AES-GCM encryption/decryption, SHA256, compressed secp points, exact AAD,
  payload parsing, five malformed payload refusals and both-AEAD name/key tamper
  refusals (including both newly precommitted BTC keys). Its separate Base58Check
  and Bech32-v0 encoding checks cover the five new #0 P2PKH/P2WPKH pairs and both
  official BIP84 addresses. It does not claim mathematical Solana subgroup
  validation or repeat account derivation. Typed-metadata negative descriptors are supplied for the
  future owning producer/consumer tests; this script does not implement the
  product validator and does not claim those product tests passed.

## Reproduce offline

Use the environment described by `../hd-corpus-generator/REPORT.md`, its
`requirements.lock` and `corpus_runtime.py` (Python 3.13.11). Do not install into
the owner runtime or access real Vault state.

```sh
/path/to/locked-oracle-env/bin/python tests/fixtures/vault_signing/protected-hd-generator/generate_vectors.py
node tests/fixtures/vault_signing/protected-hd-generator/verify_vectors.mjs
```

Compare resulting fixture bytes/hashes to the contract's frozen hashes. Revision
0.4 proposed three-BTC codec SHA256 is
`acc6b89ac03c9585618f373f9e26e018fd93513227f117195e3869ca9dfb5ffe`.
The JSON fixture warning/provenance are part of its hash. Earlier single-BTC
`66e1045e…` No-findings is not an ACK of this changed fixture.

## Deliberate limits

The AEAD fixtures have `copies: []`, an explicit VMK and deterministic public DEK
and nonces. They are **crypto/codec-only**, not valid setup/unlock/restore
acceptance envelopes and not evidence of root/factor lifecycle handling. Never
weaken actual setup-copy validation to accept them.

Four `legacy_aad` cases preserve the actual producer's existing v2 bytes: untyped,
static field, secp with **absent** curve, and explicit Ed25519. The coordinator also
called actual sandbox `protectedRecordAadHex` at source `49a24983` and matched all
four; the relevant AAD function is byte-unchanged from published-base `6edd2e14`.
This is not a claim that the different, older helper in Avibe's UI copy is the
current sandbox format or that every historical format is newly supported.

All derivation/crypto work here is small, sequential and stateless. Product
implementation tests must additionally prove canonical input validation, all
non-signing refusals, trusted popup/cancellation/UV, storage/readback atomicity,
request completion races, exact released-version downgrade and restoration,
and all funds-safety acceptance. A fixture hash or passing reference probe does
not replace those gates.
