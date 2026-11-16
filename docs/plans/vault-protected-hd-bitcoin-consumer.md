# Protected HD Bitcoin ECDSA consumer

## Scope and landed dependencies

Extend the existing protected-HD owner for explicit P2PKH/P2WPKH selections.
Reuse the ordinary Bitcoin parser, budgets, DER closure, request identity/cohort
and completion machinery. Do not add a seed/envelope, full-table validator, root
store, create/reconciliation owner or alternate custody path.

Both owning dependencies and the reviewed all-three sandbox pin are on the
default branch. The preserved independent consumer worktree imported the actual
default branch with a normal merge before implementation. No copied owners or
stacked PRs are needed. The existing `18570864` sandbox already derives all three
purposes, proves the selected index's siblings from the sealed seed and signs
ECDSA. This consumer needs no sandbox build, promotion or registry change.

## Invariants

- The receiving index remains 0..19 at `m/44'/0'/0'/0/i`,
  `m/84'/0'/0'/0/i`, `m/86'/0'/0'/0/i`. It is not hardened BIP44 account i.
- Reuse `vault_hd.BITCOIN_KEYS`: P2PKH selects `bitcoin_p2pkh`, P2WPKH
  selects `bitcoin_p2wpkh`, P2TR selects `bitcoin`. Never re-encode BIP86 as
  another purpose's input key. Omission becomes explicit P2TR only at intake;
  completion must repeat the saved explicit selector.
- The protected-HD owner authenticates the entire typed 20-row envelope and
  immutable cache/profile, and selects an enabled account before public
  projection. The daemon verifies public consistency, not seed derivation.
- Valid existing protected-HD wallets already store both extra purpose points
  in the authenticated table. Enabling this consumer requires no re-creation,
  seed import, backup repeat or immutable envelope rewrite.
- Same-index returns are exactly P2PKH(P44), P2WPKH(P84) and empty-tree
  BIP86(P86). Another receiving index is a recipient; more than one matching
  return output refuses. Do not apply ordinary single-scalar return semantics.
- The pinned sandbox proves the actual seed-derived compressed three BTC
  siblings before review and final use. Do not add unrelated EVM/Solana or
  all-account derivations, a sibling-attestation wire, or interpret DER under one
  point as proof of the siblings.
- New inputs require `ecdsa-secp256k1-der`, matching explicit Bitcoin
  derivation/address type and typed context, independently recomputed digest,
  bounded Legacy parent proof / Native BIP143 input, strict low-S DER and exact
  public-key/final-assembly closure. Preserve existing EVM, Solana, Standard,
  ordinary protected Bitcoin and protected Taproot behavior.
- Freeze saved ID/classification/derivation/selected point/raw envelope/full pin
  at intake. Preserve original requester authority. No writer spans custody;
  signature verification, pending CAS, account use/last-signer and audit settle
  under the existing reserved-writer savepoint.
- Completed GET/await/callback results use persisted frozen key/derivation after
  response loss or later record changes, not current-name reconstruction.
- Receive/sign capability comes only from the complete reviewed pin registry.
  Historical/unknown pins do not gain Legacy/Native from stored points. Existing
  Taproot compatibility remains. Source, runtime and funds acceptance differ.

## Public backend contract

For NEW protected-HD ECDSA requests:

- Delivery `bitcoin_identity` is exactly
  `{kind:"keypair", protection:"protected", signer_kind:"hd-seed"}`.
- Delivery, release and approved result `derivation` are equal:
  `{chain:"bitcoin", account:i, address_type:"p2pkh"|"p2wpkh"}`.
- `signing_key` remains `{secret_id, public_key, envelope_sha256}`. The public
  key is the selected purpose's compressed SEC1 point; raw fingerprint encoding
  remains the existing protected-HD codec.
- Delivery `bitcoin_cohort` is `{pin_id, buildHash, manifestHash}`.
- Signed release fields are exactly `name`, `scheme`, `digest`, `publicKey`,
  `signingContext`, `signingKey`, `derivation`, and `sandboxCohort`. Scheme is
  `ecdsa-secp256k1-der`; `sandboxCohort` contains `buildHash`/`manifestHash`.
  The outer signed operation remains v2 with the saved request ID and expiry.
- Approved GET result contains `type:"signature"`, strict DER `signature`,
  `derivation`, and frozen
  `signing_public_key:{curve:"secp256k1",public_key:"<selected point>"}`.
  DER is 8..71 bytes, low-S, with no transaction sighash byte or recovery ID.
- Public accounts expose only capability-admitted Bitcoin addresses/types and
  corresponding `bitcoin_paths`. Sign cards keep the selected type/address/path.
  `last_signer` remains `{session_id,at}`; the signing audit records derivation.
- NEW collection rows remain proof-free summaries. The existing exact saved-ID
  GET supplies the full initial pending signed release and approved result.
  No count, cursor or browsing expansion is part of this change.

## Owning implementation

1. `storage/vault_protected_hd.py`: selection/context dispatch, purpose-key lookup,
   capability-based public projection, strict DER versus existing 64-byte
   verification. Existing full-table/root/create/reconciliation owner stays.
2. `storage/vault_bitcoin.py`: public same-index return-script projection, called
   only after the owning full-table/enabled-account validation. Independent
   corpus cases at indexes0/1/19 verify purpose separation and malformed points.
3. `storage/vault_service.py`: NEW capture/current identity and release checks
   accept HD through its existing owner, bind derivation/cohort, supply same-index
   return scripts at intake/persisted proof/completion, and reuse atomic use/audit.
4. `vibe/api.py`: release attachment repeats persisted derivation, shared typed
   context/DER validation receives authenticated same-index returns, and explicit
   protected BTC completion exposes the existing frozen result-key contract.
5. Tests exercise actual API/request/storage/CLI owners, not a separate signing
   implementation. Synthetic root/ciphertext/public scalar fixtures establish
   daemon behavior only, never seed/AEAD/UV/custody or funded-spend acceptance.

## Validation and separate consumers

- `test_vault_protected_hd_bitcoin.py`: independent public corpus projection,
  completeness, point corruption, index separation and mutation-free behavior.
- `test_vault_protected_hd_bitcoin_integration.py`: both types at0/1/19 through
  API/request/storage/CLI, exact initial release, selected-point DER, frozen
  response-loss results, sibling returns and identity/audit rollback boundaries.
- `test_vault_protected_hd_bitcoin_boundaries.py`: actual historical/unknown pins,
  existing-record capability upgrade without reseal, omitted versus null context,
  malformed/high-S/wrong-digest DER, concurrent completion, proof-free summaries
  and persisted-release substitution refusal.
- Existing protected-HD, Standard HD, ordinary protected Bitcoin, shared context,
  API, CLI, policy and callback tests remain regression inputs. Independent exact
  head review, Codex and required CI are delivery gates, not inferred from units.

Claude owns the Web/Skill consumer: server-driven receiving choices, protected
create Done capability consumption, accurate path/type display, generic BTC
last-signed label, design frames, user-facing tests and capability-gated Skill
text. Skill claims cannot precede the backend. Shared composed Incus exercises,
physical UV/PRF, owner custody, installation and funds acceptance remain separately
owned; this source plan grants none of them.
