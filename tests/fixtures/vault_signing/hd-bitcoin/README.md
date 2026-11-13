# Public HD Bitcoin oracle

**PUBLIC TEST DATA — NEVER FUND.** All mnemonic words and seeds are deliberately
public. No wallet, custody state, UTXO, service or funded transaction is supplied.
The generated subsets export no derived private key, xpub or chaincode.

These golden files add purpose44/84 receiving keys to the original 37-case
`../hd-seed-corpus.json`, retaining its IDs, public seeds, twenty frozen synthetic
cases, official cases and passphrase/NFKD strata. Removing ONLY the documented
extension restores every original serialized byte (SHA-256
`90a9ff161c6fdd6827e362335a566265956bf9d43e1563003f60431bcf7757e7`).
Indices 0..19 remain **address index under account 0**, never hardened account i.
Mainnet/testnet4 encodings use the same coin0 leaf keys and scripts; `tb`/111 is
an encoding, not evidence of an on-chain network or UTXO. No change-address API is
implied by including published BIP84 change-address data as a formatter anchor.

## Golden inputs and provenance

- `corpus.json`: frozen independent bip_utils/embit + explicit stdlib formulas,
  independently cross-checked by lock-pinned scure/noble. SHA-256
  `5d44457c74b901dd5e4f3eeff877c6a9f47acf416fbbde27591af350e02501fb`.
  Its `bitcoin_extension` preserves generator/library/source revisions and hashes.
  There are 37 ×20 rows, with distinct BIP44/BIP84/BIP86 keys.
- `official-anchors.json`: published BIP84 public keys/addresses and SLIP132 BIP44
  address, with exact primary-source revisions and hashes. SHA-256
  `cc669708551a0d716837dcd3432f1f21d20a17ba8de9425b4a45d796997db148`.
- `transaction-signing-vectors.json`: literal published BIP143 native-P2WPKH
  preimage/digest/signature/full mixed transaction, plus four independent public
  P2PKH/P2WPKH transactions/signatures at #0/#19. SHA-256
  `71aa9a952acb1eddd107d40383b4f79b31f20b82c4f73a1e7e415af544a3ea24`.
  BIP143's legacy input0 is **P2PK**, not P2PKH: its independently calculated
  digest is anchored by the published signature and is clearly labeled calculated.
- `der-signatures.json`: forty deterministic low-S DER signatures for a fixed
  digest, independently generated in embit/coincurve using the public abandon
  seed. `generate_der_signatures.py --verify` reproduces exact bytes and checks
  every derived point against the frozen corpus. These are consumer test inputs,
  not proof that an avault binary signed anything.

The complete initial independent oracle also checked 3700 public points, 740
Taproot output keys, 4440 Bitcoin encodings and 1480 legacy/BIP143 digests across
Python and JavaScript; the integration deliberately keeps only the necessary
golden data, small replay tools and existing shared dependency lock, not a copied
environment or duplicated old generator. Product tests use cryptography and the
production public-only consumer, never the independent generator as their expected
value source.

## Offline reproduction

The existing `../hd-corpus-generator/requirements.lock` is the only oracle Python
dependency source. These are test-tool dependencies, not production dependencies.
Use that directory's interpreter/dependency setup instructions (Python 3.13.11 and
artifact-hashed lock). From this directory, with that isolated interpreter:

```sh
"$ORACLE_PYTHON" -B verify_corpus.py
"$ORACLE_PYTHON" -B generate_der_signatures.py --verify
```

`verify_corpus.py` independently rederives every added point with embit and
bip_utils, roundtrips main/testnet addresses, proves the original corpus bytes,
and rechecks transaction digests/DER/assemblies and the published BIP143 anchor.
Neither command imports product code, accesses a wallet, signs a funded key,
starts a service, contacts a network or writes in verification mode. Run the
product owning contracts with `pytest tests/test_vault_hd_bitcoin.py`.

## Deliberate limits

A DER signature excludes the transaction sighash byte. The caller appends `01`
for the SIGHASH_ALL examples after local verification and builds the scriptSig or
witness. BIP143 includes the exact prevout amount and P2PKH scriptCode, not the
witness program. Legacy sighash does not commit to the prevout amount. Raw digest
signing does not attest transaction intent or fee. There is no new protected
Legacy/SegWit grammar, broadcast API or claim of consensus/script-engine coverage.

Released-binary custody, the combined Web/backend/avault Incus exercise, state
upgrade/restore with old binaries, physical/browser verification and separately
authorized disposable testnet4 receive/spends remain acceptance gates; public
vectors must never receive funds.
