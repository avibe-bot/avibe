# Protected HD Bitcoin ECDSA consumer

## Scope and dependency boundary

Extend the existing protected-HD owner for explicit P2PKH/P2WPKH selections;
reuse the independently delivered ordinary Bitcoin parser, budgets, DER closure,
request identity/cohort and completion machinery. Do not add a seed/envelope,
full-table validator, root store, create/reconciliation owner or alternate custody
path. Ordinary delivery does not wait for this consumer.

This worktree starts from the default branch, without either dependency's
unmerged commits. The frozen protected-HD interface inspected for planning is
`65f48ae45e7b9c27a30d89aca0b56256211d31f3`; it is a contract input, not code copied
into this worktree. Integration and consumer merge require the actual protected-HD
and ordinary ECDSA owners on the default branch. Use a normal default merge when
they land, never stack or copy those owners.

## Invariants

- The receiving index remains 0..19 at `m/44'/0'/0'/0/i`,
  `m/84'/0'/0'/0/i`, `m/86'/0'/0'/0/i`. It is not hardened BIP44 account i.
- Reuse `vault_hd.BITCOIN_KEYS`: P2PKH selects `bitcoin_p2pkh`, P2WPKH
  selects `bitcoin_p2wpkh`, P2TR selects `bitcoin`. Never re-encode the BIP86
  scalar as another purpose's input key.
- The protected-HD owner still authenticates the entire typed 20-row envelope
  and immutable cache/profile, and selects an enabled account before public
  projection. The server verifies public consistency, not seed derivation.
- Same-account return scripts are exactly P2PKH(P44), P2WPKH(P84), and
  empty-tree BIP86(P86) at the selected index. A different receiving index is a
  recipient; more than one matching return output refuses. No P2SH shortcut.
- The pinned sandbox proves the actual seed-derived full-compressed three BTC
  siblings before review and at final use. Do not add unrelated EVM/Solana or
  all-account derivations, a sibling-attestation wire, or interpret DER under one
  point as proof of the siblings.
- New inputs use only `ecdsa-secp256k1-der`, a matching explicit Bitcoin
  derivation/address type, exact typed context, independently recomputed digest,
  and strict low-S DER/public-key/final-assembly closure. Keep existing EVM,
  Solana, Standard and protected Taproot bytes and error precedence.
- Freeze saved ID/classification/selected point/raw envelope/full pin cohort at
  intake. Reuse the original requester, no writer over custody waits, and atomically
  settle signature verification, pending CAS, account use/last-signer and audit.
- Complete/GET/await/callback use persisted frozen release/key/type after response
  loss or later record changes; no current-name reconstruction.
- New receive/sign capability comes only from a reviewed complete-pin registry
  entry. Unknown/older pins fail closed. Pin promotion/publication and runtime or
  funds acceptance remain separate from source work.

## Owning integration edits after dependencies land

1. `storage/vault_protected_hd.py`: extend selection/context dispatch for explicit
   types using existing `vault_hd` derivation/key maps. Pick the selected purpose
   point from the owner-selected row; call shared ECDSA validation with the three
   same-index return scripts; select DER or existing 64-byte verification by the
   actual derivation. Retain full-table/root validation and immutable AAD/raw
   fingerprint. Project only server-supported receive types.
2. `storage/vault_bitcoin.py`: public three-purpose script projection is local
   WIP here, using default-branch public point/script primitives and independent
   corpus tests. It is not an alternative authenticated-table owner. Wire a real
   consumer before publishing; do not leave a test-only production export.
3. `storage/vault_service.py`: extend only the new-kind captured identity branch
   to accept protected HD through its actual owner. Preserve initial immutable
   capture, exact selected-account/root/raw identity, cohort checks and the
   existing account-aware use/audit owner. Pass same-index return scripts to
   verification, never ordinary single-scalar return semantics.
4. `vibe/api.py`: reuse actual protected-HD material/release helpers and freeze
   selected purpose point/derivation in the existing four-field sandbox RPC.
   Reuse ordinary NEW context-omission resolution and exact saved-result paths;
   do not duplicate creation/reconciliation or add a second envelope/result.
5. Tests: exercise real owning calls after the dependency merge, including
   account0/1/19, purpose substitution/parity, typed-context mismatch, authenticated
   sibling/cache/envelope mismatch, fresh UV/cohort changes, original-requester
   use/last-signer, CAS/audit/caught/outer rollback and frozen response-loss reads.

## Current local validation

The default-only public projection uses the existing independent
`tests/fixtures/vault_signing/hd-bitcoin/corpus.json` expected script bytes for two
independent public cases at indexes0/1/19. It checks all-three completeness,
malformed-point refusal, different-index separation, mutation-free projection,
and BIP86 substitution producing a mismatch rather than claiming seed proof.
These focused tests do not establish the missing owner/DER/CAS integration.

No consumer PR, capability enablement, browser/PRF, runtime, deployment or funds
acceptance is claimed by this local WIP.
