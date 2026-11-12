# Standard-tier HD seed wallets

## Change contract

One `kind=keypair`, `signer_kind=hd-seed` row stores avault's typed seed envelope and 20 public account sets in `public_meta.hd_accounts`. No database migration. The browser performs BIP39 and seals the 64-byte seed to avault with the existing blind-box import context (`keypair-import`, `hd-seed:raw-seed`). Python never receives words, seed bytes, private keys or chain codes. Create/import is atomic: one row plus its audit, or none. Protected HD wallets and mnemonic reveal are out of scope.

Accounts #0–#19 share their index across EVM (`m/44'/60'/0'/0/i`), Bitcoin (`m/86'/0'/0'/0/i`) and Solana (`m/44'/501'/i'/0'`). Only #0 starts enabled. Enabling the next account is a metadata operation, with no avault/key unlock. Names and ask-before-signing are per account. Successful signing records the initiating Session and timestamp through the existing signed audit path, not through a second history scan.

Signing requires `chain`; `account` defaults to 0 and must be enabled. The chain fixes the scheme: recoverable ECDSA, BIP341 key-path Schnorr, or Ed25519. Plain keypairs reject these options and retain their existing wire calls. Every HD result is verified with the selected stored public key. Approval binds the record id, chain, index, public key, envelope fingerprint and payload; it cannot be replayed across accounts or a replacement row.

Policy is checked when an operation captures its signing target. A toggle affects subsequent operations, not an already-started avault operation; the database writer lock is never held across custody. Completion still rechecks record/key/envelope identity. New HD wallets have no row-wide ask policy; if an unexpected older/internal row-wide `always_ask` exists, it remains a conservative OR restriction rather than being silently ignored. Headless HD delivery requires successful audit/account bookkeeping; a stale record or failed audit withholds the signature. Existing ordinary-key best-effort audit behavior is unchanged.

## Web/CLI contract

- `POST /api/vault/secrets`: `{name, kind:"keypair", protection:"standard", signer_kind:"hd-seed", keypair:{type:"hd-seed",source:"import",format:"raw-seed"}, blind_box, description?, tags?, expected_account0?}`. Browser-generated and imported words share this call. Client-supplied account/public-key metadata is rejected.
- Optional `expected_account0` is exactly `{eth, btc_taproot, solana}`: the browser's independently derived public account-0 addresses, in EIP-55/mainnet BIP86/base58 form. Shape is checked before custody; exact strings are compared with addresses derived from avault's returned public keys before any wallet row/audit is inserted. A mismatch returns `hd_address_check_failed` (409), not a committed wallet requiring client-side deletion. Expectations never replace trusted public keys or persist as metadata. Non-HD creation rejects the field; omission preserves old-client compatibility. New UI callers must not retry without the field after refusal. This is an additional consistency check, not proof that the browser derived from the words it actually displayed; the separate browser acceptance test still owns that link.
- `POST /api/vault/secrets/{name}/accounts` with `{}` enables the next account.
- `PATCH /api/vault/secrets/{name}/accounts/{account}` accepts `name` (nonempty string, max 80 characters, or null to reset) and/or `always_ask` (boolean). Only enabled accounts are editable.
- Secret metadata returns `account_limit:20`, and enabled `accounts:[{account,name,eth,btc_taproot,solana,paths:{evm,bitcoin,solana},always_ask,last_signer:{session_id,at}|null}]`. Default `name` is null so the Web can localize its fallback. Raw public keys are not exposed by the list surface.
- `vibe vault list` / `find` add `signer_kind` and `accounts:[{account,name,eth,btc_taproot,solana}]` for HD rows.
- `vibe vault sign NAME --chain evm|bitcoin|solana [--account N]` uses `--digest` for EVM/Bitcoin and exact `--message-hex` for Solana. An explicitly supplied scheme must match the chain.

## Compatibility and release boundary

Creation and signing require avault >=0.1.11; older/unknown binaries fail with an upgrade message before persistence or custody. No version/pin/release change is included. Development integration uses a master-built binary in test-owned state until the owner approves the release.

Existing keypair schema, envelope, AAD and call bytes remain unchanged. Old Avibe can list/delete an HD row, rejects signing because its signer kind is not local, and refuses run/fetch/inject/reveal because its kind is keypair. Existing envelope cryptography separately rejects the HD type. The released-3.2.5 runtime rollback check passed in an isolated Incus environment through the lead's serialized slot (details below).

## Validation inventory

- API import boundary: avault-derived metadata only; malformed output, unsupported protection, duplicate name and old binary leave no row.
- Account owner boundary: contiguous enable, cap, names, per-account policy, ACL management and concurrent edits preserve unrelated metadata.
- Signing owner boundary: chain/scheme/input/index validation, enabled check, per-account approval, exact approval binding, signature verification and last signer for headless and approved uses.
- CLI/parser: additive flags, default account, plain-key refusal, list formatting; existing signing tests pin legacy wire compatibility.
- Hermetic real-avault integration (test seed only): import, all three chain signatures, #0/#1 addresses, non-deliverability and typed envelope. No owner's keychain or local runtime writes.
- Focused pytest + changed-file Ruff, PR CI and exact-head Codex review. Shared Incus requires a serialized slot. Owner approval is required before merge. Public documentation remains held until stable.

## Dependencies / progress

- avault#49 merged: `fcefea2` (`docs/DESIGN.md`, Standard-tier HD seed wallets).
- UI implementation is a separate lane; this document is the backend contract.
- Backend/CLI implementation complete; no schema or managed binary pin changes.
- Focused HD contracts: 74 passing cases before the CLI race regression was added. Existing API/CLI/Solana: 605 passed, 6 pre-existing skips. Expanded service/REST/ACL/parser set: 453 passed, one explicit opt-in master-binary skip. Changed-file Ruff, Skill validation and UI build passed. Revalidation and PR review/CI remain in progress.
- Real master-built avault API consumer check passed: honest 0.1.10 version first rejected, then a test-only probe override exercised real browser blind-box import and six verified signatures (#0/#1 × EVM/Bitcoin/Solana). No production version bypass exists. Test store was explicit file-only with isolated HOME/XDG/AVAULT_HOME, not the owner's keychain.
- On-path bug fixed: the CLI previously printed success with a null signature if ask-before-signing changed between its preflight and the API's later policy check. It now hands off the API's one pending request, retaining command metadata and `--no-callback`.
- Released runtime rollback passed: one unseeded Incus worktree environment ran tag 3.2.5 (`ea402b8bc`) → HD build `7f222a584` → the same released tag. The new storage owner persisted the independently generated encrypted test-wallet fixture into the database initialized by the old reader. After downgrade, the released APIs listed and deleted the row; inline signing and sign-request creation refused before custody; run/fetch/inject and value-envelope reads refused; identity, ciphertext and public metadata were unchanged by downgrade or rejected uses. A custody-failure sentinel prevents missing keys/binaries from being mistaken for a valid refusal. All updates and final service health passed. The environment used 4 CPUs/8 GiB without nesting and was deleted through the runner; no other project or master hold was touched. This proves persisted-data compatibility, not a second real custody import (covered separately above).
- Pending: end-to-end Web acceptance with the separate UI lane; fresh exact-head Codex/CI after integration with the protected-signing guidance below; owner merge approval.

## Integrated protected-signing guidance

PR #3629 corrected the stale blanket protected Bitcoin/Solana prohibitions on master. This branch retains its bounded typed Bitcoin and exact-message Solana guidance and examples alongside the standard-HD additions, at Skill version 0.9.0. HD wallets remain standard-only; no new protected-HD support or runtime/pin change is claimed.
