# Public signing fixtures

All keys/seeds here are intentionally public synthetic test material. Never use
these keys to receive funds or to authenticate a production instance.

- `hd-seed-wallet.json`: public BIP39 `abandon` ×11 + `about`, no passphrase.
  Imported by avault master `fcefea28a63016085ead3650f05c7b272bae1589`
  (unreleased HD implementation, binary still honestly reports 0.1.10), built
  with `cargo build --locked -p avault-cli -j 2`. The isolated file store supplied
  only ciphertext, 20 × 3 public keys, and six account #0/#1 signatures (digest
  `01` ×32 for EVM/Bitcoin; exact `deadbeef` message for Solana). Independently
  checked with scure/noble BIP32, SLIP-0010, ECDSA recovery, BIP86 tweak and
  Ed25519 verification, not the Avibe code under test. Bitcoin #0/#1 addresses
  agree with BIP86's public receive vectors. No plaintext seed, child private
  key, chain code or machine key is included. The envelope is AAD-bound to
  `HD_TEST_WALLET` and is opaque test data; CI tests do not need its master key.

- `hd-bitcoin/`: additive purpose44/84/86 public corpus (37 cases ×20 indices),
  independently generated low-S DER fixtures and published BIP84/143/SLIP132
  anchors. Its README records hashes, independent replay tools, index semantics
  and the distinction between public-vector tests and real custody/funds gates.

- `bip340.csv`: bitcoin/bips `bip-0340/test-vectors.csv`, downloaded 2026-11-02.
  SHA256 `34c9d1d9c3a88d524bc80778540dc43f8306ec249a7485293063c376db851c2d`.
- `bip341-wallet.json`: bitcoin/bips `bip-0341/wallet-test-vectors.json`.
  SHA256 `403e19fb81dd1f31e745699216308f61fb403774b2aafa87b631b8f7c042d37f`.
  The integration consumes the empty-tree first input only; tree signing is not
  supported. Its witness includes a final sighash byte, not part of the signature.
- `protected-sign-context.json`: byte-for-byte copy of `avibe-bot/vault-sandbox`
  PR #25, commit `fee07fe7f0153ae4e4b29dd0154703dc236fbce4`, `src/fixtures/`.
  The sandbox consumes the same bytes. This fixture must traverse the full
  Avibe input validator, not only its signed-context serialization helper.
- `canonical-json-utf16.json`: canonical bytes and SHA256 regenerated with that
  commit's actual JavaScript consumer (`src/operationContext.ts::stableJson`),
  not with the Python producer under test. The independent output agrees with
  the previous fixture bytes; its provenance is corrected accordingly.

- `rfc8032-ed25519.json`: RFC 8032 section 7.1 Ed25519 TEST 1 (empty message),
  TEST 2 and TEST 3, copied from the RFC.
  https://www.rfc-editor.org/rfc/rfc8032#section-7.1
- `protected-solana-sign-context.json`: the protected Solana signing binding,
  shared byte for byte with `avibe-bot/vault-sandbox`. SHA256
  `bd68ae39dc1eec511b4b4dbe084adbfe974d5f866a0e461e6122047eefd0746a`.
  `release` is `{name, scheme: "ed25519", messageHex, publicKey, signingContext:
  {kind: "solana-transaction-v1", messageHex}}`: the exact message, never a
  digest. `messageHex` is a 150-byte legacy Solana message assembled by hand:
  one signer (the RFC 8032 TEST 1 public key), the TEST 2 public key as
  recipient, a System Program transfer of 1,000,000 lamports, and recent
  blockhash bytes `0x01..0x20`. `signingSeedHex` is the RFC TEST 1 secret key
  and `expectedSignature` is pure Ed25519 over the message under it.
  No Avibe code produced it: `canonicalUnsignedContext` is the output of the
  sandbox's own `signedOperationContextMessage` (`src/operationContext.ts`,
  commit `52b690a47f88f9eb29e3efa1d8ce706f7afc0b78`, bundled with esbuild as
  below plus `--define:__SANDBOX_VERSION__='"fixture"'` and
  `--define:__SANDBOX_BUILD_HASH__='"fixture"'`), and both signatures were made
  with the `cryptography` library. The address literals in
  `test_vault_solana.py` are published Solana program ids, plus the TEST 2
  public key encoded by a base58 routine outside Avibe.

To regenerate the canonical expectation independently, bundle the **consumer**
from that sandbox checkout using its own installed dependencies:

```bash
./node_modules/.bin/esbuild src/operationContext.ts --bundle --platform=node \
  --format=esm --outfile=/absolute/path/to/scratch/sandbox-operation-context.mjs
```

Then from the Avibe checkout, print the regenerated fixture (review the output
before replacing it). This invokes no Avibe Python serializer:

```bash
node --input-type=module - /absolute/path/to/scratch/sandbox-operation-context.mjs \
  tests/fixtures/vault_signing/canonical-json-utf16.json <<'JS'
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
const { stableJson } = await import(pathToFileURL(process.argv[2]).href);
const { payload } = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const canonical = stableJson(payload);
console.log(JSON.stringify({
  source: 'vault-sandbox src/operationContext.ts stableJson, commit fee07fe7f0153ae4e4b29dd0154703dc236fbce4; generated with the actual JavaScript consumer via esbuild and Node.js',
  payload, canonical,
  sha256: createHash('sha256').update(canonical, 'utf8').digest('hex'),
}, null, 2));
JS
```

The same consumer's `signedOperationContextMessage` was also checked against
`protected-sign-context.json`'s `canonicalUnsignedContext`. The scalar limits
and astral/BMP key pair intentionally distinguish JS UTF-16 ordering from
Python's default code-point ordering.

Upstream BIP sources and licenses:
https://github.com/bitcoin/bips/tree/master/bip-0340
https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki
https://github.com/bitcoin/bips/tree/master/bip-0341
https://github.com/bitcoin/bips/blob/master/bip-0341.mediawiki

BIP86 address expected values in `test_vault_addresses.py` are from its three
published receive/change vectors, not computed by the implementation under test:
https://github.com/bitcoin/bips/blob/master/bip-0086.mediawiki
