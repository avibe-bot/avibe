# Vaults grant and unified delivery refactor

Status: final implementation plan, not a compatibility proposal.
Owner: Vaults workstream.
Date: 2026-07-03.

This document captures the final Vaults model agreed after the request-spec,
access/wait/request, run/fetch, tag/skill, and grant-design discussions. The
feature is not publicly launched yet, so implementation should move directly to
this final shape. Do not preserve legacy group grants, scope-type compatibility,
or migration shims unless a production release later requires them.

Related docs:

- `docs/plans/vaults.md` and `docs/plans/vaults-p0-implementation.md` describe
  the older P0/P1 model. Their `group` and `scope_type` grant sections are
  superseded by this plan.
- `docs/plans/avault-custody-core.md` describes the Avibe-side custody boundary.
- `../avault/docs/DESIGN.md` is the authoritative avault-side protocol design.

## 1. Core decisions

1. Remove `group` from the product model. Vaults keep one global secret
   namespace plus tags.
2. Keep tags as the only grouping selector. A secret can have multiple normal
   tags and multiple skill tags.
3. Represent skill association as reserved tags, preferably `skill:<name>`.
   `--skill <name>` may remain CLI sugar, but internally it is just
   `--tag skill:<name>`.
4. Grants are keyed by first-class `grant_id`, not by
   `{scope_type, scope_ref}`. A grant is a fixed approved set of protected
   secrets.
5. Tag changes never change existing grants. Adding/removing tags only changes
   future selector expansion.
6. `always_ask` remains a backend policy capability, but product UI should not
   expose configuration for it yet.
7. Keypairs are not value-deliverable. If `run`, `fetch`, `inject`, or another
   value-delivery path resolves a keypair, fail with a clear "use sign" hint.
8. Standard and protected secrets may be mixed in one `run`. If any protected
   secret is involved, avault resident agent performs one unified delivery and
   spawns exactly one child process.
9. Agent/API/CLI surfaces remain value-free. Only browser/UI approval and secure
   input flows may handle browser-side unlock material, and only as opaque
   blind boxes or browser-local ciphertext.
10. Avault stays a custody runtime. It does not know tags, skills, requests, UI
    cards, or Avibe metadata semantics.

## 2. Layer boundaries

| Layer | Owns | Must not own |
| --- | --- | --- |
| Agent / CLI | Intent: env names, tags, skill sugar, command, fetch request | Secret values, DEKs, plaintext create |
| Avibe Python | Metadata, selector expansion, request rows, grant DB, audit, UI/API orchestration, delivery frames | Key material, decryption, reusable secret state |
| Browser / UI | User approval, protected unlock, secure input, browser-side encryption and DEK release blind boxes | Agent-visible plaintext or grant creation without user action |
| avault resident agent | Grant DEK cache, envelope open, final run/fetch/inject/sign side effects | Tags, skills, request rows, UI policy, plaintext return verbs |
| Child process / HTTP egress | The actual consumer of delivered values | Vault metadata or grant semantics |

The invariant is unchanged: Python and the model handle names and opaque blobs;
avault is the only component that holds keys, opens envelopes, or materializes
plaintext for delivery.

## 3. Secret metadata

`vault_secrets` should keep:

- `name`: globally unique env-style secret name.
- `kind`: `static` or `keypair`.
- `protection`: `standard` or `protected`.
- `tags`: JSON array of strings.
- `policy`: delivery policy such as allowed hosts, allowed modes, and hidden
  `always_ask`.
- `public_meta`: description and non-secret display metadata.
- envelope columns: ciphertext, nonce, wrap metadata, etc.

Remove product use of `group_name` / `vault_groups`. Because this feature is not
launched, the implementation may remove old group UI, commands, options, service
paths, tests, and stale docs instead of migrating them.

Skill links should be represented through tags:

- Normal tag: `prod`, `github`, `deploy`, `billing`.
- Skill tag: `skill:github-release`, `skill:deploy-aws`.

The UI may show skill tags in a dedicated skill section, but the storage and
selector semantics are still tag-based.

## 4. Selectors

Selectors are Avibe-only inputs. Avault never sees them.

Supported run selectors:

- `--env NAME`: inject secret `NAME` as env var `NAME`.
- `--env LOCAL=NAME`: inject secret `NAME` as env var `LOCAL`.
- `--tag TAG`: include all static value-deliverable secrets tagged `TAG`.
- `--skill SKILL`: sugar for `--tag skill:SKILL`.

First version supports multiple tags as a union. Duplicates collapse by secret
name. If the same secret is selected more than once with conflicting env names,
Avibe must fail before delivery with a clear conflict error.

Selector output is a concrete delivery set:

```json
{
  "source_selector": {
    "env": ["OPENAI_API_KEY", "DB_URL=PROD_DB_URL"],
    "tags": ["deploy", "skill:github-release"]
  },
  "secrets": [
    {
      "name": "OPENAI_API_KEY",
      "env": "OPENAI_API_KEY",
      "kind": "static",
      "protection": "standard"
    },
    {
      "name": "PROD_DB_URL",
      "env": "DB_URL",
      "kind": "static",
      "protection": "protected"
    }
  ]
}
```

If selector expansion includes a keypair, fail with `keypair_not_value_deliverable`
and point the caller to `vibe vault sign`.

## 5. Requests

Requests capture user-facing intent. They are not grants.

Provision request:

- Used when an agent asks the user to create/store a missing secret.
- `vibe vault request NAME --spec-json ...` accepts a structured spec for
  optional fields.
- The request spec may include `protection`, `tags`, `links.skills` or direct
  skill tags, `policy.allowed_hosts`, `description`, and other non-secret fields.
- The request CLI does not expose `--skill`; skill association belongs in the
  JSON spec as tags/skill links.
- Fulfillment remains browser/UI only for secret values. Agent/API/CLI do not
  pass plaintext values.

Access request:

- Used when an agent wants to run/fetch/inject with protected secrets and no
  ready grant covers the protected set.
- If the agent requested explicit env names, the approval card should describe
  the concrete protected env unlock.
- If the agent requested tag or skill selectors, the approval card should
  describe the tag/skill request and show the protected secret names that will be
  covered.
- Standard secrets do not need to appear in the approval card unless hidden
  `always_ask` policy later forces an approval path.

Request payloads must remain value-free outside the browser/UI audience. UI
audience may hydrate protected unlock material needed to complete approval, but
agent/API/CLI audience must not.

## 6. Grant model

Final definition:

> A grant is a user-approved, time-limited, session-bound authorization for
> avault to use a fixed set of protected secrets.

Recommended `vault_grants` shape:

| Field | Meaning |
| --- | --- |
| `id` | The first-class `grant_id`; this is the avault runtime scope. |
| `member_snapshot` | JSON array of protected secret names approved at creation time. |
| `source_selector` | JSON explaining whether this came from env list, tag, skill, or request. |
| `request_id` | Access request that created the grant. |
| `session_id` | Session binding. Default required for agent-created grants. |
| `purpose` | Initial values: `run`, `fetch`, `inject`; later `sign` may use its own grant type. |
| `status` | `active`, `expired`, `revoked`, plus transient/reserved only if needed for one-shot. |
| `one_shot` | Internal capability for `always_ask`; not exposed in product UI. |
| `created_at` | Creation timestamp. |
| `expires_at` | Expiry timestamp. TTL does not slide. |
| `agent_ready_at` | Set when resident avault accepted the DEK blind boxes. |

The old `scope_type` / `scope_ref` fields are superseded. If a short bridge is
needed while avault is updated, use `scope_type = "grant"` and
`scope_ref = grant_id` only inside the implementation branch; do not preserve it
as a user or product concept.

Grant membership rules:

- The grant member set contains only protected value-deliverable secrets.
- Standard secrets are delivered in the same avault frame but are not grant
  members.
- Keypairs cannot be members of value-delivery grants.
- Tag edits do not affect active grants.
- Secret deletion, rotation, or protection/kind changes should revoke or expire
  grants that include the changed secret.

Default TTLs:

- Explicit env-list grant: 300 seconds.
- Tag or skill-tag grant: 900 seconds.
- Internal `always_ask`: one-shot, consumed after possible delivery.

These TTLs are product defaults, not UI controls for the first version.

## 7. avault resident-agent grant scope

Avault should treat `grant_id` as the runtime scope for protected DEKs.

Grant frame:

```json
{
  "type": "grant",
  "grant_id": "gr_123",
  "purpose": "deliver",
  "ttl_secs": 900,
  "deks": [
    {
      "name": "PROD_DB_URL",
      "dek_blindbox": {
        "scheme": "hpke-x25519-aes256gcm",
        "enc": "...",
        "ct": "..."
      },
      "approval": {
        "nonce": "...",
        "expires_at_unix": 1780000000
      }
    }
  ]
}
```

Avault behavior:

- Cache each protected DEK under `{grant_id, name}`.
- Effective expiry is the earlier of `ttl_secs` and the approval expiry.
- TTL never slides.
- `release`/`revoke` accepts `grant_id` and zeroizes all cached DEKs for that
  grant.
- Reject `dek_blindbox` on delivery frames. Protected DEKs must enter through a
  grant frame only.

Release frame:

```json
{
  "type": "release",
  "grant_id": "gr_123"
}
```

## 8. Unified `run` delivery protocol

Run delivery must spawn exactly one child process. Therefore mixed
standard/protected runs must not call CLI for standard and resident socket for
protected separately.

Avibe delivery planner:

1. Expand env/tag/skill selectors into a concrete set.
2. Reject keypairs and env-name conflicts.
3. Split into `standard` and `protected`.
4. If the protected set is empty, use one-shot `avault deliver run` as today.
5. If the protected set is non-empty:
   - find a ready grant covering the protected set for this session/purpose; or
   - create an access request; after user approval, relay browser DEK blind boxes
     to resident avault under `grant_id`;
   - call resident avault once with both standard and protected entries.

Resident delivery frame:

```json
{
  "type": "deliver.run",
  "grant_id": "gr_123",
  "command": ["node", "deploy.js"],
  "secrets": [
    {
      "name": "OPENAI_API_KEY",
      "env": "OPENAI_API_KEY",
      "tier": "standard",
      "envelope": {
        "ciphertext": "...",
        "nonce": "...",
        "wrap_meta": "..."
      }
    },
    {
      "name": "PROD_DB_URL",
      "env": "DATABASE_URL",
      "tier": "protected",
      "envelope": {
        "ciphertext": "...",
        "nonce": "...",
        "wrap_meta": "..."
      }
    }
  ],
  "context": {
    "session_id": "ses_123",
    "purpose": "run"
  }
}
```

Avault behavior:

- For standard entries, open using the standard machine-rooted store.
- For protected entries, require `{grant_id, name}` cached DEK coverage.
- Reject any protected entry not covered by the grant.
- Reject any value-delivery keypair.
- Inject all resolved values into one child environment.
- Return only child exit code and process output behavior already allowed by
  delivery mode; never return secret values as structured output.

## 9. Unified `fetch` delivery protocol

Fetch remains single-auth-secret in the first version unless there is a concrete
product need for multiple auth secrets in one HTTP request.

If auth secret is standard:

- use one-shot `avault deliver fetch`.

If auth secret is protected:

- require a ready grant covering that secret;
- otherwise create access request and relay DEK blind box on approval;
- call resident avault with `grant_id`.

Resident fetch frame:

```json
{
  "type": "deliver.fetch",
  "grant_id": "gr_123",
  "auth": {
    "name": "GITHUB_PAT",
    "tier": "protected",
    "envelope": {
      "ciphertext": "...",
      "nonce": "...",
      "wrap_meta": "..."
    }
  },
  "request": {
    "url": "https://api.github.com/repos/o/r/issues",
    "method": "POST",
    "headers": [],
    "body": "..."
  },
  "context": {
    "session_id": "ses_123",
    "purpose": "fetch"
  }
}
```

Avibe still performs product-level preflight:

- URL host must match the secret's allowed hosts.
- Non-loopback URLs must be HTTPS.
- Disallow Host header override.
- Disallow unsupported methods such as `TRACE`, `TRACK`, `CONNECT`.
- Preflight output file writability before avault handoff.

Avault performs custody-level enforcement:

- open the auth envelope;
- attach credential according to the request policy supplied by Avibe;
- perform the request;
- return response status/body, not the secret.

## 10. CLI surface

Target user-facing commands:

```text
vibe vault run \
  --env NAME \
  --env LOCAL=NAME \
  --tag TAG \
  --skill SKILL \
  -- COMMAND...

vibe vault fetch \
  --auth NAME \
  --url URL \
  [--method METHOD] \
  [--header 'Name: value'] \
  [--data DATA | --data-file FILE] \
  [--output FILE]

vibe vault request NAME \
  [--reason TEXT] \
  [--spec FILE|- | --spec-json JSON] \
  [--wait | --no-wait]
```

Notes:

- `run --skill` is CLI sugar and must compile to `--tag skill:<name>`.
- Do not reintroduce `vibe vault set`.
- Do not add many request flags. Keep request spec structured JSON.
- Remove group flags, group filters, group approval options, and group docs.

## 11. UI expectations

Vaults UI should support:

- tag editing for each secret;
- skill association as skill tags;
- filtering by normal tag and skill tag;
- secure input/provision request fulfillment using request specs;
- access approval cards for protected secret sets;
- approving tag/skill requests as a fixed protected secret set;
- no product-facing `always_ask` configuration in the first version.

Approval cards should show:

- caller session/agent;
- selector source: explicit env list, tag, or skill;
- protected secret names to be granted;
- command or fetch target;
- TTL copy based on the default, not a broad user-configurable policy surface;
- deny/approve controls.

Standard secrets should not clutter protected approval cards unless an internal
policy requires per-use approval.

## 12. Implementation plan

Recommended parallel tracks:

### Track A: avault protocol

- Add `grant_id` to resident grant, release, run, fetch, and inject frames.
- Key cached DEKs by `{grant_id, name}`.
- Add mixed standard/protected resident `deliver.run`.
- Keep one-shot CLI for standard-only delivery.
- Update docs/DESIGN.md and protocol tests/vectors.

### Track B: Avibe backend model and service

- Remove group product model and group grant logic.
- Add grant-id first service APIs.
- Store grant `member_snapshot`, `source_selector`, `purpose`, `one_shot`,
  and readiness by `grant_id`.
- Resolve tags/skill tags to concrete secret sets.
- Auto-create access requests for protected subsets.
- Revoke/expire grants on member deletion/rotation/protection changes.

### Track C: Avibe CLI and delivery planner

- Add `vault run --tag` and keep `--skill` as sugar.
- Remove group-related CLI surfaces.
- Route mixed standard/protected runs through resident avault once.
- Keep standard-only runs on one-shot CLI.
- Keep fetch standard/protected split, but use `grant_id` for protected fetch.
- Maintain value-free error/output contracts.

### Track D: UI and request/approval flow

- Replace group UI with tags and skill tags.
- Hide `always_ask` config.
- Show request-spec-provided tags/skill tags before saving.
- Approval cards should grant protected secret sets, not groups.
- Ensure UI audience hydration remains browser-only.

### Track E: tests and cleanup

- Delete or rewrite group tests.
- Add selector union tests.
- Add mixed standard/protected run tests.
- Add grant snapshot immutability tests for tag changes.
- Add keypair rejection tests for tag-selected runs.
- Add resident-agent protocol tests for `grant_id`.
- Update docs and i18n strings.

## 13. Validation expectations

Focused local validation should include:

- `python3 -m pytest tests/test_vault*.py tests/test_avault_agent.py -q`
- `python3 -m ruff check storage/vault_service.py vibe/api.py vibe/cli.py tests/test_vault*.py tests/test_avault_agent.py`
- `cd ui && npm run build` for UI changes
- avault: `cargo test`, `cargo clippy --all-targets --all-features`, `cargo fmt --check`

Security invariants to prove in tests:

- No plaintext secret values accepted by agent/API/CLI create paths.
- No plaintext or DEK mailbox in Avibe Python.
- Agent/API/CLI request payloads remain value-free.
- Protected unlock material appears only in UI/browser-targeted payloads.
- Mixed standard/protected run starts one child through avault resident agent.
- Tag changes do not alter existing grant membership.
- Keypairs cannot be value-delivered by explicit env, tag, or skill selectors.

## 14. Non-goals for this refactor

- Backward compatibility with group grants.
- Data migration for pre-launch group rows.
- Public `always_ask` configuration UI.
- General multi-vault containers.
- Avault understanding Avibe tags, skills, groups, or UI request semantics.
- Returning plaintext values from avault or Avibe.
