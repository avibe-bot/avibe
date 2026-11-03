# Protected Bitcoin public fixtures

These contain deliberately public scalars (1, 2, 3, 6, 11). **Never fund them.**
They are test evidence, not a wallet, a chain-membership proof or a request format.

- `core-fixtures.json`, SHA256
  `edfca8bc9e60666b40ebbaf70b71d74b59a48cfef45befa5add29996c1e74d73`:
  independent Bitcoin Core v31.1 functional-framework output, commit
  `9be056a8a72b624dae9623b2f7bded92c2a21c91`. Four cases cover every raw P / Q
  parity combination, invented witness/non-witness funding parents, self-transfer
  and recipient-plus-return. Parents' previous outpoints are invented, so these
  fixed transactions are **not** mempool candidates. Expected digests/signatures
  come from unchanged upstream Core code, not either product implementation.
- `codec-fixtures.json`, SHA256
  `f31e2c387327ab42fb1bc4b931768a75c9eb0f852b495478cd80fa23c9f7fb7b`:
  supplemental independent Core fixtures for five output templates on four
  networks (20 cases), plus 12 same-key legacy/v0 refusals. Full expected
  addresses come from the native Core `bitcoin-tx` utility; signatures/digests
  come from the same unchanged functional framework. Its network flags choose
  address encoding, not chain membership.
- `policy-fixtures.json`, SHA256
  `ef784cea89d9c22ddd10189776cd31ad5e244feb6ad686d2d7d801260b16d5d1`:
  nine valid boundaries (all three sequences, exact/above warning thresholds,
  fee ceiling and dust floor) and 56 malformed/unsupported contexts. The
  expectation includes the intended rejection reason, so stale digest failures
  cannot hide a missing parser/policy check. `generate_policy.py` checks pinned
  complete Core source files, imports them unchanged and reproduces the JSON:

  ```sh
  python -B tests/fixtures/vault_bitcoin/generate_policy.py \
    --framework /path/to/bitcoin-core/test/functional --check
  ```

Both language consumers use byte-identical fixture files. Tests also prove the
service/API/CLI reject before request/notification/signing side effects, verify
signed release contents, and revalidate persisted protected contexts at settlement.

Separate live-code acceptance belongs to vault-sandbox PR #25:
`scripts/core-acceptance.py` creates a new isolated offline regtest datadir,
mines public synthetic coinbases, invokes the actual `main.ts` RPC with simulated
fresh-UV authorization, and passes resulting witnesses to `testmempoolaccept`.
It retains a receipt for four accepted RPC signatures and 16 signature-invalid
negatives. False amounts use the actual RPC; false-script and raw-P negatives use
the real primitive because the typed RPC already refuses the wrong script.
This is not physical passkey E2E, Signet funding, deployment or broadcast evidence.
