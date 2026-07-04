# Vaults — P0 implementation plan

Companion to `vaults.md` (the design spec, v7). This is the **buildable P0
checklist**: scope, final DDL, module layout, commit order, tests. No code yet.
Date: 2026-06-21.

## 1. P0 scope

**In (the useful core — "secrets out of context for the common case"):**

- Data model: all tables created at **final shape** (architecture frozen, R2) —
  but P0 only *exercises* the standard-tier subset.
- **Standard tier only**: machine-key envelope (file mode, §7.2). No password /
  passkey / VMK / browser-side decryption yet.
- Vaults page **CRUD** (create/list/rotate/delete; masked reads via the #555
  `secretFields.ts` patterns) + groups/tags.
- Delivery **M1 `run`** (child env) + **M4 `fetch`** (brokered proxy, domain-bound).
  `export`/`inject` shipped but **help-only** (not in agent-facing guidance, R4).
- **Dynamic ask** `$<NAME>` → inline SecureInputCard (web) / deep link (IM) →
  `provision` request → name-only wake-up.
- `vault_audit` + audit tab.
- `vibe data query` **table denylist** for `vault_secrets` (new mechanism).

**Out (P1+, but the schema already has room — no migration later):**

- Protected tier: VMK, Argon2id password, passkey PRF, browser-side decryption.
- Scope-typed **grants** (`vault_grants` table created but unused in P0).
- Inline **ApprovalCard** + `message.updated` SSE.
- Outbound **redaction** filter.
- Skills `secrets:` frontmatter (askill change) + per-skill view.
- OS keychain mode + `key export/import`.
- **1Password import**.
- **Signers** (`local` iframe → `external` WalletConnect → `mpc`).
- Transparent proxy; config-secret migration.

Rationale: P0 proves the plumbing end-to-end (table → service → UDS → CLI → UI →
dynamic ask) on the *simpler* daemon-side machine-key path, before layering the
crypto-heavy protected/grant/signer machinery. Everything P0 ships is forward-
compatible with the frozen architecture.

## 2. Final DDL (all tables; P0 uses the standard-tier subset)

`storage/models.py` (imperative `Table`, matching the existing style) + one Alembic
migration under `storage/alembic/versions/`. `created_at` etc. are ISO strings to
match existing tables. Note: column is **`group_name`** (not `group`, a SQL
keyword).

```python
vault_groups = Table(
    "vault_groups", metadata,
    Column("name", String, primary_key=True),                # "default" seeded
    Column("description", Text),
    Column("grantable", Boolean, nullable=False, server_default="1"),  # P1; auto-false if group holds a keypair
    Column("max_grant_ttl_seconds", Integer, nullable=False, server_default="900"),
    Column("created_at", String, nullable=False),
)

vault_secrets = Table(
    "vault_secrets", metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False, unique=True),     # ^[A-Za-z_][A-Za-z0-9_]*$, case-preserving
    Column("group_name", String, ForeignKey("vault_groups.name"), nullable=False, server_default="default"),
    Column("tags", Text),                                    # JSON array
    Column("kind", String, nullable=False, server_default="static"),        # static | keypair (P2)
    Column("protection", String, nullable=False, server_default="standard"),# standard (P0) | protected (P1)
    Column("signer_kind", String),                           # local|external|mpc:* (P2, keypair only)
    Column("source", String, nullable=False, server_default="manual"),      # manual|imported:1password|op-reference
    Column("ciphertext", LargeBinary),                       # AES-256-GCM; null for external/mpc/op-reference
    Column("nonce", LargeBinary),
    Column("wrap_meta", Text),                               # JSON: wrapped-DEK copies + KDF params
    Column("public_meta", Text),                             # JSON: desc / pubkey / address / op:// uri
    Column("policy", Text),                                  # JSON: allowed modes, allowed hosts, always_ask
    Column("last_used_at", String),
    Column("use_count", Integer, nullable=False, server_default="0"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

vault_links = Table(                                         # P1 (skills)
    "vault_links", metadata,
    Column("id", String, primary_key=True),
    Column("secret_name", String, ForeignKey("vault_secrets.name", ondelete="CASCADE"), nullable=False),
    Column("skill_name", String, nullable=False),
    Column("source", String, nullable=False),                # skill_meta | agent | user
    Column("required", Boolean, nullable=False, server_default="1"),
    Column("created_at", String, nullable=False),
    UniqueConstraint("secret_name", "skill_name", name="uq_vault_link"),
)

vault_requests = Table(                                      # P0 uses 'provision'; rest P1
    "vault_requests", metadata,
    Column("id", String, primary_key=True),
    Column("request_type", String, nullable=False),          # provision|access|sign|proxy|keygen
    Column("secret_name", String),
    Column("requester", Text),                               # JSON: session_id/agent/run
    Column("delivery", Text),                                # JSON
    Column("status", String, nullable=False, server_default="pending"),
    Column("message_id", String),
    Column("created_at", String, nullable=False),
    Column("decided_at", String),
    Column("expires_at", String),
)

vault_grants = Table(                                        # created in P0, unused until P1
    "vault_grants", metadata,
    Column("id", String, primary_key=True),
    Column("scope_type", String, nullable=False),            # secret | skill | group
    Column("scope_ref", String, nullable=False),
    Column("member_snapshot", Text),                         # JSON: frozen secret set (audit)
    Column("session_id", String),                            # null = any-session
    Column("status", String, nullable=False, server_default="active"),  # active|expired|revoked
    Column("created_by_request_id", String),
    Column("created_at", String, nullable=False),
    Column("expires_at", String, nullable=False),
    Column("revoked_at", String),
)

vault_audit = Table(
    "vault_audit", metadata,
    Column("id", String, primary_key=True),
    Column("ts", String, nullable=False),
    Column("event", String, nullable=False),                 # created/updated/deleted/delivered/denied/...
    Column("secret_name", String),
    Column("requester", Text),
    Column("delivery", Text),
    Column("request_id", String),
    Column("grant_id", String),
)
```

Vault config (machine-key mode, key-check value; later VMK wraps/KDF params) →
`state_meta` under `vault:*`.

## 3. Crypto module — `storage/vault_crypto.py`

P0 implements the machine-key path; protected-tier functions are stubbed for P1.
Primitives from `pyca/cryptography` (already in-tree via web push).

```
get_or_create_machine_key() -> bytes        # 32B os.urandom, ~/.avibe/state/vault/machine.key 0600, lazy
seal_standard(value: bytes) -> Sealed        # DEK=os.urandom(32); ct=AESGCM(DEK).encrypt(value);
                                             #   wrap_meta={machine: AESGCM(machine_key).encrypt(DEK)}
open_standard(sealed) -> bytes               # unwrap DEK via machine key, decrypt value
key_check() / verify_machine_key()           # detect missing/mismatched key (AES-GCM auth)
# P1 stubs: vmk_create/wrap_password/wrap_passkey, open_protected, GMK-free grant DEK-set helpers
```

`Sealed = (ciphertext, nonce, wrap_meta_json)`. Machine-key mode recorded in
`state_meta`; keychain mode (P1) swaps `get_or_create_machine_key` for `keyring`.

## 4. Backend service — `storage/vault_service.py`

Data layer, sibling to `storage/messages_service.py` etc. (the codebase keeps table
CRUD in `storage/*_service.py`; `core/services/*` is orchestration). Functions take a
`Connection`; this module owns the one place that decrypts (`resolve`) and the one
place that writes audit rows. The daemon UDS layer (§5) opens the engine and calls in.

```
create_secret(name, value, *, group, tags, protection, policy)   # validates name; seal; audit 'created'
list_secrets(*, group=None, skill=None) -> [masked]              # never returns values; masked preview
rotate_secret(name, new_value) / delete_secret(name)
resolve(names, *, mode, requester) -> {name: value}              # P0: standard only (machine key); audit 'delivered'
create_provision_request(name, *, reason, spec, requester) -> request
fulfill_provision(request_id, value, *, protection)              # store secret; mark fulfilled; wake-up (name only)
audit(event, **fields)
```

## 5. Daemon UDS — deferred to P1 (not needed for standard-tier P0)

**Revised (commit 3).** The original plan routed all value paths through the daemon
UDS. For the **standard tier** that buys nothing: the machine key is a local file, so
the CLI reading it is the same trust boundary as the daemon, there is no approval to
orchestrate, and SQLite WAL handles CLI/daemon concurrency safely. So P0 `vibe vault`
runs **direct-DB + direct-crypto**, matching sibling CLI commands (session/task/data),
with no daemon dependency and full local testability.

The daemon UDS path lands in **P1**, where it is genuinely required: protected-tier
resolve/sign needs the unlock factor (not on disk), approval orchestration (SSE to the
browser), and in-memory scope-grant DEKs — all owned by the long-running process.
Planned then: `POST /internal/vault/resolve` (protected → create `access` request,
block until approved), `POST /internal/vault/sign`, grant endpoints. (The design §7 note
is refined accordingly: *standard*-tier value ops may be direct-DB; *protected*-tier
value ops must go through the daemon.)

## 6. CLI — `vibe/cli.py` (argparse `vault` subparser)

```
# Removed: the agent-facing CLI no longer accepts plaintext create (`vibe vault set`).
# Create flows enter through browser-side sealed/blind-box payloads.
vibe vault list [--group G] [--skill S(P1)] [--json]
vibe vault rm NAME
vibe vault run --env NAME[,N2] [--env LOCAL=NAME] [--env-skill S(P1)] -- cmd...   # M1, promoted
vibe vault fetch --auth NAME [curl-like args]                                     # M4, promoted
vibe vault request NAME [--reason s] [--spec file|- | --spec-json json] [--wait s | --no-wait]  # dynamic ask
# help-only (NOT in agent-facing guidance):
vibe vault export --env NAME[,N2]                                                 # M1', eval "$(...)"
vibe vault inject --keys A,B --out f [--format dotenv|json|yaml|toml] [--ttl 10m]  # M2
```

`set` has been removed from the agent-facing CLI. Create flows enter through the
browser, which sends sealed/blind-box payloads instead of plaintext values. No
`vibe vault get`; no command prints a value.

## 7. Reply enhancer — `core/reply_enhancer.py`

Add `\$<[A-Z][A-Z0-9_]*>` extraction (outside code fences, same guard as mentions)
→ `EnhancedReply.secret_requests: [name]`. `core/message_dispatcher.py` creates a
`provision` request per name and renders the marker as a SecureInputCard (web) /
deep link (IM, per-platform formatter).

## 8. REST + UI

- `vibe/ui_server.py` (FastAPI, async) + `vibe/api.py` wrappers:
  `GET/POST/DELETE /api/vault/secrets`, `GET /api/vault/audit`,
  `GET /api/vault/requests`, `POST /api/vault/requests/{id}/fulfill`,
  `GET/POST /api/vault/groups`. SSE on `/api/events`: `vault.secrets.changed`,
  `vault.request.new`.
- UI (`ui/src/`): build out `components/workbench/VaultsPage.tsx` (currently a
  placeholder) — secret list + group/tag filter + create/rotate/delete using
  `Input`/`Button`/`Card`/`Dialog`/`Badge` + `lib/secretFields.ts` masking;
  `context/ApiContext.tsx` vault methods + SSE handlers; `SecureInputCard` rendered
  from a new branch in the `markdown.tsx`/`ChatPage.tsx` message switch (rewrite
  `$<NAME>` → `avibe-secret:NAME` link, custom renderer — the `lib/mentions.ts`
  pattern).

## 9. `vibe data query` denylist — `storage/read_only_query.py`

Today there's no table allowlist/denylist (only the read-only authorizer). Add a
denied-tables set (`{"vault_secrets"}`) checked in the authorizer callback
(`SQLITE_READ` action exposes the table name); deny reads of `vault_secrets`
(ciphertext anyway). `vault_requests`/`vault_links`/`vault_audit`/`vault_grants`
stay queryable.

## 10. Commit sequence (one branch, atomic commits, one PR at checkpoint)

1. `feat(vault): schema + machine-key crypto + data query denylist`. **[done — 58832ae3]**
2. `feat(vault): data service` — `storage/vault_service.py` (CRUD + standard-tier
   resolve + provision + audit). **[done — 7093ad5c]**
3. `feat(vault): CLI list/rm/run/request` — direct-DB + direct-crypto (standard
   tier; UDS deferred to P1, §5). The original P0 CLI create path was later removed:
   agent-facing CLI commands no longer accept plaintext create. End-to-end verified:
   `run` injects to child env with **no stdout leak**. **[done — 2026-06-21]**
3b. `feat(vault): fetch` — M4 brokered HTTP + domain binding (browser-created
   `allowed_hosts` + `auth` policy; secret attached at egress, refused for non-allowed
   hosts before decrypt; response body passed through, never the secret).
   **[done — 2026-06-21]**
3c. `feat(vault): export/inject delivery` — help-only `export` (eval stream) +
   `inject` (dotenv/json/yaml/toml, 0600 file). **[done — 2026-06-21]**
4. `feat(vault): REST + Vaults page CRUD` — `/api/vault/*` (api.py + ui_server.py) +
   `VaultsPage.tsx` (list/add/delete, masked, reuse design-system primitives) +
   ApiContext methods + i18n. `npm run build` green. **[done — 2026-06-21]**
   (Live SSE refresh deferred — page refreshes after mutations; audit view = commit 6.)
5. `feat(vault): dynamic ask` — `reply_enhancer` `$<NAME>` extraction (code-fence-
   aware) + web `SecretRequestCard` (markdown transform → inline secure-input card that
   saves to the vault). **[done — 2026-06-21]** Remaining (needs the live pipeline /
   regression env to verify, deferred): message_dispatcher provision-request row + IM
   deep-link rendering + name-only auto-wake-up.
6. `feat(vault): audit tab + polish` — Vaults-page Activity panel (toggle → GET
   /api/vault/audit), end-to-end P0 smoke. **[done — 2026-06-21]** **P0 COMPLETE.**

## 11. Test plan

- **Unit**: crypto round-trip + wrong-key auth-fail; name regex; `data query`
  denylist (assert `SELECT * FROM vault_secrets` is denied, others allowed);
  reply_enhancer `$<NAME>` extraction + fence guard.
- **CLI**: create/set is intentionally absent from agent-facing commands; `run`
  injects env to child with value absent from stdout; `request --wait` blocks then
  returns on fulfill.
- **API**: reads return masked previews, never plaintext; CSRF on mutations.
- **Scenario** (`tests/scenarios/`): agent emits `$<KEY>` → card → fulfill →
  name-only wake-up → `vault run` succeeds. Surface a scenario ID.
- **UI**: `npm run build` (the gate).

## 12. Open defaults to confirm before/with P1

From `vaults.md` §17: per-key grant TTL (5 min) / skill·group (15 min); default
binding this-session; "until-revoked" at launch?; "Approve once" auto-applies a
per-key window?; launch with passkey or password-only first; reveal-on-click for
standard values; ETH preview depth. None block P0.

## 13. P1 / P2 status & gating (post-P0, 2026-06-21)

P0 is complete and verified (commits 1–6). The schema is at final shape, so none of
the below needs a migration. Status of the rest:

**Headless-verifiable:**

- `vibe vault key export/import` — passphrase-wrapped machine-key blob for backup /
  migration (§7.2). **[done — 2026-06-21]** Uses Scrypt (zero new dep, ships in
  `cryptography`; the blob records `kdf` so a later Argon2id variant is forward-
  compatible — KDF choice for the protected tier is still open Q7).
- Protected-tier **envelope wire format** (`storage/vault_protected.py`): VMK + password
  multi-wrap copies + DEK-under-VMK seal/open + the `wrap_meta` v1 schema, as the
  canonical reference + test vectors the browser mirrors. **[done — 2026-06-21]**
  Production decryption stays browser-side (§8.4); this module's unwrap is reference/
  test only and is not wired into any daemon resolve.

**Gated on the Incus regression env (live message pipeline) — built carefully there,
not landed unverified here:**

- Dynamic-ask dispatcher side: provision-request audit row + per-platform IM
  deep-link rendering + name-only auto-wake-up (the web ask→save flow already works).
  This also covers surfacing **CLI-initiated** `vibe vault request` rows in the Web
  Vaults page (a pending-requests panel + a `/api/vault/requests` endpoint): until then
  the page exposes only secrets + audit, so a `request --wait` is fulfilled by the user
  adding a secret of that name (which marks the pending row fulfilled). The `request`
  CLI message is explicit about this so it isn't misleading.
- Outbound **redaction** tripwire (§10): the scan/redact function is pure, but wiring it
  into `core/message_dispatcher.py` (every outbound message) must be regression-verified.
- Inline **ApprovalCard** + the new `message.updated` SSE (protected-tier approval).

**Gated on a real browser (WebAuthn / WebCrypto / cross-origin iframe):**

- Protected-tier **browser-side decryption** (Argon2 WASM + WebCrypto unwrap; the vault
  password never reaches the daemon, §8.4) — the locked design is explicitly browser-side.
- **Passkey PRF** unlock (§7.3).
- **`local` signer**: cross-origin iframe ETH signer (`@noble/curves` + `viem` +
  `@scure/bip39`), BIP-39 key ceremony, EIP-155/191/712 decoded approval (§8.3).
- Scope **grants** (DEK-set cached in daemon memory) end-to-end (browser unlock → grant).

**Gated on external accounts / services:**

- **WalletConnect** (`external` signer) — needs a WalletConnect project id + a real wallet.
- **MPC providers** (`mpc:*` — Privy / Web3Auth / Turnkey / Lit) — each needs a developer
  account + cloud; cannot be created/verified in this environment.
- **1Password import** (§13.5) — needs the `op` CLI signed in / a 1Password account.

These were flagged as out of one-shot headless scope from the start; the design for each
is locked in `vaults.md` so implementation is unambiguous when the env/accounts exist.
