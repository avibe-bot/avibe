# Independent public HD corpus

**All words, passphrases and seeds here are intentionally PUBLIC TEST MATERIAL. Never fund or reuse any of these wallets.** Synthetic cases were generated solely for testing and never intentionally funded. Official reference addresses may have historical third-party activity; no chain lookup was performed.

`../hd-seed-corpus.json` is the shared backend/browser oracle, joined by `id` and `seed_sha256`. It contains 37 entries × 20 accounts: 20 OS-CSPRNG synthetic cases (five in each 12-/24-word × empty/nonempty-passphrase group), all 16 English 12-/24-word official BIP39 vectors, and the no-passphrase BIP86 vector. Corpus SHA-256: `90a9ff161c6fdd6827e362335a566265956bf9d43e1563003f60431bcf7757e7`.

## Reproduce

From this directory, create a disposable Python 3.13.11 environment outside the repository and install the complete artifact-hashed lock:

```sh
uv venv --python 3.13.11 /tmp/hd-corpus-verify-venv
uv pip sync --python /tmp/hd-corpus-verify-venv/bin/python --require-hashes requirements.lock
/tmp/hd-corpus-verify-venv/bin/python generate_corpus.py --verify
```

`--verify` regenerates in memory and byte-compares the corpus and `self-checks.json`, without writing or creating entropy. Do not use `--init-random` to reproduce: frozen `random-inputs.json` is already present and initialization refuses to overwrite it. Without flags, the script rewrites the two outputs using the same frozen inputs. Python/package versions are included in output; use the pinned versions for byte-identical regeneration. Dependency installation needs network/cache; generation itself is offline, sequential and bounded to this small corpus. No product, avault, browser or runtime is called.

## Contract

Paths are explicit, never library coin-class defaults:

- EVM: `m/44'/60'/0'/0/i`; 65-byte uncompressed SEC1 public key and EIP-55 address.
- Bitcoin: `m/86'/0'/0'/0/i`; 33-byte untweaked internal public key; BIP86 even-y lift and empty-tree TapTweak; 32-byte output key; `5120 || output_key` script and bech32m address. Testnet `tb1p` is **test encoding of the identical script**, not coin type 1 derivation or a production network option.
- Solana: `m/44'/501'/i'/0'`; raw 32-byte public key and Base58 address.

NFKD is applied explicitly to mnemonic and passphrase before PBKDF2. Passphrase whitespace is significant and preserved. All corpus words are canonical lowercase English; UI acceptance/rejection of extra whitespace or mixed case is not specified by this oracle. `self-checks.json` includes NFC/NFD/NFKD passphrase equivalence and changed-passphrase negatives.

## Independent checks and provenance

Python bip_utils/embit expectations are independent of browser scure/noble and Rust avault. BIP32 public keys are compared across bip_utils/embit. Bitcoin even-y lift/TapTweak is expressed explicitly and cross-checked against embit p2tr, including both original parities. Solana SLIP-0010 is cross-checked with stdlib HMAC and PyNaCl. EVM payload is checked with Keccak-256. These do not claim every low-level dependency is an independent implementation.

The generator verifies 24 English and 24 Japanese known BIP39 seeds/root keys and all three published BIP86 examples. `sources/manifest.json` pins upstream commits and content SHA-256 for the BIP39-linked Trezor/Japanese vectors and Bitcoin BIP39/86/350 specifications. Those snapshots are used offline. This corpus is an oracle, **not evidence that production custody or UI passed it**; actual imports, all-account signatures and rendered-address comparisons are separate acceptance runs.
