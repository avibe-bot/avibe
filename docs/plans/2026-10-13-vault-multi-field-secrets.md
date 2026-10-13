# Vault multi-field secrets

Status: P1 contract. The owner approved the direction and the four decisions in
section 2 on 2026-10-13.

Related: `vaults-grant-delivery-refactor.md` (current grant and delivery model),
`avault-custody-core.md`, `vault-sandbox-protocol-v2.md`, and
`../avault/docs/DESIGN.md` (authoritative envelope and AAD design).

## 1. Background

A user asked for one Vault entry to hold several secret values, like a
1Password item: a login has a username, a password, and a 2FA secret. Today a
Vault secret holds exactly one value, so a login becomes three unrelated
secrets.

## 2. Goal and decisions

One secret can hold several named fields. Agents reference a field as
`NAME.field`. Values still never enter the model context, and avault's
cryptography and wire formats do not change.

Approved decisions:

1. A secret has one **default field** (its existing value) plus optional
   **extra fields**.
2. Authorization stays per secret. Approving a protected secret releases the
   DEKs of all its fields, and grants still list secret names.
3. `--tag` and `--skill` deliver every field of each selected secret. `--env`
   still maps exactly one value to one variable.
4. P1 can replace the value of any field, including the default field.

Built-in 2FA code generation is P2 (section 13).

## 3. Names and records (shared contract)

| Term | Grammar |
| --- | --- |
| Secret name (unchanged) | `^[A-Za-z_][A-Za-z0-9_]*$` |
| Field name | `^[a-z][a-z0-9_]{0,63}$` |
| Record name | `^[A-Za-z_][A-Za-z0-9_]*(\.[a-z][a-z0-9_]{0,63})?$` |

- A **record** is one encrypted value. The default field's record name is the
  bare secret name (`GITHUB_LOGIN`), whatever the default field is called. An
  extra field's record name is `<secret>.<field>` (`GITHUB_LOGIN.username`).
  `<secret>.<default field name>` is never a record name.
- A **ref** is what users and agents write: `NAME` or `NAME.field`. Both `NAME`
  and `NAME.<default field name>` resolve to the default record.
- Existing secrets have a default field named `value` and no extra fields.
- Field names are unique within a secret, including the default field's name.
  A secret has at most 32 fields, counting the default field.
- Extra field names are immutable because their ciphertext binds the record
  name. To rename one, remove it and add it again. The default field's name is
  only a label and can be renamed. Which field is the default is fixed when the
  secret is created.
- Only `static` secrets can have extra fields. Keypair secrets keep one field,
  and refs such as `KEY.field` are rejected.

Every envelope binds its record name:

- Standard tier: avault seals and opens with AAD
  `record || "machine-aesgcm-v1" || 0x01`.
- Protected tier: the protected-record v2 AAD JSON carries
  `"name": "<record>"` and `"record": {"kind": "static"}`.
- Blind boxes: the `name` AAD field is the record name for `seal` and
  `agent-deliver`, and the agent-deliver `operation_hash` covers the record
  name.

avault already treats this name as an opaque string. A probe with avault 0.1.6
and a temporary file store delivered `GH.username` and `GH` to one child process,
and rejected a `GH.username` envelope presented as `GH.password` or `GH`. The
vault-sandbox crypto validator still accepts only secret names, so it must
accept record names before protected secrets can have extra fields.

## 4. Storage

- New nullable TEXT column `vault_secrets.fields`. It holds a JSON array of the
  extra fields in display order:

  ```json
  [{"name": "username", "ciphertext": "...", "nonce": "...", "wrap_meta": "...",
    "created_at": "...", "updated_at": "..."}]
  ```

  NULL or `[]` means no extra fields.
- The default field keeps the existing `ciphertext`, `nonce`, and `wrap_meta`
  columns unchanged.
- The default field's name is `public_meta.default_field`. When it is absent
  or invalid, the name is `value`.
- All fields share the secret's `kind`, `protection`, tags, policy, resource
  access policy, and audit history.
- Every envelope change bumps `vault_secrets.updated_at`, so stale approval
  material is detected through the existing member versions. Field writes are
  atomic per secret: a concurrent change fails as a conflict, never as a lost
  update.
- The migration adds the column and its downgrade drops it. Released versions
  ignore the column and keep reading the default value. The `vibe data query`
  denylist already covers `vault_secrets`.

## 5. Value-free metadata

The secret metadata payload (`_meta_payload`, REST, and `vibe vault list/find
--json`) adds two keys:

- `default_field`: the default field's name.
- `fields`: every field name, default field first, then extra fields in stored
  order. An existing secret shows `"default_field": "value", "fields": ["value"]`.

## 6. Delivery

| Selector | Records | Environment variable |
| --- | --- | --- |
| `--env NAME` | default record | `NAME` |
| `--env NAME.field` | that field's record | `NAME_<FIELD>` |
| `--env LOCAL=NAME[.field]` | the referenced record | `LOCAL` |
| `--tag T`, `--skill S` | every record of each selected static secret | default record as `NAME`; each extra field as `NAME_<FIELD>` |

- `<FIELD>` is the field name in upper case: `GITHUB_LOGIN.username` becomes
  `GITHUB_LOGIN_USERNAME`. The rule also applies when the ref names the default
  field: `GITHUB_LOGIN.password` becomes `GITHUB_LOGIN_PASSWORD` and delivers
  the record `GITHUB_LOGIN`.
- Conflicts fail before delivery, as today but per record: one record selected
  under two variable names, or one variable name mapped to two records.
- An unknown field fails with a `field_not_found` error that lists the secret's
  fields. Keypair rejection (`keypair_not_value_deliverable`) is unchanged.
- `vibe vault fetch --auth NAME[.field]` attaches that record. The allowed
  hosts and auth scheme come from the secret's policy.
- `vibe vault inject` accepts the same selectors as `run`, including field refs.
  Avibe always sends an explicit `key`, because avault's fallback key would be
  the record name, which is not a valid key.
- `vibe vault export` stays deprecated and returns `export_deprecated`, with no
  delivery. Its parser and help are unchanged.
- Commands that only authorize, such as `vibe vault access`, accept
  `NAME.field` and authorize the secret.
- Every avault entry carries `name` = record name, that record's `envelope`,
  and `tier` = the secret's protection.
- Use counts stay per secret. The delivery audit lists the delivered fields.

## 7. Authorization

- Grants, access requests, `always_ask`, and one-shot handling stay keyed by
  secret names. `member_snapshot` keeps listing secret names.
- A protected DEK release covers every record of each protected member:
  - Approval card `grant_options[].unlock_material` carries one material per
    record, named by record name.
  - The agent-binding batch issues one signed release context per record, with
    the operation hash over the record name. The signed `display.secrets` lists
    every record (`{"name": "<record>", "kind": "static"}`) because the sandbox
    requires the display block to cover each release item.
  - Grant creation requires the submitted DEK names to equal the record names
    of the protected members.
- Within a live grant, delivery may use any record of a covered secret.
- Any envelope change expires the secret's pending requests and active grants,
  and releases resident avault scopes, as rotate and delete do today.
- The approval card shows each member's field names.

## 8. API

- `POST /api/vault/secrets` accepts an optional `default_field` and optional
  `fields`: `[{"name", "record", "blind_box"}]` for standard secrets or
  `[{"name", "record", "sealed"}]` for protected secrets. The top-level `blind_box` or
  `sealed` is still the default field. For standard secrets Python seals each
  field with `avault seal --name <record> --blind-box`. Keypairs reject `fields`.
- `PUT /api/vault/secrets/{name}/fields/{field}` takes `{"record", "blind_box"}`
  or `{"record", "sealed"}`. For the default field it replaces the default envelope.
  Otherwise it adds the field (appended) or replaces its value. It returns the
  masked metadata.
- `DELETE /api/vault/secrets/{name}/fields/{field}` removes an extra field. The
  default field cannot be removed (409).
- `PATCH /api/vault/secrets/{name}` also accepts `default_field` to rename the
  default field. The name must be valid and unused; this is pure metadata and
  leaves envelope versions, pending requests, and active grants unchanged.
- `POST /api/vault/secrets/{name}/reveal-context` accepts an optional `field`
  and returns the context and envelope of that record.
- Every extra create field and field PUT declares the `record` name used to
  seal it; the server rejects a mismatch with its current field resolution as
  409 `record_mismatch` without storing anything, and the UI re-reads metadata
  and re-seals once (the top-level create record is implicitly `NAME`).
- New error codes: `invalid_field_name`, `field_exists`, `field_not_found`,
  `too_many_fields`, `fields_not_supported`, `record_mismatch`.
- Plaintext rejection, resource-access checks, SSE updates, and audit apply to
  every new path. Field changes audit as `updated` with
  `{"field": "<name>", "op": "add" | "replace" | "remove"}`.
- The browser derives the record name (`NAME` for the default field,
  `NAME.field` otherwise) and seals with it: standard secrets use
  `standardCreateBlindBoxContext(record)`, protected secrets use the sandbox
  `seal({name: record, kind: "static"})`.

## 9. Provision requests

The request spec gains `fields` (field names in display order) and
`default_field` (one of `fields`; defaults to the first). They never carry
values.

```bash
vibe vault request GITHUB_LOGIN --reason "Log in to GitHub" \
  --spec-json '{"protection":"protected","fields":["username","password","totp"],"default_field":"password"}'
```

The browser form starts with those rows. The user may remove rows, and every
submitted field needs a value.

## 10. UI

- Create: a static secret looks the same as today until the user adds a field.
  Then the value becomes a list of field rows (name and value), with exactly one
  default. A Login template fills `username`, `password` (default), and `totp`.
  File upload stays available only for a single-value standard secret.
- A protected create seals every field in the same sandbox session and sends
  one create request. Vault establishment fields are sent once.
- Edit: replace any field's value, add or remove extra fields, and rename the
  default field. Name, kind, and protection stay read-only.
- List and detail show field names when a secret has more than one field.
- Protected reveal works per field.
- The approval card groups records by secret and shows field names.
- Strings go through `ui/src/i18n/{en,zh}.json`; reuse the existing primitives.

## 11. Cross-repository work

| Repository | Change |
| --- | --- |
| avibe | Storage, service, API, CLI, UI, `use-avibe-vault` skill, tests |
| vault-sandbox | Accept record names in crypto-name validation, with tests |
| avault | DESIGN documents the record-name convention; vectors and tests pin field records; no code change |
| avibe-docs | Vault concept page in English and Chinese |

The sandbox change only widens accepted names, so it can deploy first. Standard
secrets with fields work without it; protected secrets with fields need it.

## 12. Validation

- Existing secrets keep their delivery behavior; their metadata adds only
  `default_field` and `fields`.
- New paths accept no plaintext.
- Fields are sealed and delivered under their record names; avault tests show
  a field envelope does not open under another record name.
- Selectors cover refs, variable naming, tag and skill expansion with extra
  fields, conflicts, unknown fields, and keypair rejection.
- Protected approval releases every record, delivery of any field works under
  the grant, and a field change expires the grant.
- The migration upgrades and downgrades with existing rows.
- The skill's CLI examples parse.

## 13. Out of scope for P1

- **2FA codes (P2).** A `totp` field type. avault computes the current code at
  delivery, so the child gets the code and never the seed, and sandbox reveal
  shows a live code. This needs avault (HMAC-SHA1/256/512, base32, `otpauth://`
  parsing) with a DESIGN update, the sandbox, and Python type metadata. Until
  then a 2FA secret can be stored as an ordinary field.
- HTTP Basic auth in `fetch` from a username and password field.
- 1Password import that maps item fields to Vault fields.

## 14. Known by design

- Approving a protected secret unlocks all its fields for the grant's lifetime.
- Extra field names and the choice of default field cannot change after
  creation.
- Standard values stay hidden in the UI.
- The CLI cannot write fields; values enter only through the browser.
- `export` remains deprecated and never delivers values. Agent-initiated field
  deletion is out of scope; browser/REST removal of extra fields is supported.
- There are no plaintext or non-secret fields; non-secret notes belong in the
  description.
