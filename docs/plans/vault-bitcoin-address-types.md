# Standard HD Bitcoin address types

Status: Agreed consumer contract, 2026-11-13. Product P1 for 3.2.6; stable waits.
The avault producer and Web consumer ACKed this profile and the API projection.
Owners: backend/API/CLI/Skill `sesx9u9m2kqeu`; avault `sesw5vhkrcnbg`;
Web `ses22xu7v27qa`; merge/Incus lead `sesk4xcxve4ev`;
release/pin lane `sesjzdekjp6ak`. Protected HD remains a separate 3.2.7 change.

## Outcome and fixed profile

One BTC row offers only script types the selected key and custody implementation
can sign for. Ordinary secp256k1 standard keypairs offer their existing three
address formats under one public key. HD wallets use THREE DISTINCT derived keys;
re-encoding the BIP86 key as bc1q is not BIP84 support. No arbitrary paths, xpub
or chain-code export, change addresses, extra account indices, broadcast API, or
new protected-HD signing grammar is introduced.

Product fixed these paths on November 13 at 14:04 +08. Shared index i is 0..19:

| address_type | UI label / mainnet prefix | fixed HD path | scheme |
| --- | --- | --- | --- |
| p2pkh | Legacy / 1 | m/44'/0'/0'/0/i | ecdsa-secp256k1-der |
| p2wpkh | Native SegWit / bc1q | m/84'/0'/0'/0/i | ecdsa-secp256k1-der |
| p2tr | Taproot / bc1p | m/86'/0'/0'/0/i | schnorr-secp256k1-bip341-keypath |

Omitted Bitcoin address_type stays the EXACT shipped Taproot derivation and wire.
The index is address_index under account 0, NOT hardened account i. Account #0
matches the corresponding reference-wallet first address. Do not promise Phantom
hardened-account-i parity for #1+, or remap already shipped receiving addresses.
A UI switch does not move or convert funds between addresses.

## avault producer wire (joint ACK November 13, 14:09)

New one-shot `avault keypair derive-public` takes NO argv options. Stdin has
exactly:

```json
{"name":"WALLET","key_envelope":{"ciphertext":"...","nonce":"...","wrap_meta":"..."},"profile":"bitcoin-address-types-v1"}
```

Only authenticated STANDARD HD seed envelopes are accepted. Protected material,
unknown fields, ordinary key envelopes and bad names/versions are refused. Open
inside avault and return public material only; no envelope write, seed replacement,
reencryption, plaintext, private key or chain code:

```ts
{
  type: "hd-seed", profile: "bitcoin-address-types-v1",
  accounts: Array<{
    account: number; evm: string; bitcoin: string; solana: string;
    bitcoin_p2pkh: string; bitcoin_p2wpkh: string;
  }>
}
```

Exactly 20 ordered rows. Existing `bitcoin` remains the original compressed BIP86
INTERNAL point. Extra Bitcoin fields are compressed secp256k1 points at purpose
44/84 respectively. Existing EVM/Solana fields are unchanged. The response profile
versions the PUBLIC RESPONSE, not a caller-selected arbitrary path.

Import adds explicit `--profile bitcoin-address-types-v1` to the existing
`keypair import --type hd-seed --format raw-seed [--blind-box]` command. That result
is the above response plus `envelope`. ABSENT profile preserves the entire old
result shape; older Avibe importers reject unsolicited extra fields.

Sign adds optional `derivation.address_type` with exactly p2pkh/p2wpkh/p2tr, valid
ONLY with chain=bitcoin. Scheme can be omitted or must match the selected type.
Unknown/non-Bitcoin type, invalid account and mismatched scheme fail before seed
access. v0.1.11 must refuse new fields/options/verb; no fallback. Existing omitted
selector/default imports and all non-Bitcoin/ordinary calls retain their bytes.
A new released avault and managed pin are required for the additional types;
release number/assets remain owned by the release lane.

## Avibe storage and public projection

No DDL migration: additive public metadata in the existing hd-seed row. Keep kind,
protection, signer_kind, secret id, ciphertext/nonce/RAW wrap_meta and every old
public key intact. The canonical stored account rows gain `bitcoin_p2pkh` and
`bitcoin_p2wpkh`; public metadata records the validated extension profile. Enabled
state, names, per-account always_ask, last_signer, usage and all other metadata are
not reset. Row-wide/per-account confirmation policy still applies across every
Bitcoin type. Callers never supply or replace stored public keys through PATCH.

For existing wallets, a public-preparation operation captures the accessed
standard HD row's id/envelope/base public table, invokes derive-public OUTSIDE any
SQLite writer transaction, validates all 20 rows and exact existing-key equality,
then reserves the writer, re-reads identity/envelope/base keys and merges only the
two new keys into the CURRENT mutable account metadata. A concurrent rename,
policy edit, enable or sign must not be lost. Two successful equal preparations
are idempotent; different identity/envelope/base keys refuse. Failure/interruption
leaves the complete old wallet usable; never overwrite/re-import/reseal/delete it.
The seed and machine custody state, not the words, suffice for this extension.
Missing custody or old avault produces a clear refusal and no wallet mutation.

New HD create can explicitly request the extension using
`keypair.profile:"bitcoin-address-types-v1"` alongside its existing
`{type:"hd-seed",source:"import",format:"raw-seed"}` spec. Omission remains the old
create surface. New UI requests the profile and sends expected account-0 ETH,
Legacy, Native SegWit, Taproot and Solana addresses. Expectations only reject,
never supply stored keys; all required checks and initial policy precede INSERT,
audit/provision/publication. Legacy three-address expectations stay accepted for
legacy create; extended create requires all five. Post-save UI comparison remains
an additional blocking check, never a substitute for atomic persistence.

Public enabled-account projections retain existing fields and add only:

```ts
{
  btc_legacy?: string, btc_segwit?: string,
  bitcoin_paths?: {p2pkh:string; p2wpkh:string; p2tr:string},
  bitcoin_address_types: Array<"p2pkh"|"p2wpkh"|"p2tr">
}
```

`btc_taproot` and `paths.bitcoin` remain the old values. The type list is a
server-owned capability, not a client inference from address prefixes. A prepared
HD secret also exposes `bitcoin_profile:"bitcoin-address-types-v1"`; an old
unprepared secret omits that field. Installed-av​​ault capability is probed/cached
outside serialization and SQLite writer transactions; new types require both
validated stored points and a supporting installed avault. Temporary custody
failure never changes or deletes the public identities.

Ordinary keypair responses use the same type tokens/list beside their existing
signing_addresses fields; protected ordinary keys currently offer only supported
typed Taproot, not Legacy/Native merely because public formatting can compute
them. Ed25519 rows offer no BTC types. The UI defaults to p2tr when offered,
otherwise the first server-listed type. A single type has no switch. If a list is
missing on an older response, show only its existing btc_taproot row when present;
never infer the other types or retain three BTC rows. The selected display type is
browser-session preference only, not a persisted default, policy change or CLI
signing selection.

### Existing-wallet preparation API

`POST /api/vault/secrets/{name}/bitcoin-address-types` has no caller-selected paths,
keys, profile or account indices. It uses the existing management-API CSRF and
editor-or-higher gate plus resource-management authority, not use-only authority.
Only standard HD seed wallets are eligible. Successful first or idempotent repeat
returns `{ok:true,secret:updated_secret}`, matching account PATCH. The wallet's
Accounts dialog may call it once on open when profile/types are missing and the
user can manage; never call during list serialization/render. Non-managers see
only already prepared capabilities and do not trigger a write.

Failure is atomic and preserves the old wallet:

- `403 forbidden` for missing management authority.
- `404 secret_not_found` for a missing target.
- `409 hd_bitcoin_types_state_changed` for concurrent identity/envelope/base-table
  replacement; a later explicit retry starts from the new snapshot.
- `409 hd_bitcoin_types_integrity_failed` for invalid producer output, disagreement
  with any old identity, or inconsistent existing extension keys.
- `avault_upgrade_required` for a runtime that does not support the profile/verb;
  use the existing HD avault-upgrade notice and never retry a legacy profile.

The Web refreshes the returned secret in place. It keeps existing public data on
failure, shows the upgrade notice for old avault, and visibly reports preparation
failure for integrity mismatch; do not silently swallow a custody/public identity
error. Permission/no-longer-existing errors follow ordinary management behavior.

Extended create's expected_account0 has exactly `eth`, `btc_legacy`, `btc_segwit`,
`btc_taproot` and `solana`. An unsupported avault produces
`avault_upgrade_required` before persistence, mapped to the same existing notice;
there is NEVER silent fallback to a legacy-three-address import. Omitted profile
still deliberately selects the unchanged old API surface.

## Signing input and completion

CLI adds `--address-type p2pkh|p2wpkh|p2tr` only alongside `--chain bitcoin` for HD.
REST sign accepts address_type with chain/account; the normalized persisted
selection carries it for the new branch. Omitted selector keeps legacy Taproot
requests byte-compatible. No selector chooses an ordinary key's arbitrary path.

Every stage must bind selected script type: preflight/version check, public key
selection, request/card, signed release, avault request, claim/completion, signature
verification and audit. A caller cannot change/omit a stored new type on approval
or completion. Same-name recreation, changed envelope/key, disabled/wrong account,
wrong curve/scheme or changed input/context refuses. Standard pending->signing->
approved claim semantics, per-account policy and original-requester attribution
are unchanged. Public-projection refresh cannot weaken approval policy.

Every successful STANDARD HD Bitcoin sign result with an explicit address_type
also returns `signing_public_key` alongside its existing signature and derivation:
`{"curve":"secp256k1","public_key":"<lowercase 33-byte compressed SEC1 hex>"}`.
This applies to explicit p2pkh, p2wpkh and p2tr selections. For p2tr the returned
point is the selected internal BIP86 point, NOT its tweaked output key. It exposes
no private material and does not add fields to public account/list projections.

The returned point must be the same frozen selected key against which this
signature was verified; never re-resolve it by wallet name after signing or
accept a caller-supplied replacement point. Immediate headless results, persisted
approved results, claim/await responses, CLI JSON and approval continuations retain
that same public-key field. Omitted address_type and ordinary-key/EVM/Solana calls
keep their existing result shape. Existing failure, atomic audit/usage and
identity-change refusal rules still apply; a public result is not a new authority.

Legacy and Native use the EXISTING low-S DER ECDSA primitive. The caller constructs
the correct legacy or BIP143 digest; avault does not parse Bitcoin transactions.
For the smallest tested spend use SIGHASH_ALL=0x01; append the sighash byte AFTER
DER (not inside the signature API), and assemble Legacy scriptSig or Native
witness with the selected compressed public key. BIP143 binds exact prevout amount
and P2PKH scriptCode for the selected P2WPKH key. Verify the returned DER against
the returned selected public key and independently check its HASH160 against the
selected address/script before transaction assembly/broadcast. Taproot retains
BIP341 tweaked-output verification and its existing bounded typed context.
Raw-digest signing does not attest amounts, fees or transaction intent. Do not
claim a new typed Legacy/Native transaction grammar or protected support.

## Acceptance and delivery gates

Extend the owner's mnemonic acceptance and owning repository tests:

- Official BIP44/84/86/143 fixtures and >=20 public random 12/24-word cases with
  empty/nonempty/NFKD passphrases; every enabled index 0..19 and all three BTC types.
- Displayed address = independent derivation = persisted public identity = key
  that verifies the actual signature; preserve all original EVM/Solana/Taproot.
- New create exact five-address checks, #0 preview/ready state, and caller-built
  legacy/BIP143 scripts/digests/witness and DER+sighash-byte verification. Build
  Legacy scriptSig and Native witness using only ordinary consumer inputs and the
  returned bound public key, not private DB reads, fixture-injected public points
  or extra custody commands. Cover headless and approved/await/continuation output
  retention, all three explicit selections, and unchanged omitted-call results.
- Wrong type/account/scheme/non-BTC inputs, missing/changed approvals, cross-type
  signature substitution, corrupt envelope/point, old avault and unsupported tier
  refuse with no false successful use/audit.
- Existing RC-wallet in-place backfill, concurrent prepare+rename/enable/policy/sign,
  interruption/rollback/lost response, restart/DB+WAL/full-state restore and old-reader
  compatibility; no re-import, old Taproot address/policy/seed never moves.
- One lead-authorized combined Incus run uses backend+Web+released avault pin and
  #3637 (exercise Vault fetch/run once), <=1.5GB synthetic fixtures, one heavy job.
- Separate disposable-wallet testnet4 receive/spend per script type at #0/#1,
  signature verified locally before broadcast; explicit budget/destinations,
  never public-vector funds. Owner hand-test is a later visible step.
- Independent GPT/xhigh exact-head adversarial review, persistent combined PR
  review/CI Watch and +10-minute pickup Tasks; final funds gate before merge-ready.

A source merge does not authorize a release, pin, Mac install/service restart or
owner mainnet funds. Product gives Vault/signing go; lead merges. Each release,
pin and regression cohort retains its own exact source/artifact evidence.
