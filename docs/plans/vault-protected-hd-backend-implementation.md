# Protected HD backend implementation

Status: implementation candidate, not runtime acceptance or feature enablement.
Contract: [Protected HD wallets, revision 0.6](vault-protected-hd-wallets.md).
That contract and its committed codec/fingerprint vectors remain unchanged.

## Scope and invariants

- Add a distinct browser-custodied protected-HD branch; do not relax standard HD.
  Python accepts only opaque sealed material and public consistency claims. It
  never derives a seed, unwraps a key, or dispatches protected HD to avault.
- Preserve the original raw wrap string. Validate bounded JSON, envelope sizes,
  the exact profile and twenty six-field public rows. Storage builds the required
  profile and twenty ten-field settings/provenance rows; caller public metadata
  is not an authority. Public consistency is not ciphertext/seed attestation.
- Compare all five expected account-0 addresses in storage before insert, then
  reread the inserted projection within the same rollback boundary. Include
  resource registration, audit, provision fulfilment and first setup. A caught
  error must not commit partial wallet/setup effects. Existing durable setup
  survives an unsuccessful wallet create.
- For a non-establishing protected-HD create, bind the complete current root:
  durable-first, otherwise only the current legacy root. Remove only the existing
  record-specific fields; retain all unknown root extensions. Do not search for
  an older matching root, replace/promote a root, attach a factor to a rootless
  history, replay enrollment, or require a policy registration for legitimate
  factorless legacy custody. Keep this check out of old generic create paths.
- Compare the authenticated profile/table with the cache on protected-HD reads,
  account management, signing intake and completion. Refuse a corrupt item, not
  the entire Vault listing/startup. Account labels are cosmetic; record name,
  ID, envelope, profile, public key, input and selection are security identity.
- Protected custody wins before avault discovery/version checks and before any
  standard headless/claim path. Mutable false flags cannot change this. Reuse
  existing management ACLs and sequential account enablement without resealing.
- Normalize omitted Bitcoin type to explicit `p2tr` only at protected request
  intake. Persist and repeat that exact selection throughout the release and
  completion. EVM/Solana forbid an address type; other protected Bitcoin types
  and the ordinary-key agent-operation context are unsupported. Standard
  omitted-type bytes and independently supported explicit types stay unchanged.
- Freeze the exact protected release and existing raw-envelope HD fingerprint.
  Preserve the HD Solana snapshot rather than replacing it with the ordinary
  Solana binding. Completion rereads and verifies the selected persisted key,
  exact input/context/release and signature before the pending-to-approved CAS.
  Bitcoin verification uses the BIP86 output key, not the internal point.
- Own completion CAS, usage, original-requester audit and selected-account
  last-signer in one nested rollback boundary, even when a storage caller catches
  an error. Do not hold a writer across a browser/custody wait or create a second
  CLI continuation path.
- Generic field ingestion/rotation and value/reveal/DEK owners accept only their
  intended explicit record kind or its untyped legacy form. Inspect selected or
  enumerated envelopes as well as the outer row; a typed HD/unknown extra field
  cannot hide behind a static default. Preserve existing corrupt-extra safe
  degradation: an unreadable optional section does not disable a valid default.

## Owning implementation

- `storage/vault_protected_hd.py`: typed raw envelope, root/table projection,
  protected selection/context and public-signature verification composition.
- `storage/vault_signatures.py`: existing public secp256k1 verification extracted
  from the API for reuse at the storage completion owner; no signing/custody.
- `storage/vault_service.py`: atomic create/readback, current-root binding,
  tier-aware account reads/management, strict request/completion and value-kind
  boundaries. Existing policy authorization continues to reject protected HD.
- `vibe/api.py`, `vibe/cli.py`: branch before avault, thread persisted derivation
  through request/release/completion, and retain existing continuation ownership.
- `skills/use-avibe-vault/SKILL.md`: distinguish standard/protected HD, supported
  contexts and Taproot-only protected commands. No runtime pin changes.

## Verification and evidence limits

Primary new coverage is `tests/test_vault_protected_hd.py`: exact projection and
five-address expectations, caught create/setup and postclaim rollback, full-root
reuse/refusal, three-chain nonzero-account signing, no avault, strict selection
and snapshot mutations, label-vs-record identity, replay/expiry, original
requester, two-writer create/completion, response loss, and generic typed-field
value/DEK refusal. Extend the existing policy owner test for a valid standard-HD
proof whose target is subsequently changed to protected.

Keep standard HD, ordinary EVM/Bitcoin/Solana, signing vectors, service/setup,
policy and CLI tests as separate parity coverage. Do not rewrite standard
omitted-type expectations to make the protected tests pass.

The test data is public and must never be funded. Backend tests compose opaque
synthetic material with independently committed signature oracles; they prove
server consistency/verification and transaction ownership, **not** PRF, AEAD,
seed/table derivation, browser decoding or physical-user ceremony. Those require
producer and real composed-runtime evidence.

## Remaining delivery boundaries

- Complete independent security review, exact-head Codex review and required CI.
- Obtain a separately authorized combined local-Incus allocation before any
  composed exercise. Do not use owner services or remote tenant infrastructure.
- Coordinate actual wire deltas with the sandbox owner; none are introduced by
  this implementation. F4 keeps the existing canonical envelope-object hash,
  distinct from the unchanged HD string-array fingerprint. F2 is the producer's
  immediate-parent RPC guard, not a backend flag or authority.
- Publication/pinning, final candidate acceptance and deployment are separate
  gates. This work does not introduce static generate-secret, install credentials,
  use funds, publish a sandbox, or restart the owner's runtime.
