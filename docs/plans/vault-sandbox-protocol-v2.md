# Vault authorization model & sandbox protocol v2

Status: design proposal for discussion. No implementation yet.
Owner: Vaults workstream.
Date: 2026-07-09.
Relates: `vault-crypto-sandbox.md` (v1 sandbox design), `vaults-grant-delivery-refactor.md`,
`vault-protected-delete-authz.md`, vault-sandbox repo `README.md`.

## 1. Why a redesign

The cross-origin sandbox was retrofitted onto a protected-vault implementation
that originally kept passkeys in the main app. The retrofit moved the crypto
correctly (VMK, PRF, signing, DEK release all live in `sandbox.avibe.bot` now),
but three things never got redesigned around the new boundary:

1. **The authorization model.** v1 answered "how do we stop a compromised
   parent" with "fresh passkey UV per operation" and separately kept a 10-minute
   unlock window. The result: the unlock window exists, is surfaced in the UI
   with a countdown, and buys the user *nothing* — every sensitive operation
   still pops an OS passkey prompt.
2. **The protocol.** RPC payloads grew ad hoc per operation. The context a
   human needs to authorize (which agent, which session, what command, what
   egress, how long) never crosses into the sandbox, so the sandbox — the one
   surface the parent cannot spoof — renders raw IDs while the spoofable parent
   card renders the rich story.
3. **The API surface.** No batching (N secrets = N ceremonies), no events
   (parent polls/mirrors with its own duplicate timer), no reveal entry point
   (`unseal` is implemented but unreachable), no policy/config surface, and the
   keypair-create flow degenerated into a paste-a-private-key dialog.

This document proposes the unified redesign: an **authorization session
model** (risk-tiered, one passkey per human intent), a **versioned, evented,
context-carrying protocol v2**, and a **single-clock TTL model** with clear
product language separating "vault unlocked" from "agent grant duration".

## 2. Verified current behavior (evidence)

All paths verified in code on 2026-07-09 (avibe `27e2a1b5`, vault-sandbox
`82d1520`).

### Interaction cost today

"Confirm" = a click inside the sandbox iframe card. "Passkey" = a full OS
biometric/PIN prompt (`navigator.credentials.get()`); PRF-unlock and UV-confirm
prompts are indistinguishable to the user.

| Flow | Unlocked (within 10-min window) | Locked |
| --- | --- | --- |
| First-time setup | — | 2 OS prompts (`create()` + PRF `get()`), +popup & extra click on iOS |
| Explicit unlock (create form) | — | 1 passkey |
| Create protected static (#842 parent-value) | silent | blocked until unlocked (panel in form → 1 passkey), then silent |
| Create protected keypair | sandbox dialog: paste-field + Generate/Seal-pasted buttons | 1 passkey + that same dialog |
| Approve access, 1 protected secret | 1 confirm + **1 passkey** | 1 confirm + 1 passkey (unlock-on-approve) |
| Approve access, N secrets (tag/skill) | N confirms + **N passkeys** | N confirms + N passkeys (1st doubles as unlock) |
| Approve sign | 1 confirm + 1 passkey | 1 confirm + 1 passkey |
| Reveal/copy plaintext | **no UI entry point exists** | — |
| Delete protected | no passkey (#833) | no passkey |

Key code facts:

- `confirmSensitiveOperation` (sandbox `main.ts:312`) = in-card confirm click
  **plus** `confirmWithPasskeyUv` (`webauthn.ts:311`, a fresh
  `credentials.get()` with `userVerification: required`, no caching). It runs on
  the *unlocked* path of `withApprovedVmk` (`main.ts:366`) and
  `handleReleaseDek` (`main.ts:973`). The unlock window changes which prompt
  fires (UV vs PRF), never whether one fires.
- `approveAccess` (parent `vault-approval-card.tsx:224`) loops materials:
  per secret one `createVaultAgentBinding` HTTP call, then one `releaseDEK`
  RPC, each with its own confirm + passkey.
- `promptSealInput` (sandbox `operationUi.ts:164`) for `kind: "keypair"`
  renders a password input ("paste private key") as the dominant element with
  `Generate & seal` and `Seal pasted key` buttons below it. The parent form
  hides its signing-key builder entirely for protected keypairs
  (`vault-secret-form.tsx:1055` is standard-only), so the user meets this
  sandbox dialog cold, after a form that looked finished.
- Two independent 10-minute clocks: parent `useProtectedVault.ts:33`
  (`VAULT_AUTO_LOCK_MS`, own `setTimeout`, re-armed on `sealValue`) and sandbox
  `vaultLifecycle.ts:17` (re-armed on `withUnlockedVmk`). The parent reconciles
  by polling `status` after operations.
- `unseal` is fully implemented in the sandbox (confirm + passkey + in-sandbox
  display/clipboard) and wrapped in `vaultSandboxClient.ts`, but **no parent
  component calls it**.
- The sandbox confirm card for `releaseDEK` shows `grantId`, `requestId`,
  `expiresAt` — raw IDs (`main.ts:943`). The parent approval card shows command,
  egress, session link, source chips, and a humanized TTL — none of which
  crosses the RPC boundary.
- TTLs in play: sandbox/parent unlock window 10 min; grant TTL 300 s (env) /
  900 s (tag/skill), options up to 3600 s (`vault_service.py:49`); pending
  request TTL 30 min; authz challenge TTL 120 s. The approval card labels the
  grant TTL "Duration"; the lock indicator labels the unlock window with a
  countdown; nothing explains they are unrelated.
- The v1 design's anti-clickjacking geometry/visibility checks
  (IntersectionObserver, min-size, fail-closed) are **not implemented**.
- Sandbox integrity pinning is deliberately off pre-launch
  (`vaultSandboxManifest.ts`, `VAULT_SANDBOX_INTEGRITY_ENFORCED = false`);
  unchanged by this proposal, still a firm pre-launch gate.

### Why this happened

v1 (`vault-crypto-sandbox.md` §"High-risk operations…") reasoned: an in-iframe
click is clickjackable, therefore per-operation authorization must be a fresh
WebAuthn UV assertion. That conclusion was applied uniformly to `sign`,
`releaseDEK`, and `unseal`, which made the unlock window semantically empty —
it gates only silent `seal`. The window UI (countdown pill, "Lock now") shipped
anyway, promising a meaning the model doesn't deliver. That mismatch is the
root of the "unlocked but still prompted every time" experience.

## 3. Problems

- **P1 — The unlock window grants nothing.** One passkey per operation
  regardless of unlock state; N-secret approvals cost N passkeys; the countdown
  pill advertises a session that doesn't exist.
- **P2 — Protected keypair creation is wrong-shaped.** The form looks complete,
  then a second sandbox dialog leads with a paste-a-private-key field for a key
  the product should simply generate. Generate must be the zero-input default;
  import must be an explicit, secondary choice; the parent form must say what
  will happen.
- **P3 — The authorization point is context-starved.** The sandbox is the only
  unspoofable surface, yet it renders raw IDs while the spoofable parent shows
  the human story. Authorization-relevant context must reach the sandbox, and
  must reach it **signed** so a compromised parent can't rewrite it.
- **P4 — Protocol and API gaps.** No versioned op negotiation beyond a constant,
  no batch operations, no sandbox→parent events (hence the duplicate parent
  clock), no reveal entry, no policy surface, no unlock-window configuration.
- **P5 — TTL language collides.** "Unlocked · 9:12" (browser VMK window) and
  "Duration · 15 min" (agent grant) are different products of different clocks
  shown in adjacent UI with no distinction.

## 4. Design goals

- **G1 — One passkey per human intent.** A batch approval is one intent. An
  approval inside a freshly authorized session is one intent already paid for.
- **G2 — The sandbox is *the* authorization surface.** It renders
  daemon-endorsed context, in human language, for every consent it collects.
- **G3 — One clock.** The sandbox owns time; the parent renders events.
- **G4 — Explicit risk tiers.** Silent / confirm / passkey-always are product
  decisions per operation class, configurable where it matters.
- **G5 — A narrow, versioned, evented protocol** with one request envelope and
  one signed-context shape, so the next operation inherits the rules.
- **G6 — The hard invariant is untouched.** VMK, PRF output, DEKs, private
  keys, and stored plaintext never cross the boundary. (The #842 parent-value
  concession for *newly entered* static values is kept and documented: at entry
  time the value necessarily exists in the parent DOM, so sandbox entry adds no
  protection for creation. Reveal of *stored* values stays sandbox-only.)

## 5. The authorization session model

### 5.1 Unlock becomes an informed session grant

Unlocking is redefined from "VMK becomes available" to "the user grants an
**authorization session**": for the next W minutes (default 10, configurable),
operations at or below the *confirm* tier proceed with in-sandbox confirmation
only — no repeated passkey. The unlock ceremony card states exactly that:

> Unlock your vault for ~10 minutes. While unlocked, approvals ask for an
> in-sandbox confirmation instead of your passkey. Signing always requires
> your passkey.

This turns the existing countdown pill from a lie into the truth.

### 5.2 Risk tiers

| Tier | Operations | Unlocked (in session) | Locked |
| --- | --- | --- | --- |
| **R1 — self custody** | `seal` (create), `status`, `lock` | silent | first op unlocks: 1 passkey |
| **R2 — delegated read** | `approveRelease` (DEK release, incl. batch), `reveal` (display/copy) | **in-sandbox confirm, no passkey** | confirm + 1 passkey (unlock-on-approve, starts session) |
| **R3 — signing** | `sign` | confirm + **passkey UV, always** | confirm + passkey (PRF; unlocks + authorizes in one prompt) |

Sliding renewal stays: any successful R1/R2 operation re-arms the window
(sandbox-side only).

`deleteAuthzAssertion` stays outside the tiers (server-challenge-bound, no VMK,
currently unused by delete per #833).

### 5.3 Why R2 without per-op passkey is sound

What does the per-op UV prompt actually add on top of an in-sandbox confirm?

- **It does not add content binding for the user.** The OS prompt shows the RP
  name, not the operation. The user reads the operation from the sandbox card
  in both designs; UV proves presence + user verification, nothing more.
- **XSS cannot click the sandbox.** A compromised parent can *request* an R2
  operation but cannot script into the cross-origin frame; the confirm click
  must be a real user gesture inside the sandbox document.
- **The DEK release is already key-bound.** The sandbox seals DEKs only to a
  daemon-signed resident agent key verified against the verification key pinned
  in VMK-authenticated root metadata. A malicious parent cannot substitute its
  own recipient; the worst case of a tricked confirm is an early legitimate
  delivery to the legitimate agent.
- **The remaining delta is UI redress** (overlay/clickjack a *present* user).
  v1 already accepted that geometry checks bound-but-don't-eliminate this, and
  then didn't implement them. v2 makes them mandatory before any R2/R3 confirm:
  fully-visible + minimum-size + IntersectionObserver + focus, fail closed with
  `sandbox_not_visible`. A user who can be redressed into clicking a visible
  confirm card can be redressed into completing a passkey prompt too — the
  marginal defense of UV-per-op is small, and its cost broke the product.

`sign` stays R3 unconditionally: a signature can move funds irreversibly, the
per-op prompt is the last line against a redressed confirm, and v1's verified
signing-context + sandbox-derived display already assumes it. For users who
want R2 to behave like R3, a **Strict approvals** setting (§8) restores
today's behavior.

### 5.4 What the flows become

| Flow | v2 unlocked | v2 locked | Today |
| --- | --- | --- | --- |
| Create static (standard form) | silent | 1 passkey (unlock panel) | same |
| Create keypair (generate) | **silent** — form shows address preview, sandbox is invisible | 1 passkey (form unlock gate) | 1 passkey + paste-first dialog |
| Approve access, 1 secret | **1 confirm** | 1 confirm + 1 passkey | 1 confirm + 1 passkey (always) |
| Approve access, N secrets | **1 confirm (batch card lists all N)** | 1 confirm + 1 passkey | N confirms + N passkeys |
| Approve M pending requests | M confirms (or 1 with inbox batch-approve) | +1 passkey total | M×(confirm+passkey) |
| Sign | 1 confirm + 1 passkey | 1 confirm + 1 passkey | same |
| Reveal stored value | 1 confirm (+passkey in Strict) | confirm + passkey | unreachable |

## 6. Protocol v2

### 6.1 Envelope — v2 only, no compatibility

**Decision (Alex, 2026-07-09): Vaults has never shipped to real users, so no
version negotiation, no v1 compatibility, no migration work anywhere in this
plan — build the clean final state.** Same channel (`avibe.vault.crypto`),
`version: 2`; the sandbox serves only v2 and v1 ops are deleted. Deploy
ordering (sandbox deploys are instant, avibe installs lag) is handled by
operational discipline — publish the sandbox first, then the avibe release —
and only dev/regression setups can ever observe a brief mismatch.

Handshake gains `policy` in its result (see §6.5) so one round-trip tells the
parent everything it needs to render.

### 6.2 Signed operation context (the P3 fix)

The daemon already signs an agent binding for DEK release. v2 widens that
binding into the general **operation context**: everything the sandbox should
display is inside the ed25519-signed payload, verified in-sandbox against the
pinned daemon verification key (existing root-metadata mechanism, unchanged).

```ts
type SignedOperationContext = {
  v: 2;
  purpose: "agent-deliver" | "sign" | "reveal";
  requestId: string;
  grantId?: string;
  // Display block — rendered verbatim by the sandbox card because it is signed
  // by the daemon, not asserted by the parent:
  display: {
    secrets: Array<{ name: string; kind: "static" | "keypair" }>;
    sessionLabel?: string;       // "Workbench · fix-ci-flake"
    command?: string;            // truncated, sanitized server-side
    egress?: string;             // "api.github.com"
    source?: { env?: string[]; tags?: string[]; skills?: string[] };
    grantTtlSeconds?: number;    // rendered as "Agent access: 15 min"
  };
  agent?: { publicKey: AvaultPublicKey; fingerprint: string };
  expiresAt: string;
  signature: { alg: "ed25519"; keyId: string; value: string };
};
```

Rules:

- Canonical JSON for signatures and envelope hashes is UTF-8 JSON with keys
  sorted and separators `(",", ":")`; non-ASCII characters are **not** escaped
  (Python `ensure_ascii=False`, matching JS `JSON.stringify` semantics).
- The sandbox renders **only** signed `display` fields plus data it derives
  itself (decoded signing context, derived addresses, digest). Parent-supplied
  free text is never rendered in a consent card.
- For `sign`, the verified-signing-context mechanism is unchanged (sandbox
  re-derives digest from typed data, refuses mismatches); the signed context
  adds the *surrounding* story (session, request) next to the decoded payload.
- For local-only operations with no daemon round-trip (`seal`, `reveal` of a
  just-listed secret), the daemon issues the signed context on request —
  `reveal` gets a signed context naming the secret; `seal` needs none (R1).
- Backward guard: contexts carry `expiresAt` and `requestId`; the sandbox
  refuses expired or reused contexts (per-frame LRU of consumed `requestId`s),
  so a captured context can't be replayed later in the window.

### 6.3 Operations (v2 surface)

| Op | Tier | Change vs v1 |
| --- | --- | --- |
| `handshake` | — | returns `policy`; picks protocol version |
| `status` | R1 | + `policy`, + `session: { expiresAt, strict }` |
| `setup` | — | unchanged flow; PRF-on-create fast path (§7.3) |
| `unlock` | — | card copy = session grant (§5.1); returns `policy` + `expiresAt` |
| `lock` | R1 | unchanged |
| `seal` | R1 | replaces v1 `seal`; keypair = in-sandbox generate returning `{envelope, publicKey, addresses}` with **no interactive UI** (v1 `promptSealInput` paste-first card and the `sandbox-entry` input mode are removed; import cut per §7.2) |
| `approveRelease` | R2 | **new, batch-first**: `{ items: Array<{ material, context: SignedOperationContext }> }` → one confirm card listing all members → N blind boxes. Single secret = 1-item batch. Replaces v1 `releaseDEK`. |
| `sign` | R3 | v1 `sign` + signed surrounding context |
| `reveal` | R2 | v1 `unseal` renamed; parent gains a real entry point (§7.4) |
| `deleteAuthzAssertion` | — | unchanged (kept for #818-style server-verified authz) |
| `set-appearance` | — | unchanged |

Removed/forbidden: parent-triggered UV-confirm as a generic primitive (it was
the v1 shape that leaked passkeys into every path).

### 6.4 Events (sandbox → parent)

One-way notifications on the same channel, `kind: "event"`:

- `vault.state` — `{ state, expiresAt?, reason: "unlock" | "renew" | "manual-lock" | "auto-lock" | "unload" }`.
  Emitted on every transition and on renewal. **This deletes the parent's
  duplicate timer**: `useProtectedVault` keeps only mirrored state + a render
  countdown driven by `expiresAt`.
- `ui.show` / `ui.hide` — the sandbox requests/releases its modal slot; the
  parent expands/collapses the iframe + backdrop. Replaces the parent guessing
  `interactive: true` per call, and lets silent-path R1/R2 ops skip the modal
  entirely.

Cross-tab remains: BroadcastChannel lock propagation (unchanged), plus
`vault.state` per frame so every tab's UI stays honest.

### 6.5 Policy object

```ts
type VaultSessionPolicy = {
  windowSeconds: number;          // 300 | 600 | 1800, default 600
  strictApprovals: boolean;       // true = R2 behaves as R3 (today's behavior)
  parentValueSealAllowed: boolean; // #842 concession switch, default true
};
```

Stored daemon-side (vault settings), served to the parent, passed to the
sandbox at `handshake`/`unlock`. The sandbox enforces it (the parent copy is
display-only).

### 6.6 Confirm-surface hardening (prerequisite for R2)

Before any R2/R3 confirm the sandbox verifies its own presentation:
document visible + focused, frame ≥ minimum size, IntersectionObserver
reports ≥ 0.99 visible (v2 `trackVisibility` occlusion detection where the
engine supports it), no pending `ui.show` unacknowledged. Confirm buttons are
dead for the first ~500 ms after render (anti-timing-redress) and may use
hold-to-confirm for release/reveal. Otherwise fail closed with
`sandbox_not_visible` (parent responds by expanding the modal and retrying).
This was designed in v1 and never built; in v2 it is a blocker for shipping
R2-without-passkey. Note the shape of the residual: an XSS with **no user
present** can complete nothing (every R2/R3 needs a real gesture inside the
sandbox document); the residual is exclusively "user present and redressed",
bounded by these checks and by Strict mode.

## 7. Flow redesigns

### 7.1 Approvals (access)

Parent: one click (Approve) on the request card. Parent fetches **one** batch
of signed contexts (`POST /vault/agent-bindings:batch` with the request id;
daemon returns per-secret bindings sharing one display block), sends one
`approveRelease`. Sandbox: one card — title, session label, command, egress,
agent grant duration, and the full member list — one confirm (plus one passkey
iff locked or Strict). Parent then submits blind boxes via the existing
fulfill endpoint.

**Scope ruling (Alex, 2026-07-09): this single-request tag/skill batch (one
card approves every protected secret the selector covers) is a MUST for this
release.** Cross-request inbox "approve all pending" is cut — with selector
batching in place it has no remaining need.

**Approver-chosen grant duration.** The card's fixed "Duration" line becomes a
control directly above the approve button: **One-time / 5 min / 15 min**,
defaulting to the approver's remembered last choice (persisted daemon-side in
vault settings; first-ever default 5 min). One-time means the agent may
complete the current delivery and the grant then ends — no DEK cache window
(align with the existing `one_shot` grant semantics; the agent still gets a
short execution window, ~60 s, to perform that single delivery). The
per-selector fixed defaults (env 300 s / tag 900 s) are replaced by this
control; the daemon enforces the chosen value as the grant TTL and the agent
binding TTL.

### 7.2 Protected keypair creation

**Converged (Alex, 2026-07-09): one path — generate; creation lives entirely
in the parent form.** Import is cut (revisit only on a real user need). No
sandbox-entry option, no per-creation choices.

The form UI is 100% in the parent and *identical to the standard tier*: a
"Generate key" button plus the `SigningAddressList` preview. The only change
is what the button calls: `seal({ kind: "keypair" })` — the sandbox silently
generates the secp256k1 key, seals it under the VMK, and returns
`{ envelope, publicKey, addresses }`. R1 semantics: silent while unlocked; the
form's existing unlock gate covers the locked case (unchanged from today).

- The private key is **born inside the sandbox and never crosses the
  boundary**; until submit the parent holds only ciphertext + public data.
- Regenerate = call again, discard the previous envelope. Cancel = drop the
  envelope client-side; the sandbox stays stateless.
- `promptSealInput`'s interactive card is deleted entirely. During creation
  the sandbox is a headless engine; consent cards appear only at
  authorization time (release / sign / reveal). One sentence:
  *creation-time sandbox is an invisible engine; authorization-time sandbox
  is the safe's front panel.*

Rationale kept on record: relocating generation to the parent would gain
nothing (generate has no input, so there is no UX to unify) and would cost the
protected tier its strongest keypair promise — the private key's whole
lifecycle, birth included, stays out of the main app origin. Static values are
different in kind: they are user-typed, so the parent DOM sees them by nature
(G6), and parent entry is the converged single path there too.

### 7.3 First-time setup

Keep the v1 top-level/iframe split (Safari constraints are physics). Two
improvements: attempt PRF evaluation during `create()` (supported on Chromium;
skips the second OS prompt when present, falls back to the assert otherwise),
and fold the "setup, then first seal" sequence into one sandbox flow when
triggered from the create form (`setup` immediately continues into the pending
`seal` without dropping back to the parent).

### 7.4 Reveal

Secret detail (protected static) gains "Show value / Copy value" actions
calling `reveal`. R2: confirm in-sandbox, plaintext rendered inside the
sandbox frame only. This closes the orphaned-`unseal` gap with the
already-implemented sandbox surface.

**Copy-mode caveat**: the system clipboard is a shared resource — once the
sandbox writes plaintext there, the parent origin (and any XSS in it) can read
it back via a paste event or clipboard permission. Display mode is the default;
copy is an explicit second action with a visible warning, and under Strict it
keeps the per-op passkey. The clipboard exposure is identical under v1's
passkey-per-op, so this is a documentation/product-copy fix, not a regression.

### 7.5 Sandbox anti-phishing mark — DEFERRED (cut from v2 scope, 2026-07-09)

Kept on record for a later phase. An iframe has no address bar: the user
cannot verify that a card *claiming* to be the sandbox actually is one, and a
persistent parent XSS can paint a pixel-perfect fake to phish consent.
Mitigation (when revisited): a user-picked personalization mark (emoji +
phrase) stored in the **sandbox origin's** storage — cross-origin isolated,
unreadable and unpaintable by the parent; genuine cards render it, fakes
cannot; degrades safely when unset. Product decision: not now — creation input
stays in the parent (its original motivating scenario disappeared) and the
consent-card hardening can wait until real-user launch review.

## 8. TTL model and product language

One clock: the sandbox's. Parent renders `expiresAt` from `vault.state` events.

Two user-facing concepts, named apart everywhere (i18n keys, cards, docs):

- **解锁窗口 / Unlock window** — "your vault, in this browser tab". Indicator
  pill (existing) + new tooltip: "While unlocked, approvals don't re-ask for
  your passkey. Signing always does."
- **授权时长 / Agent access duration** — "what the agent receives" (grant TTL,
  5/15/60 min). Rendered only inside approval/consent cards and grant lists,
  never as a countdown pill.

Settings (Vault settings section, daemon-persisted, §6.5): unlock window
5/10/30 min (default 10); Strict approvals toggle (default off; on = today's
passkey-per-operation for R2); both enforced in-sandbox. The daemon also
remembers the approver's last grant-duration choice (§7.1) — a stored
preference, not a sandbox-enforced policy.

Unchanged: 30-min pending-request TTL, 120-s challenge TTL (internal). The
per-selector grant-TTL defaults are **replaced** by the approver-chosen
One-time / 5 min / 15 min control (§7.1).

**Grant TTL ≠ plaintext lifetime** (product copy must say this): during the
grant window the only cached materials are per-secret DEKs inside the resident
avault agent's mlock'd, zeroize-on-drop memory (never the daemon DB, never the
parent). Expiry is enforced agent-side (`purge()` on every activity + idle
timeout) *and* daemon-side; early revoke calls `avault_agent_release` and
fail-closes by resetting the whole resident agent if that release fails.
What the TTL cannot do is recall plaintext already delivered — a child
process's env, an already-sent HTTP request, an injected file live on their
own terms. Revocation guarantees "no further deliveries", not "un-deliver".

## 9. Daemon changes

1. **Batch bindings endpoint** — one call returns per-secret signed contexts
   sharing a display block (§7.1). Existing single-binding endpoint remains for
   compat during migration.
2. **Signed display context** — extend the binding builder to embed
   `display` (session label, command, egress, source, grant TTL, member list).
   Sanitize/truncate server-side; this is consent copy, not logs.
3. **Reveal contexts** — small endpoint issuing a signed `reveal` context for a
   named protected secret (no grant machinery).
4. **Vault settings** — persist `VaultSessionPolicy` plus the remembered
   grant-duration choice (§7.1); expose via existing settings surfaces.
5. **Grant duration control** — accept the approver-chosen One-time (one_shot,
   short execution window) / 300 s / 900 s value on grant creation and agent
   bindings; drop the per-selector defaults.
6. *(Deferred, separate plan)* envelope slimming: stop duplicating the full VMK
   `wrap_meta` in every record envelope (the `baseVmkWrapMeta` strip dance in
   three places is the smell); reference a vault-level wrap-meta version
   instead. Touches avault AAD compatibility — explicitly out of v2 scope.

## 10. Migration

Pre-launch, no production passkeys or protected records to preserve; sequencing
is only about keeping master shippable.

- **Phase A — protocol core**: v2 envelope + negotiation, events
  (`vault.state`, `ui.show/hide`), parent clock deletion, policy plumbing
  (static defaults first). No behavior change to ceremonies yet.
- **Phase B — authorization session**: risk tiers in the sandbox
  (`approveRelease` batch op with signed contexts, R2 confirm-only path,
  geometry/visibility hardening as its blocker), daemon batch-bindings +
  display context, parent approval card switches to one-shot batch.
- **Phase C — creation & reveal**: keypair form-embedded silent generate
  (§7.2, deletes `promptSealInput`; import cut), setup PRF-on-create fast
  path, setup+first-seal fold, reveal entry in secret detail.
- **Phase D — policy & polish**: settings UI (window length, Strict), TTL
  language sweep (i18n), remove v1 ops from the sandbox, re-pin integrity
  manifest + versioned iframe URL (existing pre-launch gate, tracked
  separately).

Each phase lands cross-repo in lockstep pairs (vault-sandbox PR + avibe PR),
same as the #845/#846 pattern.

## 11. Decisions — ALL RESOLVED (Alex, 2026-07-09). Spec is final; build the clean final state, no compatibility/migration work anywhere.

1. **Default posture** — Balanced default (R2 = confirm-only in window);
   Strict is a settings toggle.
2. **Window length** — 10 min default, sliding renewal, options 5/10/30.
3. **Reveal** — in scope, ships in Phase C (display default, copy explicit).
4. **Batch approval** — single-request tag/skill batch (one card approves all
   protected members of the selector) is a **MUST this release**; cross-request
   inbox "approve all" is cut as unnecessary once selector batching exists.
   Grant duration becomes an approver-chosen control on the card: One-time /
   5 min / 15 min, remembered last choice (§7.1).
5. **Per-secret strictness** — not built; global Strict covers the need.
6. **Creation** — parent-form-only for both static and keypair; single path,
   no per-creation choices; keypair import cut; anti-phishing mark deferred
   (§7.5). Keypair key material remains sandbox-born as an implementation
   detail with zero UX surface (§7.2).

Implementation split (Alex): frontend UI/UX & product-experience work →
Claude; daemon/backend & cryptography-sensitive work (incl. the sandbox) →
Codex. Orchestrated from the design session; each implementation agent owns
its PR review loop; the orchestrator reviews and merges last.
