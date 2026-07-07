# Protected Vault secret deletion authorization

Status: design plan only, no implementation.
Owner: Vaults workstream.
Date: 2026-07-07.

## Background verified from current code

Protected-tier secret values are still client-side custody. The browser unwraps
the VMK with a password or WebAuthn PRF passkey, seals protected envelopes, signs
protected keypair requests, and releases protected DEKs as blind boxes. The
daemon stores names, metadata, ciphertext, nonce, and opaque `wrap_meta`; it does
not currently verify passkeys or hold the VMK.

The current delete paths are uneven:

- `vibe vault rm` now checks metadata first and refuses protected secrets with
  `protected_delete_forbidden`.
- The Vaults page has a client-side destructive confirmation dialog, including
  stronger warnings for signing keys.
- `DELETE /api/vault/secrets/<name>` still calls `api.delete_vault_secret(name)`
  directly.
- `api.delete_vault_secret` calls `vault_service.delete_secret`, expires related
  requests/grants, deletes the row, and publishes updates.
- `vault_service.delete_secret` does not re-authorize protected rows.

That means same-origin and CSRF only protect the HTTP route. A same-machine agent
or script can obtain both and delete a protected row through HTTP even though the
CLI and UI button path look gated.

## Product goal

Deleting a protected secret must require a fresh user authorization that the
daemon can verify before it mutates storage. The proof must be operation-scoped,
must name the exact protected secret being deleted, and must not require the
daemon to store protected values, DEKs, or the VMK.

Non-goals for the first implementation:

- changing standard-tier deletion behavior;
- making the daemon decrypt protected secret values;
- adding a general account login system;
- solving same-origin XSS or a fully compromised browser profile;
- building all future destructive-operation policy in the first PR.

## Threat model

Blocked:

- Local agent via CLI: already blocked by `vibe vault rm`; the service guard
  should also make future CLI regressions fail closed.
- Local agent via HTTP: cannot delete a protected secret unless it can complete a
  server-issued factor challenge for that exact delete operation.
- Remote caller: still needs the existing setup auth, same-origin/CSRF checks,
  and a valid protected-operation proof. A stolen CSRF token alone is not enough.
- Replay caller: cannot reuse a previous proof because challenges are short-lived,
  single-use, and bound to `{operation, secret_id/name, updated_at}`.

Not fully blocked:

- The real user intentionally authorizing deletion.
- Malware that can drive the user's browser and satisfy the OS/passkey/password
  ceremony.
- Attackers that can arbitrarily edit the local SQLite database or patch the
  running daemon. This feature is an API-layer authorization boundary, not a
  host compromise boundary.

The daemon verifies one of these proofs:

- a WebAuthn `get()` assertion with user verification, checked against a
  daemon-stored credential public key; or
- a password-derived signing proof, checked against a daemon-stored public key
  derived at protected-vault setup or migration time.

## Compared approaches

### 1. Server-verifiable WebAuthn assertion

Add a server-side protected authorization factor registry. Passkey setup uses a
daemon-issued WebAuthn registration challenge and stores the credential public
key, credential id, RP id, algorithm, and signature counter. Protected delete
uses a new daemon challenge; the browser calls `navigator.credentials.get()`
with `userVerification: "required"` and submits the assertion to the daemon.
The daemon verifies `clientDataJSON.challenge`, origin/RP id, authenticator
data flags, signature, credential id, and counter before deleting.

Pros:

- strongest match for passkey user presence;
- no VMK or plaintext reaches Python;
- same-machine scripts cannot forge assertions without the authenticator and
  user verification;
- cleanly reuses WebAuthn's replay protection and per-credential public key
  model.

Cons:

- current PRF passkey setup did not persist the credential public key, so old
  passkey-only vaults cannot be silently upgraded;
- needs a WebAuthn verification implementation and registration challenge flow;
- passkey-only does not cover password-only vaults.

### 2. Delete-request plus approval flow

Model protected delete as a new `vault_requests` type, for example
`request_type="delete"`. A local or remote caller creates a pending delete
request. The user approves it in the browser, and approval completes the delete.

Pros:

- fits the existing human-in-the-loop Vaults request UX;
- good audit trail and good remote/async story;
- can later support "agent requested deletion, user approved from inbox".

Cons:

- by itself, it is not an authorization primitive. If approval only means "the
  UI posted approve", a same-machine agent can post that too.
- adding a durable pending destructive state is more machinery than direct UI
  delete needs.
- it still needs a daemon-verifiable factor proof at approval time.

This is useful as an outer workflow, not as the root security boundary.

### 3. Short-lived browser/session delete token

After the browser unlocks the protected vault, mint a short-lived daemon token
that can authorize one or more protected deletes.

Pros:

- small UI change if it reuses the existing unlock dialog;
- can reduce repeated passkey/password prompts.

Cons:

- if minted from the current UI-only unlock, it does not close the HTTP bypass;
- if stolen from browser storage, it becomes a bearer delete capability;
- "vault was unlocked earlier" is weaker than fresh presence for deletion.

This should not be the first fix. A short grace token could be layered later only
after the token is minted from a server-verified factor proof and is restricted
to one operation.

## Recommendation

Implement a reusable protected-operation authorization service, and use it first
for protected secret deletion. Keep the first product surface as direct delete
from the Vaults page, not a new request queue item. The route and service should
fail closed for protected rows unless a fresh operation-scoped proof verifies.

The recommended shape is a hybrid of approach 1 and the required password
parity:

1. Store server-verifiable authorization factors for the protected vault.
2. Issue short-lived delete challenges from the daemon.
3. Verify either a WebAuthn assertion or a password-derived signature.
4. Consume the challenge and delete the protected row in one transaction.
5. Keep `vault_requests` available for a later "agent asks user to delete"
   workflow, but do not make it the security primitive.

This is the smallest design that closes the agent-via-HTTP hole without moving
protected values into the daemon.

## Schema and storage

Add `vault_auth_factors`:

- `id`: stable factor id, e.g. `vaf_*`.
- `kind`: `webauthn` or `password_signature`.
- `label`: user-visible device/factor label, optional.
- `rp_id`: WebAuthn RP id for passkeys.
- `credential_id`: base64url/raw credential id for passkeys.
- `public_key`: COSE/JWK or normalized public-key bytes.
- `alg`: WebAuthn COSE alg or password-signature alg, e.g. `eddsa-ed25519`.
- `sign_count`: last accepted WebAuthn counter, nullable for password factors.
- `transports`: JSON array, optional.
- `password_kdf`: JSON params for password-derived private-key derivation,
  nullable for passkeys.
- `created_at`, `updated_at`, `last_used_at`, `disabled_at`.

Add `vault_operation_challenges`:

- `id`: challenge id, e.g. `vop_*`.
- `operation`: initially `delete_secret`.
- `secret_name`, `secret_id`, `secret_updated_at`: binds proof to the row that
  existed when the challenge was issued.
- `challenge_hash`: hash of the random challenge bytes; never store only a
  reusable bearer token.
- `expires_at`, `consumed_at`.
- `factor_id`: set when consumed.
- `created_at`.

Migration:

- Create both tables.
- Do not backfill fake factors.
- Existing standard secrets remain deletable.
- Existing protected secrets with no registered auth factor return
  `protected_authz_setup_required` for protected delete until a migration
  ceremony registers a real factor.

Custody boundary:

- The daemon stores public verification material and challenges only.
- It does not store protected plaintext, DEKs, VMKs, passkey PRF outputs, or raw
  passwords.
- `wrap_meta` remains the browser/crypto custody object, not the server
  authorization verifier.

## Passkey factor flow

Setup and migration should stop using a purely browser-random passkey creation
challenge for the authorization factor. The browser asks the daemon for
registration options:

`POST /api/vault/authz/factors/webauthn/options`

The daemon returns a registration challenge with:

- RP name/id derived from the current UI origin;
- `userVerification: "required"`;
- resident-key settings matching the current protected vault UX;
- allowed algorithms, initially ES256 and RS256 only if the verifier supports
  them;
- a challenge id with a short expiry.

The browser calls `navigator.credentials.create()` with the PRF extension still
enabled for VMK wrapping. It then submits the attestation response:

`POST /api/vault/authz/factors/webauthn`

The daemon verifies registration and stores the credential public key. The
browser continues storing the PRF `credential_id` and `prf_salt` inside
`wrap_meta` for VMK unlock. The public key is only for daemon authorization.

Delete proof:

1. Browser calls `POST /api/vault/secrets/<name>/delete-challenge`.
2. Daemon checks the secret exists and is protected, creates a challenge, and
   returns WebAuthn request options for registered passkey factors.
3. Browser calls `navigator.credentials.get()` with `userVerification:
   "required"`.
4. Browser submits the assertion with the delete request.
5. Daemon verifies and consumes the challenge before deleting.

This assertion is separate from the WebAuthn PRF assertion used to unwrap the
VMK. Delete does not need the VMK.

## Password-factor parity

Password-only protected vaults need a server-verifiable proof that does not send
the password on every delete. The recommended steady-state factor is a
password-derived signing key:

- During password setup, the daemon issues password-factor registration
  parameters: a random `authz_salt`, KDF params, and a challenge.
- The browser derives `authz_seed = KDF(password, authz_salt, params)` and then
  `delete_authz_seed = HKDF(authz_seed, "avibe:vault:authz:password:v1")`.
- The browser derives an Ed25519 keypair from `delete_authz_seed`, sends the
  public key plus registration proof, and discards the seed.
- On delete, the browser asks the user for the password again, re-derives the
  private key, signs the canonical challenge payload, and submits the signature.
- The daemon verifies the signature with the stored public key.

The UI already depends on password KDF code and `@noble/curves`; Python already
depends on `cryptography`, which can verify Ed25519 signatures. The exact
implementation may choose another deterministic signature algorithm if browser
and Python support is better, but it must keep the daemon-side verifier public.

Security note: a password-derived public key still allows offline password
guessing against local metadata, but the current password `wrap_meta` already
allows offline guessing via authenticated VMK unwrap. This does not add a new
class of risk; it avoids making the delete proof a bearer secret stored in the
database.

## API changes

Challenge issue:

`POST /api/vault/secrets/<name>/delete-challenge`

Returns one of:

- `{ok: true, challenge_id, expires_at, operation, secret_name, webauthn, password}`
- `{ok: false, code: "secret_not_found"}`
- `{ok: false, code: "not_protected"}` if a protected challenge was requested
  for a standard row.
- `{ok: false, code: "protected_authz_setup_required"}` if no usable factor is
  registered.

Delete:

Keep `DELETE /api/vault/secrets/<name>` as the canonical route. For protected
rows, require a JSON body:

```json
{
  "authz": {
    "challenge_id": "vop_...",
    "factor_id": "vaf_...",
    "kind": "webauthn",
    "assertion": {
      "id": "...",
      "rawId": "...",
      "type": "public-key",
      "response": {
        "clientDataJSON": "...",
        "authenticatorData": "...",
        "signature": "...",
        "userHandle": "..."
      }
    }
  }
}
```

For password factors, `authz` carries `{kind: "password_signature",
signature, public_key_alg}` instead of `assertion`.

Route behavior:

- Standard row: existing delete behavior remains.
- Protected row without `authz`: return `409 protected_auth_required`.
- Protected row with invalid/expired/replayed proof: return `409
  invalid_protected_authz`.
- Protected row with valid proof: consume challenge, delete row, expire related
  grants/requests, publish `vaults.updated`.

Service boundary:

- Change `vault_service.delete_secret` so protected rows require a verified
  protected-operation authorization context, not just API-route checks.
- The API layer can parse and verify WebAuthn/password payloads, but the final
  storage mutation should still fail closed if the service is called without a
  verified context.
- The CLI can continue refusing protected delete, but future code paths inherit
  the service-level guard.

## UI changes

Build on the existing delete dialog instead of adding a separate approval queue.

For standard secrets:

- No UX change.

For protected secrets:

1. User opens the existing delete dialog.
2. Dialog shows the existing destructive copy and, for protected rows, a factor
   authorization step.
3. On confirm, the UI requests a delete challenge.
4. If passkey factors are available and usable on the current RP id, prompt
   WebAuthn user verification.
5. If password factors are available, offer password confirmation.
6. Submit the proof with the delete request.
7. Show existing success/error toasts and refresh.

The dialog should not unlock or expose the VMK just to delete a row. Passkey
delete authorization is a normal WebAuthn assertion; password delete
authorization is a password-derived signature. The current protected unlock
panel remains for create, sign, and protected-access approval flows.

If the daemon returns `protected_authz_setup_required`, the UI should open a
short "Enable protected delete authorization" migration flow and make clear that
deletion is blocked until at least one server-verifiable factor exists.

## Backward compatibility

There is no cryptographic way to reconstruct a WebAuthn credential public key
from the current `wrap_meta`; existing passkey copies only store PRF unlock
metadata. There is also no daemon-checkable password verifier for existing
password-only vaults. Any plan that silently accepts "browser says it unlocked"
would preserve the current UI-only gap.

Therefore legacy behavior should fail closed:

- Protected delete is refused until a server-verifiable auth factor is
  registered.
- Existing users can continue using protected secrets for create/access/sign
  flows as before.
- The UI guides them through registration the first time they attempt protected
  deletion or when opening Vault settings after upgrade.

Migration options:

1. Preferred for passkey-capable users: register a new WebAuthn authorization
   factor. The registration itself requires real user verification, and future
   deletes use server-verified assertions.
2. Preferred for password-only steady state: register a password-derived public
   key the next time the user confirms the protected password in the browser.
3. For existing password-only vaults, the product must choose between:
   - fail closed until the user adds a passkey or recreates the protected vault
     under the new setup flow; or
   - an explicit one-time legacy migration where the daemon verifies the
     password against existing `wrap_meta`, wipes transient VMK/password
     material, and stores only the password-derived public key.

The second migration branch is the only way to cryptographically verify an
existing password-only vault without a passkey, but it is a custody trade-off and
should be accepted explicitly before implementation.

## Scope boundary

The first implementation should enforce this only for protected secret deletion.
The underlying authorization service should be named generically enough to cover
future protected destructive operations:

- removing the last protected auth factor;
- rotating/replacing the protected vault root metadata;
- deleting protected keypairs;
- possibly protected secret metadata changes that affect delivery policy.

Do not gate standard metadata edits, standard deletion, request denial, or grant
revocation with this new proof in the first phase.

## Phased todo

### Phase 0: design acceptance

- Decide the legacy password-only migration policy: fail-closed only, or
  explicit one-time daemon password verification.
- Decide WebAuthn verification dependency: implement minimal verification with
  `cryptography`/CBOR parsing, or add a focused WebAuthn/FIDO2 dependency.
- Decide the exact password-derived signing algorithm after a small browser and
  Python compatibility spike.

### Phase 1: daemon primitives

- Add auth factor and operation challenge tables plus migrations.
- Add factor registration APIs for WebAuthn and password-derived factors.
- Add challenge issue and proof verification helpers.
- Add audit events for challenge issued, factor registered, delete authorized,
  and delete denied.
- Add focused tests for challenge expiry, replay, secret mismatch, stale row
  version, invalid signatures, and no-factor fail-closed behavior.

### Phase 2: protected delete enforcement

- Change `api.delete_vault_secret` to accept an optional `authz` payload.
- Change the HTTP route to pass JSON body through for DELETE.
- Change `vault_service.delete_secret` to refuse protected rows without a
  verified authorization context.
- Keep CLI protected deletion refused.
- Add API/service tests proving direct protected delete without proof fails even
  when origin/CSRF would otherwise pass.

### Phase 3: UI integration

- Extend the Vaults delete dialog with protected factor challenge handling.
- Add WebAuthn assertion and password-derived signature helpers.
- Add i18n strings for protected auth required, setup required, expired
  challenge, and invalid proof.
- Add focused UI tests for passkey options, password fallback, and setup-required
  error handling.

### Phase 4: migration UX

- Add a Vault settings or inline migration panel for protected auth factors.
- Register auth factors during new protected vault setup.
- Guide legacy users through passkey registration or the accepted password-only
  migration policy.
- Add telemetry/audit-only counters for how many protected rows are blocked by
  missing auth factors, without exposing secret values.

### Phase 5: follow-up scope

- Revisit whether protected auth should also guard destructive protected factor
  management and protected metadata policy changes.
- Consider an optional `vault_requests` delete workflow for remote/agent-initiated
  deletion requests, using the same proof verifier at approval time.
