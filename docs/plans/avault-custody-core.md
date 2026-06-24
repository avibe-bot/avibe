# avault — Rust custody core for Avibe Vaults (Avibe-side integration)

**Status:** Approved · implementation started. The custody core lives in its own repo:
[`avibe-bot/avault`](https://github.com/avibe-bot/avault) (Rust). The **authoritative full
design** is `avault/docs/DESIGN.md` (mirrored from this document). **This doc now tracks the
Avibe side** — how the Python daemon integrates avault — and stays in sync with the repo.
**Owners:** Vaults workstream
**Related:** #631 (Vaults P0), #632 (the "base avibe on vt" proposal that prompted this), `docs/plans/vaults.md`, `docs/plans/vaults-p2-signer.md`

> Avibe-side integration surface to build (P1): route `storage/vault_crypto.py`'s value path
> through the `avault` CLI; `ensure_avault_installed` in `vibe runtime prepare`;
> `resolve_cli_path("avault")` + a Settings · Dependencies card; keep the `vault_secrets`
> schema as the metadata source of truth. The crypto/store/signer themselves live in the
> avault repo — see its `docs/DESIGN.md`.

---

## 0. TL;DR

Build **`avault`** — a small, hardened **Rust** key-custody core under the `avibe-bot` org — and make it the only component in Avibe that ever holds key material or performs cryptography. We write it fresh (we do **not** fork `vt`), but we borrow `vt`'s proven ideas: a pure crypto core (`derive_dek` / AEAD-with-AAD / `Zeroizing`) and the "agent-as-DEK-broker" release protocol.

Avibe (Python) keeps only **metadata and orchestration**. It never holds the master key, never decrypts, and never holds reusable secret state. The wire between Python and `avault` carries only **ciphertext** or **blobs sealed to `avault`** ("blind boxes") — never plaintext, never key material.

`avault` integrates the same way `askill` does: a dependency that `vibe runtime prepare` ensures, resolved on `PATH`, surfaced in Settings · Dependencies.

Two **trust roots**, chosen per secret:

- **Standard tier (machine-rooted):** the root key lives on the machine (hardware keystore where available, `file+mlock` fallback). Headless use is allowed. Protects at-rest/disk-theft and keeps values out of the LLM, but does **not** survive a compromised machine.
- **Protected tier (human-rooted):** the root (VMK) is wrapped by a factor only the user can supply in the browser (passkey-PRF or password). The machine alone cannot decrypt. **No headless use.** Survives a compromised machine.

---

## 1. Background & motivation

### 1.1 Where Vaults P0 (#631) stands

P0 ships the **standard tier** in Python:

- Envelope encryption in `storage/vault_crypto.py`: a random per-secret DEK encrypts the value (AES-256-GCM); the DEK is wrapped under a 32-byte **machine key** at `~/.avibe/state/vault/machine.key` (mode 0600).
- Short-lived CLI processes (`vibe vault set/list/run/fetch/request/export/inject/key`) do direct-DB + direct-crypto; no daemon.
- Delivery modes: `run` (child env) and `fetch` (brokered HTTP); `export`/`inject` are help-only.
- The core invariant: **the model handles secret _names_; the platform handles secret _values_.** `$<NAME>` dynamic-ask wakes the agent with a name only; `vault_secrets` is denylisted in `data query`.

P0 is correct for what it is, and it should ship as-is. This document is about **P1+**.

### 1.2 The gaps P0 leaves open

1. **In-memory key hygiene (Python).** The machine key is an immutable `bytes`: it cannot be zeroized, it is copied into OpenSSL (a copy we do not control), and it is exposed to swap / coredump / ptrace with no `mlock` / `PR_SET_DUMPABLE` guard. This is a structural limit of Python's object model (immutability + GC + interning), not a patchable code smell.
2. **The protected tier needs a factor not on the machine.** P0 leans on a browser passkey, which is unavailable in headless / native / IM-only contexts.
3. **The signing oracle is unbuilt.** Keypair signing (ETH-first) has a design seam (`SignerProvider`) but no implementation.

### 1.3 Why a Rust custody core

For classic memory safety (no UAF/overflow) Python is already fine. The decisive difference is **secret-in-memory hygiene**: deterministic destruction, controlled copies, zeroization, page-locking, constant-time comparison. Python's object model structurally cannot deliver these; Rust can (`zeroize`, `subtle`, `mlock`).

The danger of weak hygiene scales with **how long a secret lives × how often it is reused × whether it is a key**:

- A **long-lived master key** held in Python is catastrophic — one key, alive for the whole daemon lifetime, unlocking every secret, un-zeroizable, GC-copied, swappable.
- A **transient single value** crossing one request handler is a different magnitude — bounded, single-secret, single-request.

The fix is therefore **not** "make sure no byte ever touches Python" (an absolutist claim that is impossible to fully honor and not the real issue). The fix is: **Python is never the component that holds keys, performs decryption, or keeps reusable secret state.** That is fully achievable, and it is what `avault` guarantees.

### 1.4 Why a fresh project, not a fork of `vt`

`vt` (≈6.7k LOC Rust) is an excellent reference and validates our design, but it is built for a different principal:

- Its **principal is a human at a Mac**; ours is an **autonomous agent**.
- Its **custody surface is macOS-only** (`server_macos/` is ≈54% of the codebase: Keychain, Secure Enclave/Touch ID, the 1.8k-LOC SSH-agent that also embeds its `AuthCache`, FIDO2). Only the ≈1.1k-LOC pure core (`derive_dek`, AEAD+AAD, the v2 envelope, `Zeroizing`) is cross-platform and directly reusable.
- Its trust anchor is **Touch ID**; ours must be **browser passkey/password + a cross-platform machine store**.
- Its signer is **SSH-agent (Ed25519/RSA/ECDSA)**; we need **secp256k1 (ETH)**.

Forking it means inheriting the macOS shell we would rip out, while still rebuilding the cross-platform custody we actually need. Writing fresh lets us own a clean core shaped to our model and borrow `vt`'s ≈1.1k-LOC of proven crypto ideas directly. (See Appendix A.)

---

## 2. Goals & non-goals

**Goals**

- One hardened Rust component is the sole holder of key material and sole crypto engine.
- Python never holds keys, never decrypts, never holds reusable secret state.
- Cross-platform (macOS / Linux / headless), local-first.
- Two explicit trust roots selectable per secret.
- Integrate as a `vibe runtime prepare` dependency, mirroring `askill`.
- An ETH-first signer with a path to hardware/external signers.

**Non-goals (now)**

- Replacing the P0 Python standard-tier path before P1 lands. P0 ships first.
- Third-party custody (MPC providers, WalletConnect, 1Password import) — deferred.
- A general multi-backend custody abstraction. We commit to one core (`avault`) behind the seam Vaults already designed; we do not build a plugin framework.

---

## 3. The fundamental law

> **Headless autonomous use ⟺ the decryption capability lives on the machine.**

If a secret must be usable by an unattended agent, the machine must be able to decrypt it without a human present — which means the key (or a path to it) is on the machine. You cannot have both "no human needed" and "key not on the machine." There is **no perfectly safe place** for a key that must support headless use; the honest answer is to **tier secrets by value** and pick the trust root per secret.

(Moving the root to a remote KMS/HSM only relocates the problem: the machine still holds a bootstrap credential to call it, and it breaks local-first. Noted as an escape hatch, not the default.)

---

## 4. The two trust roots (tiers)

### 4.1 Standard tier — machine-rooted

- **Where the key lives:** the OS hardware keystore where available — macOS Keychain/Secure Enclave, Linux TPM — with `file+mlock` (0600) as the headless fallback.
- **How it decrypts:** `avault` asks the keystore to release/use the master key. With a hardware element, the unwrap can happen inside the element and the raw key never leaves it.
- **Headless:** yes. This is the point of the tier.
- **What it protects:** at-rest encryption (a stolen disk/backup is useless); other processes (with a hardware store + ACL); swap/coredump; and values never enter the LLM, transcript, or Python's persistent state.
- **What it does _not_ protect:** a machine compromised under your own UID. An attacker running as you can, while you are present/unlocked, coerce a decryption. Hardware keystores make the key **non-extractable**, but **use can still be coerced while unlocked.** The real boundary here is the OS account + the hardware element, not cryptography.
- **Use it for:** API keys an agent uses headlessly.

### 4.2 Protected tier — human-rooted

- **Where the key lives:** the machine stores only the **wrapped VMK** (`wrapped_vmk`). The machine alone cannot unwrap it.
- **How it decrypts:** only in the browser, with the user's factor.
  - **Password:** `scrypt(password, salt)` → KEK → unwrap VMK locally (WebCrypto).
  - **Passkey:** WebAuthn **PRF extension** → the authenticator (Touch ID / security key) returns a stable per-credential secret → unwrap VMK. Without the physical authenticator + the user gesture, the VMK cannot be derived.
- **Headless:** no. Each use requires a live browser unlock ceremony.
- **What it protects:** disk/backup theft, **and a compromised machine** (the attacker cannot produce the passkey gesture).
- **Cost:** no unattended use.
- **Use it for:** signing keys and crown-jewel secrets.

| | Standard (machine-rooted) | Protected (human-rooted) |
|---|---|---|
| Root key at rest | master key in hardware keystore / `file+mlock` | only `wrapped_vmk`; machine can't open it |
| Unlock factor | none (OS account + hardware element) | passkey-PRF or password, **in browser** |
| Headless use | ✅ yes | ❌ no |
| Survives stolen disk | ✅ | ✅ |
| Survives compromised machine | ❌ | ✅ |
| Plaintext ever in Python | transient on create (acceptable) — or 0 with blind box | **never** |
| Typical secret | API keys | signing keys, crown jewels |

---

## 5. The blind-box boundary

`avault` holds a keypair; its **public key** is known to the browser. The rule:

> `avault` holds an **X25519** keypair; its public key is published to the browser via the daemon. Any sensitive datum that must cross the machine boundary is **sealed to that public key with HPKE** (RFC 9180 — DHKEM-X25519-HKDF-SHA256 / AES-256-GCM), producing an opaque envelope `{enc, ct‖tag}`. Python only ever relays a **blob it cannot open**. `avault` is the sole opener — and **plaintext only goes _into_ `avault`; it never comes back _out_ to Python.** `avault` returns only ciphertext, delivery side-effects, exit codes, or signatures. (Protected-tier callers must **pin / attest** `avault`'s public key — see §11.4.)

This makes "does a byte touch Python?" the wrong question. Python carries only blind boxes and ciphertext; the **keys** (machine key, VMK, DEKs, `avault`'s private key) are never in Python; and `avault`'s API is shaped so cleartext can never flow back to its caller.

### 5.1 What Python holds on each path

| Path | Source does | Python holds | `avault` does |
|---|---|---|---|
| Standard create | seals the value to `avault`'s pubkey | a blind box | open → re-wrap under machine key → returns **ciphertext** to store |
| Protected create | encrypts under VMK in the browser | ciphertext | not involved |
| Standard deliver | — | the DB ciphertext | unwrap with machine key → inject → returns **exit code** |
| Protected deliver | passkey unlock → releases the per-record **DEK**, sealed to `avault` | a blind box | open → decrypt DB ciphertext → deliver → returns **result** |

In every row Python holds only ciphertext or a blind box. (The standard-create row can instead accept a transient plaintext POST; see §11.3.)

---

## 6. Components & responsibilities

```
┌────────────┐   blind box / signature   ┌──────────────────────┐
│  Browser   │ ───────────────────────►  │  Avibe daemon (Py)    │
│ (factor,   │ ◄───────────────────────  │  metadata + relay     │
│  unlock,   │   avault pubkey, ciphertext└──────────┬───────────┘
│  signing)  │                                       │ blind box / ciphertext
└────────────┘                                       │ (never plaintext/keys)
                                                      ▼
                              ┌────────────────────────────────────┐
                              │  avault (Rust)                      │
                              │  • avault-core: AEAD+AAD, derive/   │
                              │    wrap DEK, Zeroizing              │
                              │  • avault-store: master/VMK store   │
                              │  • CLI (one-shot) + agent (resident)│
                              └───────┬───────────────┬─────────────┘
                                      │               │
                              master/VMK store     child process
                              (keychain/SE/TPM/      (env / file / HTTP egress)
                               file+mlock)
```

| | `avault` (Rust) | Avibe daemon (Python) |
|---|---|---|
| Key material | machine key / VMK / DEKs / its own keypair | **never holds any** |
| Crypto | seal, open, sign, release-DEK | **never performs any** |
| Storage | cross-platform master/VMK store | `vault_secrets` DB: ciphertext columns + all metadata |
| Metadata / orchestration | none | groups, tags, links, audit, requests, REST/UI, `$<NAME>`, IM approval cards, scope-grant bookkeeping |
| Delivery | run (child env) / fetch (HTTP egress) / inject (file) | initiates only; never touches plaintext |

The DB (`vault_secrets` and friends) stays Python-owned and is the source of truth for **metadata**. `avault` never touches SQLite; Python passes it ciphertext blobs and gets back ciphertext or results.

---

## 7. End-to-end flows

Running example: secret **`OPENAI_API_KEY`** (standard tier); task: over Slack you tell the agent "run `sync.py`," which needs the key.

Through-line legend: 🔓 plaintext · 📦 blind box (sealed to `avault`) · 🔒 ciphertext · 🗝️ key material.

### 7.1 Create

**Standard tier (blind-box variant, recommended):**

1. Browser collects name + value; 🔓 plaintext is in the browser only.
2. Browser **seals the value to `avault`'s pubkey** → 📦; `POST /api/vault/secrets` carries the blind box.
3. Daemon relays 📦 to `avault` (it cannot open it).
4. `avault`: open 📦 → read master key from store → fresh DEK → AES-256-GCM encrypt (random nonce, **AAD = `name + scheme + version`**) → wrap DEK under master key → zeroize plaintext + DEK → return 🔒 `{ciphertext, nonce, wrap_meta}`.
5. Daemon writes the row to `vault_secrets` (ciphertext, wrap_meta, preview `…last4`, `protection=standard`, audit `created`). 🔒 only; no plaintext, no key persists in Python.

**Protected tier:** step 2 is the **browser encrypting under the VMK** (it unlocks the VMK with the passkey/password first, or uses an existing VMK session) and the POST body is already 🔒. Python never sees plaintext at all.

### 7.2 Authorize

The agent (a child process of the daemon) knows it needs the **name** `OPENAI_API_KEY`, not the value.

1. Agent invokes the use, e.g. `vibe vault run --env OPENAI_API_KEY -- python sync.py` — it passes the **name**.
2. Daemon checks for an active **grant** covering this secret / session / not expired.
   - **Hit (within TTL):** skip approval, go to §7.3.
   - **Miss:** the daemon pushes an **ApprovalCard** to the current session surface (Web chat card / IM interactive message):

     ```
     🔐 Agent wants to use a secret
     Session: #sync-task  (Claude Code)
     Secret:  OPENAI_API_KEY        ← name only, never the value
     For:     python sync.py        ← the exact command
     Egress:  local child process (no network)
     [✅ Approve once] [⏱️ 15 min · this session] [📦 group · 15 min] [🚫 Deny]
     ```
3. **Only the user, in the browser/IM, can approve.** Neither the agent nor the daemon can self-grant.
4. On approval the daemon records a **grant** `{scope_type, scope_ref, session_id, expires_at}`. Within the TTL the same session reusing the same scope is not re-prompted; a different session / secret / expiry re-prompts.

Honest boundary: this protects the value from entering the LLM context / transcript / Python, and lets the user **see the exact command** the agent will run. It is not a defense against a fully compromised agent the user blindly approves; the human-reviewed command is that line of defense.

### 7.3 Decrypt & deliver

**Standard tier:**

1. Grant active. Daemon reads the row's 🔒 ciphertext + wrap_meta from the DB and hands them to `avault` with "deliver via run, command = `python sync.py`."
2. `avault`: read master key → unwrap DEK → AES-GCM decrypt + verify AAD → 🔓 plaintext in a `Zeroizing` buffer.
3. `avault` **forks `python sync.py`** with `OPENAI_API_KEY=<plaintext>` in the child's environment, waits, then zeroizes plaintext + DEK.
4. Daemon receives only the **exit code** and writes a value-free `delivered` audit row.

🔓 plaintext lived only in `avault`'s memory (wiped) and in `sync.py`'s environment. It never entered Python, the LLM context, or Slack.

**`fetch` variant:** `avault` makes the HTTP request itself, attaching the secret at egress (header/bearer/query), and returns only the response body. The value never reaches a child env or Python.

**Protected tier (DEK blind-box):** see §8.2 — the browser releases the per-record **DEK** sealed to `avault`; `avault` decrypts the DB ciphertext and delivers; the value materializes only inside `avault` for that one approved use.

---

## 8. Signing & the protected tier specifics

The pivotal distinction:

> A secret **value** (API key) is itself secret and must reach a machine-side consumer. A **signature** is public — so you never need to move the private key. **Sign where the key is unlocked.**

### 8.1 What the browser produces — a key, not a value

On a protected unlock the browser releases the **per-record DEK** (scoped to the grant), **not** the plaintext value and **not** the VMK:

- Browser pipeline: factor → KEK → unwrap **VMK** → unwrap the secret's `wrapped_dek` → **DEK (32 bytes)**. It never needs the bulk ciphertext.
- Releasing the per-record DEK (least privilege) — not the VMK — means `avault` can decrypt exactly the approved secret(s), not everything.
- Keeping the value out of the browser JS heap is deliberate; the value should materialize in `avault`, not in the browser.

### 8.2 Delivering a protected value

Browser seals the DEK to `avault`'s pubkey → 📦 → daemon relays → `avault` opens, decrypts the DB ciphertext with the DEK, and delivers. For a scope grant, the browser releases the scope's DEK-set; `avault` caches it for the TTL (resident agent, §12). The value materializes only inside `avault`.

### 8.3 ETH signing — sign in the browser

For a high-value (protected) ETH key, **sign in the browser** with `@noble/curves` secp256k1 (same `@noble` family we already use for `@noble/hashes` scrypt):

- Browser unlocks the private key, signs the tx/message locally, and returns **only the signature** (public, non-secret) through the daemon.
- The private key **never reaches `avault`, Python, or the machine.** Strongest posture; cost is no headless signing.

Honest constraints:

- **secp256k1 is not supported by Secure Enclave / passkeys (all P-256).** So an ETH key is a **software key gated by a hardware factor** (passkey provides the unlock gesture; the key itself runs in browser JS via `@noble`). Putting the private key in hardware requires a **hardware wallet (Ledger / WalletConnect)** — the `external` `SignerProvider`, deferred.
- Browser JS heap holds the key briefly; typed `Uint8Array` can be wiped after use (better than Python's immutable `bytes`), and the exposure is one operation while the user is present.

### 8.4 If you want unattended signing

Set the ETH key to the **standard tier** and have **`avault` sign** with a machine-rooted key (`avault` gains a secp256k1 signer). Weaker (the machine can sign while you are away) but enables automation. Choose the tier by the signing key's value.

This maps onto the `SignerProvider` ladder: **local** (protected = browser-sign / standard = `avault`-sign) → **external** (hardware wallet, strongest, deferred) → **mpc** (deferred).

Unifying principle:

> **Value → browser releases the DEK; `avault` decrypts & delivers (value materializes in `avault`).**
> **Signature → sign at the unlock point (browser); return only the signature (the private key never moves).**

---

## 9. Authorization & grants

- **ApprovalCard** (§7.2) is rendered on the current session's surface (Web chat / IM). It shows the agent/session, the secret **name**, the exact command/host, the requested scope, and TTL options. It never shows the value.
- **Scope-typed grants:** `{scope_type ∈ {secret, group, …}, scope_ref, session_id, expires_at}`. Recorded by the daemon; suppress re-prompts within the TTL.
- **DEK cache (resident agent, P2):** after the first release, `avault` caches the unwrapped DEK-set for the grant TTL. Repeated uses in the window don't re-hit the store or re-prompt. The daemon proves it is the authorized caller via `SO_PEERCRED` (peer credential on the socket) — not a shared token. On expiry the cache is cleared and zeroized.

Generalized from `vt`'s `AuthCache` rigor: strict TTL with **no** sliding refresh, idempotent grants, PID-reuse defense, lock-clears-the-cache — but re-keyed from `{TTY/app}` to `{scope_type, scope_ref, session_id}` and fed by the UI/IM approval path alongside (or instead of) a biometric one.

---

## 10. Envelope & crypto

- **Keep the P0 `wrap_meta` column shape** (`{scheme, wrapped_dek, dek_nonce}`): storing `wrapped_dek` gives **cheap master rotation** (re-wrap DEKs without touching ciphertext) and does not break the P0 DB. We do **not** adopt `vt`'s pure-derive model (`derive_dek(master, salt)` with nothing stored), which forces a full re-encrypt on every master rotation. The hygiene win comes from Rust owning the key + crypto, which is orthogonal to the envelope shape.
- **Borrow from `vt`:** AES-256-GCM with **AAD binding `name + scheme + version`** (so ciphertext can't be transplanted between records), HKDF-based DEK derivation where applicable, decrypt results in `Zeroizing`, constant-time compare (`subtle`).
- **Protected tier:** VMK wrapped by N factor-copies (password via scrypt; passkey-PRF copies added browser-side), each secret's DEK wrapped by the VMK — the format already prototyped in `storage/vault_protected.py` and `ui/src/lib/vaultCrypto.ts`, now produced/consumed across the browser ↔ `avault` boundary.

---

## 11. Memory hygiene — why Rust, and the honest residuals

### 11.1 Why Python can't

- `bytes`/`str` are **immutable** → no in-place overwrite; you wait for GC and can't verify erasure.
- GC may **move/copy** objects; small strings may be **interned**.
- Passing into the crypto lib makes an **OpenSSL C-side copy** Python doesn't manage.
- No `mlock` → pages can **swap** to disk; no `PR_SET_DUMPABLE=0` → **coredump/ptrace** can read it.

For a **long-lived master key** every one of these is exposed for the whole daemon lifetime. That is the structural problem.

### 11.2 Why Rust can

`Zeroizing<…>` buffers wiped on deterministic `Drop`; constant-time comparison (`subtle`); `mlock` + `PR_SET_DUMPABLE(0)` on the key pages; tight control over copies. `avault` holds keys for the minimum window and wipes them.

### 11.3 The honest residual on standard-create

If standard-create takes a **plaintext POST** (no blind box), the value exists as one transient `bytes` in one Python request — un-zeroizable, briefly swappable. It is bounded (single value, single request, not reused) and the daemon is **in-boundary for the standard tier anyway** (it can ask `avault` to decrypt any standard secret). So it is an acceptable, minimized residual — or eliminated entirely by the **blind-box create** (§7.1), which is the recommended default.

### 11.4 The other honest caveat — pubkey integrity

The blind box assumes the browser gets `avault`'s **genuine** public key. A fully compromised daemon could substitute its own key when relaying. For the **standard tier** the daemon is in-boundary (not in the threat model). For the **protected tier** (where a compromised daemon _is_ in scope), `avault`'s pubkey must be **pinned / attested** (TOFU + pin, or shown to the user) — an explicit control, called out, not buried.

---

## 12. Integration model — like `askill`

`askill` is the precedent: a required local dependency that `vibe runtime prepare` ensures (`ensure_askill_installed`), resolved via `resolve_cli_path("askill")`, reported by `askill_status()`, surfaced in **Settings · Dependencies**, with a managed auto-reconcile loop.

`avault` mirrors the **touchpoints** exactly:

- `vibe runtime prepare` → `ensure_avault_installed` (idempotent; skipped under `--offline`).
- `resolve_cli_path("avault")` on `PATH`; config `agents.avault.cli_path`.
- `avault_status()` → installed / version / path, shown as a Settings · Dependencies card.

**Distribution — recommend the Show-Runtime model over `askill`'s `curl | sh`.** A custody binary is version-sensitive (client/agent must match; `vt`'s own README warns about lockstep upgrades). So ship **per-platform prebuilt `avault` binaries bundled in the wheel + a manifest, version-locked to the avibe release** (exactly how Show Runtime ships its `*.tgz`), rather than a floating `curl | sh`. The integration touchpoint stays identical; the distribution is safer.

**Two run modes (same binary):**

1. **CLI one-shot (P1):** the daemon spawns `avault seal/open/...`; it reads the master key, uses it, wipes it on `Drop`, exits. The common path is `askill`-shaped, and the per-op window already gets Rust hygiene.
2. **Resident agent (P2):** `avault agent` listens on a unix socket, holds the grant DEK-set for the TTL, and is the signing oracle. The daemon authorizes via `SO_PEERCRED`. Only grant-caching and signing pay this cost.

**Ingest without Python reading plaintext:** for the CLI `set` path, pass stdin's **file descriptor** straight to the `avault` subprocess (Python never `read()`s the bytes). For the web path, the browser uses the blind box (§7.1).

---

## 13. Cross-platform key stores

`avault-store` selects the strongest local store available:

- **P1:** `file + mlock` (0600) — works headless, on Linux, and on macOS. Universal baseline.
- **Later, as factors / stronger roots:** macOS **Keychain / Secure Enclave**, Linux **TPM** (seal/unseal, optional PCR/auth binding), cloud **KMS** KEK. These raise standard-tier strength (non-extractable keys) and can serve as protected-tier factors on the machine for the no-browser case.

This is an internal store selection inside `avault`, not an Avibe-level plugin layer.

---

## 14. Project shape

- **Repo:** `avibe-bot/avault` (name provisional — see §16).
- **Cargo workspace:**

  ```
  avault/
  ├─ crates/
  │  ├─ avault-core/    # pure crypto: AEAD+AAD, derive/wrap DEK, envelope, Zeroizing. No I/O, no platform deps. Unit-tested, auditable.
  │  ├─ avault-store/   # cross-platform master/VMK store: file+mlock (P1) → keychain/SE/TPM/KMS
  │  └─ avault-cli/     # the `avault` binary: one-shot ops + the resident agent
  └─ ...
  ```

- `avault-core` is the auditable heart; it borrows `vt`'s proven crypto shapes and has no platform or I/O dependencies.

---

## 15. Roadmap

| Phase | Scope | State |
|---|---|---|
| **P0** | Python standard tier: DB + envelope + delivery + `$<NAME>` (#631) | **done — keep & merge; not replaced before P1** |
| **P1** | `avault-core` + CLI + `file+mlock` store; Rust takes standard-tier seal/open; blind-box create; `vibe runtime prepare` ensure + Dependencies card. Closes the memory-hygiene gap. | next |
| **P2** | Resident agent + `SO_PEERCRED` + scope-grant DEK cache + signer (secp256k1; approval-card context in the sign prompt). Protected-tier non-browser factors via hardware stores. | after P1 |
| **P3** | Multi-factor (passkey-PRF copies, TPM, KMS KEK); external `SignerProvider` (hardware wallet / WalletConnect). | later |

**Recommendation:** make P1 **CLI-only** — push the agent, grants, and signing to P2 — so the first step is small and headlessly verifiable.

---

## 16. Open decisions

1. **Name:** `avault` / `avibe-custody` / other?
2. **Envelope:** keep `wrap_meta` / `wrapped_dek` (cheap rotation, no DB break — recommended) vs `vt`'s pure-derive?
3. **Distribution:** bundled-in-wheel + manifest, version-locked (recommended) vs `askill`-style `curl | sh`?
4. **P1 = CLI-only first?** (recommended yes.)
5. **Protected-tier pubkey trust:** pinning vs attestation mechanism for `avault`'s public key.

---

## 17. Honest residuals (collected)

- **Standard-create transient plaintext** in Python if not using the blind box — bounded, acceptable, or eliminated by blind-box create (§11.3).
- **`avault` pubkey distribution integrity** — in-boundary for standard; needs pin/attest for protected (§11.4).
- **Standard tier ≠ machine-compromise resistance** — it is at-rest + use-gating + no-LLM-exposure, not "safe if the box is owned" (§4.1).
- **Browser JS hygiene** is best-effort (wipeable typed arrays, non-extractable WebCrypto keys), not a secure enclave; exposure is one operation while the user is present (§8.3).
- **secp256k1 is not hardware-backed** on Apple SE / passkeys — software key + hardware unlock factor; true hardware custody needs an external wallet (§8.3).

---

## Appendix A — relationship to `vt`

**Borrow (the ≈1.1k-LOC pure core ideas):** `AesGcmCrypto`, `derive_dek` (HKDF), the AEAD-with-AAD discipline, the v2 envelope + **DEK-release** protocol, `AuthCache`'s rigor (as our grant cache), and the zeroize discipline throughout.

**Don't inherit (the ≈3.6k-LOC macOS shell):** the Keychain-only store, the SSH-agent user surface, FIDO2 enrollment, TOTP, the remote-sudo PAM path, the `VT_AUTH` shared-token channel, and the legacy `vt://mac` format.

**Build fresh for us:** cross-platform store (`file+mlock` → keychain/SE/TPM/KMS), per-record standard/protected policy, `SO_PEERCRED` daemon authorization, scope-typed grants fed by UI/IM approval, the `name+scheme+version` AAD aligned to our columns, a secp256k1 signer, and the browser-sign path for protected ETH keys.

Net: `vt` proves the model and donates the crypto shapes; `avault` is the clean, cross-platform, agent-shaped custody core those shapes belong in.

---

## Appendix B — cryptographic primitives (review reference)

Concrete, implementation-ready values for every step.

| Step | Primitive / parameters |
|---|---|
| Symmetric AEAD | AES-256-GCM · 96-bit nonce · 128-bit tag |
| DEK / master / VMK | 256-bit, CSPRNG |
| DEK wrap | AES-256-GCM key-wrap (under master key or VMK) |
| AAD binding | `name ‖ scheme ‖ version` |
| Blind-box sealing | HPKE (RFC 9180) · DHKEM-X25519-HKDF-SHA256 · AES-256-GCM → `{enc, ct‖tag}` |
| Password KDF | scrypt `N=2^15, r=8, p=1` → 256-bit KEK |
| Passkey factor | WebAuthn PRF extension (CTAP2 `hmac-secret`) → 32 B → KEK |
| Signing | ECDSA / secp256k1 · EIP-155 / EIP-1559 · keccak256 digest |
| Browser libs | `@noble/curves`, `@noble/hashes`, WebCrypto |
| Rust libs | `aes-gcm`, `hkdf`, `hpke`, `x25519-dalek`, `k256`, `zeroize`, `subtle` |
| Resident-agent auth | `SO_PEERCRED` (Linux) / `LOCAL_PEERCRED` (macOS) on the unix socket |
| Memory hardening | `mlock(2)` · `prctl(PR_SET_DUMPABLE, 0)` · `madvise(MADV_DONTDUMP)` |
| At-rest storage | SQLite, base64 text columns (`ciphertext` / `nonce` / `wrap_meta`) |

These are starting recommendations, not frozen choices — items #2 (envelope) and #5 (pubkey trust) in §16 may still move them.

---

## Appendix C — avault interface & transport

### Minimal interface

`avault` exposes a deliberately narrow verb set. The defining property: **there is no `decrypt → plaintext` verb.** Plaintext only goes _in_ (sealed); it can only be *delivered* or *signed*, never returned to the caller.

| Verb | Input | Output | Purpose |
|---|---|---|---|
| `pubkey` | — | X25519 public key + fingerprint | the browser fetches this before sealing a blind box (protected tier must pin / attest it) |
| `seal` | blind box (the value) + name/scheme | envelope `{ciphertext, nonce, wrap_meta}` | standard-tier create: open box → wrap DEK under master → return ciphertext (never plaintext) |
| `deliver` | envelope + mode (`run` / `fetch` / `inject`) + *optional* DEK blind box | exit code / response body | decrypt and deliver. No DEK ⇒ standard tier (master key); with DEK ⇒ protected tier (browser-released DEK) |
| `sign` | key envelope + digest/tx + *optional* DEK blind box | signature (public) | standard-tier signing (secp256k1); the private key never leaves `avault` |
| `key export` / `key import` | passphrase (stdin) | encrypted backup / ok | back up, migrate, restore the master key |

The resident agent (P2) adds `grant` / `release`: cache a scope's DEK-set for a TTL so repeated uses in-window skip re-unlock. Standard-tier signing of an ETH key is `sign`; protected-tier ETH signing happens entirely in the browser and never reaches this interface.

### Transport

Two modes, the same integration touchpoints as `askill`. **Both channels carry only names, blind boxes, ciphertext, and results — never plaintext or keys.**

- **P1 — CLI subprocess (askill-shaped).** Avibe spawns the `avault` binary. Control args via argv/JSON; **bulk blobs (blind boxes, ciphertext) via stdin** (kept out of argv so they don't show in `ps`); results via **stdout JSON**. The `run` child inherits stdio. One-shot: use the key, zeroize, exit.
- **P2 — resident agent (unix socket).** `avault agent` listens on `~/.avibe/run/avault.sock` (0600) and exchanges **length-prefixed JSON frames**. Authorization is **`SO_PEERCRED` (Linux) / `LOCAL_PEERCRED` (macOS)**: `avault` reads the connecting peer's uid/pid to confirm it is the same-user Avibe daemon — **no shared token**, so no decrypt-authorizing secret is re-introduced into Python. The agent is resident so it can hold the grant DEK-cache and act as the signing oracle (keys held across calls).

### Where avault's own keys live (esp. Linux without a Keychain)

- **The X25519 receiver keypair is ephemeral and in-memory.** It is only used to open blind boxes, so it is generated at agent start (or per CLI invocation) and **never written to disk**. The public key is published on demand; the protected tier pins/attests the *current* public key (re-pin on agent restart). This leaves **only the master key** needing durable secure storage.
- **Master-key store on Linux (strongest available wins):**
  - **TPM 2.0** (present on most Linux hosts) — seal the master key to the TPM; the wrapping key never leaves the chip, optionally bound to PCRs/policy. This is the Linux analog of Keychain/Secure Enclave.
  - **systemd-creds / kernel keyring** — unseal at service start (via TPM or a host key) into non-swappable kernel memory; good for headless services.
  - **`file (0600) + mlock`** (the no-hardware floor) — owned by the service user, kept out of swap and coredumps.
- **Honest floor:** with no hardware root and no operator factor, the master key's at-rest protection reduces to the **OS user account** (the fundamental law again). `file+mlock` is plaintext-at-rest: it resists other users / remote / a stolen disk (with full-disk encryption), but not an attacker already running as your uid. Optional hardening: wrap the master key under a **boot-time passphrase KEK** or a **cloud KMS KEK** (stronger at rest, at the cost of headless start or a network + bootstrap credential). And note: the **protected tier stores nothing decryptable on the box at all** — for high-value secrets, that side-steps the Linux at-rest question entirely.

### Authentication — who may call avault

- **Other users / remote: refused.** The socket is `0600`, owned by the service user; `avault` checks the kernel-supplied peer **uid** (`SO_PEERCRED`/`LOCAL_PEERCRED`, unforgeable) and accepts only its own uid. There is no network listener. The P1 CLI is `fork`/`exec`-ed directly by the daemon, so there is no "someone else connects" surface at all.
- **Another program running as the *same* user: can call avault — by design.** The standard tier's boundary *is* the OS account: an attacker already running as your uid can read the `file+mlock` master key, `ptrace` the daemon, etc. — so refusing same-uid callers would be security theater.
- **Why same-uid is still acceptable — three backstops + one root answer:**
  1. **Narrow interface** — even a same-uid caller can only `deliver`/`sign` (results); there is no `decrypt → plaintext`.
  2. **Full audit** — every call is recorded.
  3. **Hardware non-extractability** — with TPM/SE the key can't be exfiltrated; an attacker can at most *coerce a use* (which is audited), not steal the key.
  4. **Root answer = the protected tier** — `avault` has no VMK and cannot decrypt without a browser-released DEK, so for high-value secrets "who can call avault" stops mattering; the cryptography enforces it.
