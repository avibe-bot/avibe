# Editable signing confirmation policy

## Scope and trust boundary

Ordinary standard/protected keypairs and standard HD accounts share the ask-before-signing policy. Raising confirmation does not require a Passkey. Lowering it will require a fresh assertion by an existing Vault Passkey; first setup uses the existing Vault setup UX and trust-on-first-use, without another owner-enrollment anchor.

This guards ordinary or accidental lowering through normal API/CLI calls. Standard-tier confirmation is not isolation from a hostile process with the same OS user's shell/database permissions: existing local Owner HTTP approval and direct policy-file access are outside this soft guard. The UI must state that standard-tier confirmation protects against mistakes and funds belong in the protected tier. Protected custody/signing continues to require its existing browser cryptography; changing this setting never bypasses that boundary.

## Stage one: enable only

Until the dedicated policy assertion flow is available:

- Existing `PATCH /api/vault/secrets/{name}` accepts top-level `always_ask: boolean` only for ordinary keypairs (`signer_kind` null/local), standard or protected. It persists in the existing `policy.always_ask` JSON; no migration.
- HD uses the existing `PATCH /api/vault/secrets/{name}/accounts/{account}` field. Policy applies to the shared account index across all chains, not to one chain.
- False→true is allowed. True→false returns HTTP 409 / `vault_policy_downgrade_unavailable`. Saving the same value is allowed, so metadata forms can save an unchanged checkbox.
- Description/tags/account names retain their existing behavior. A mixed payload requesting a forbidden downgrade fails atomically, including its display changes and audit.
- Full `policy` updates still cannot modify internal `always_ask`; omission/null preserves it. Static secrets and HD row-wide policy do not gain the ordinary-keypair editor.
- Storage owns the shared monotonic transition check, including callers that bypass HTTP. Existing resource-management ACLs, metadata CAS and HD short writer reservation remain in effect. No custody call, unlock, factor registration or sandbox RPC is involved.
- Confirmation affects later target captures, not an already-started custody operation. Pending requests and encrypted envelopes are not rewritten by raising it. Protected signing remains browser-only even if its policy was previously false.

Web owns the corresponding disabled-on-state and explanation. Backend errors are localized in the existing English/Chinese catalogs. No new CLI command is added; existing clients must receive the same storage/API refusal if they attempt to lower the setting.

## Stage two: fresh Passkey authorization

A subsequent change will add the named policy-downgrade purpose and share one storage-owned verifier between ordinary keys and HD accounts. Its assertion must be bound to the record identity, selected account if any, old/new policy, revision, expiry and single-use operation; check/consume/change must be atomic and prevent stale/ABA or cross-target authorization. Existing factors must be used rather than silently replaced. A first setup may establish the TOFU factor and then continue the requested operation.

The assertion's sandbox integration gets a **separate later sandbox version and fresh pin**. It is not part of the RC13 display-only candidate. It may ship in 3.2.6 if complete in time, otherwise 3.2.7; stage-one enable-only editing has no sandbox dependency. The exact producer/consumer contract must be settled before that later sandbox writer starts. No policy RPC, registration changes, version/pin or downgrade authorization is introduced by stage one.

## Validation

The storage boundary tests ordinary standard/protected and null/local signer kinds, accepted raising/idempotence, rejected lowering, strict booleans, unchanged encrypted identity, preserved policy fields and metadata, ACL denial and a concurrent policy writer. API/HTTP tests pin the common 409 error and verify a raised standard key requests approval before custody. HD tests cover the same refusal with atomic mixed edits, no row-wide bypass, unaffected sibling accounts and all-chain approval. These tests complement rather than replace the whole-wallet browser, recovery, funded and final adversarial acceptance.
