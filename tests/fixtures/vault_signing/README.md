# Public signing fixtures

All keys/seeds here are intentionally public synthetic test material. Never use
these keys to receive funds or to authenticate a production instance.

- `bip340.csv`: bitcoin/bips `bip-0340/test-vectors.csv`, downloaded 2026-11-02.
  SHA256 `34c9d1d9c3a88d524bc80778540dc43f8306ec249a7485293063c376db851c2d`.
- `bip341-wallet.json`: bitcoin/bips `bip-0341/wallet-test-vectors.json`.
  SHA256 `403e19fb81dd1f31e745699216308f61fb403774b2aafa87b631b8f7c042d37f`.
  The integration consumes the empty-tree first input only; tree signing is not
  supported. Its witness includes a final sighash byte, not part of the signature.
- `protected-sign-context.json` and `canonical-json-utf16.json`: byte-for-byte
  copies of `avibe-bot/vault-sandbox` PR #25, commit
  `fee07fe7f0153ae4e4b29dd0154703dc236fbce4`, `src/fixtures/`.
  The sandbox consumes the same bytes. The protected fixture must traverse the
  full Avibe input validator, not only its signed-context serialization helper.

Upstream BIP sources and licenses:
https://github.com/bitcoin/bips/tree/master/bip-0340
https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki
https://github.com/bitcoin/bips/tree/master/bip-0341
https://github.com/bitcoin/bips/blob/master/bip-0341.mediawiki

BIP86 address expected values in `test_vault_addresses.py` are from its three
published receive/change vectors, not computed by the implementation under test:
https://github.com/bitcoin/bips/blob/master/bip-0086.mediawiki
