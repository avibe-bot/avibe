# Standard-tier HD seed wallets

## Change contract

One `kind=keypair`, `signer_kind=hd-seed` row stores avault's typed seed envelope and 20 public account sets in `public_meta.hd_accounts`. No database migration. The browser performs BIP39 and seals the 64-byte seed to avault with the existing blind-box import context (`keypair-import`, `hd-seed:raw-seed`). Python never receives words, seed bytes, private keys or chain codes. Create/import is atomic: one row plus its audit, or none. Protected HD wallets and mnemonic reveal are out of scope.

Accounts #0–#19 share their index across EVM (`m/44'/60'/0'/0/i`), Bitcoin (`m/86'/0'/0'/0/i`) and Solana (`m/44'/501'/i'/0'`). Only #0 starts enabled. Enabling the next account is a metadata operation, with no avault/key unlock. Names and ask-before-signing are per account. Successful signing records the initiating Session and timestamp through the existing signed audit path, not through a second history scan.

Signing requires `chain`; `account` defaults to 0 and must be enabled. The chain fixes the scheme: recoverable ECDSA, BIP341 key-path Schnorr, or Ed25519. Plain keypairs reject these options and retain their existing wire calls. Every HD result is verified with the selected stored public key. Approval binds the record id, chain, index, public key, envelope fingerprint and payload; it cannot be replayed across accounts or a replacement row.

Policy is checked when an operation captures its signing target. A toggle affects subsequent operations, not an already-started avault operation; the database writer lock is never held across custody. Completion still rechecks record/key/envelope identity. New HD wallets have no row-wide ask policy; if an unexpected older/internal row-wide `always_ask` exists, it remains a conservative OR restriction rather than being silently ignored. Headless HD delivery requires successful audit/account bookkeeping; a stale record or failed audit withholds the signature. Existing ordinary-key best-effort audit behavior is unchanged.

## Web/CLI contract

- `POST /api/vault/secrets`: `{name, kind:"keypair", protection:"standard", signer_kind:"hd-seed", keypair:{type:"hd-seed",source:"import",format:"raw-seed"}, blind_box, description?, tags?}`. Browser-generated and imported words share this call. Client-supplied account/public-key metadata is rejected.
- `POST /api/vault/secrets/{name}/accounts` with `{}` enables the next account.
- `PATCH /api/vault/secrets/{name}/accounts/{account}` accepts `name` (nonempty string, max 80 characters, or null to reset) and/or `always_ask` (boolean). Only enabled accounts are editable.
- Secret metadata returns `account_limit:20`, and enabled `accounts:[{account,name,eth,btc_taproot,solana,paths:{evm,bitcoin,solana},always_ask,last_signer:{session_id,at}|null}]`. Default `name` is null so the Web can localize its fallback. Raw public keys are not exposed by the list surface.
- `vibe vault list` / `find` add `signer_kind` and `accounts:[{account,name,eth,btc_taproot,solana}]` for HD rows.
- `vibe vault sign NAME --chain evm|bitcoin|solana [--account N]` uses `--digest` for EVM/Bitcoin and exact `--message-hex` for Solana. An explicitly supplied scheme must match the chain.

## Compatibility and release boundary

Creation and signing require avault >=0.1.11; older/unknown binaries fail with an upgrade message before persistence or custody. No version/pin/release change is included. Development integration uses a master-built binary in test-owned state until the owner approves the release.

Existing keypair schema, envelope, AAD and call bytes remain unchanged. Old Avibe can list/delete an HD row, rejects signing because its signer kind is not local, and refuses run/fetch/inject/reveal because its kind is keypair. Existing envelope cryptography separately rejects the HD type. The rollback check targets released 3.2.5 in an isolated Incus environment, through the lead's serialized slot.

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
- Pending: lead-serialized Incus rollback check against released 3.2.5; end-to-end Web acceptance with the separate UI lane; exact-head Codex/CI and owner merge approval.

## Follow-ups outside this backend change

The existing Vault Skill still contains older blanket protected Bitcoin/Solana restrictions, while current master has typed protected signing integrations. Those release-sensitive statements should be reconciled with the owning protected-signing workstream and sandbox pin; this change adds only standard-HD guidance and does not claim new protected-HD support.
