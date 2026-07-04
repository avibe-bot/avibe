# Vault requests in chat — cards, unified dialogs, auto-resume

## Background

A vault request that needs the human — **provision** (fill a not-yet-stored secret),
**access** (approve an agent using a protected secret), **sign** (approve a signature) —
currently surfaces to the user in two thin ways:

- **Vaults page** (`VaultsPage` → `PendingRequestsSection`): lists pending requests and
  opens the approval / provide dialog. Since #765 this is **SSE-driven** (`vaults.updated`
  event via `connectWorkbenchEvents`; the 5s poll is only a fallback when the event bridge
  is down).
- **`$<NAME>` inline card** (`markdown.tsx` → `SecretRequestCard`): if an **agent reply**
  literally contains the marker `$<UPPER_NAME>` (regex `\$<([A-Z][A-Z0-9_]*)>`, outside code,
  gated on `secretRequests={isAgent}`), it renders an inline "provide" card. `reply_enhancer`
  only *parses* the marker — nothing *instructs or injects* it, and it is **provision-only**.
  In practice it rarely fires (the model isn't told to emit it), and access/sign have no chat
  card at all.

Agent-side, a request either **blocks** the turn (`--wait`, model idles until timeout) or
returns and the agent must **poll/re-run** (`vibe vault await`) — i.e. hand-roll its own wait.

Two dialogs also drifted visually: the `$<NAME>` card and the Vaults "Add secret" dialog both
wrap the same `VaultSecretForm`, but with **different headers** (custom cyan-key header vs a
plain `DialogHeader` title), so they read as two different things.

## Goal

1. Push an **interactive card into the chat** when a request is created — not just the Vaults
   page — matching Avibe's IM-first "colleague" model.
2. **Unify the dialogs** so every entry point opens the *same* add/provide dialog and the
   *same* approval dialog (reuse + visual consistency).
3. Make the agent flow **friendly**: the agent doesn't block or hand-roll a watch — request
   resolution **auto-resumes** its session via the *same* callback entry Agent Run uses.

## Constraints

- **Browser-only crypto.** Filling / approving / signing needs browser-side sealing, DEK
  release, and browser-signs-protected. So the actionable dialog runs in the **web** UI only.
  On pure IM (Slack/Discord/…), the card degrades to a **notification + deep link** to the web
  approval — never an inline approve.
- Preserve the cardinal invariant: only public material (signature / avault-bound DEK blind
  box) ever reaches the daemon.

## Design

### Interaction model (user's refinement)

- **Provision (add-secret)** → **card A only** (inline, in the timeline). No strong reminder —
  the user may choose not to add it.
- **Approval (access / sign)** → **card A** inline, **plus a conditional floating bar B**:
  when a pending approval card is **not in the viewport** (scrolled past, above *or* below),
  float B above the composer. When the request **expires**, the A card collapses to an
  "expired" state and B auto-dismisses. Multiple pending → B shows a count.
- **Click B** → open the approval dialog directly (fastest path); with several pending, act on
  the oldest.
- Visibility is tracked with an `IntersectionObserver` on the inline A card(s); B is derived
  from "any unresolved approval whose card is off-screen".

### Dialog unification

Two shared components under `ui/src/components/ui/`, one consistent header style (icon tile +
title + subtitle, design.pen `F4N19`/`vyed5`):

1. **`VaultSecretDialog`** — Dialog wrapping `VaultSecretForm` (create *and* provide modes).
   Callers: Vaults "Add", Vaults pending-provision (`PendingRequestsSection`), the `$<NAME>`
   chat card (`SecretRequestCard`), and the new chat provision card.
2. **`VaultApprovalDialog`** — Dialog wrapping `VaultApprovalCard`. Callers: Vaults "Review",
   the new chat approval card, and the floating bar B.

`SecretRequestCard` and `VaultsPage`'s inline `AddSecretDialog` / review `Dialog` collapse into
these two.

### Chat cards

- A small `VaultRequestCard` (Form A) rendered in the chat transcript, keyed by request
  `session_id` == current session, fed by the existing `vaults.updated` SSE (fetch pending for
  the session on the event; no new backend event needed for v1). Variants: access (gold lock),
  sign (violet pen + scheme-relevant address via `SigningAddressList`), provision (accent key).
  Resolved → collapses to a quiet "✓ approved / ✕ denied / ⏱ expired" line.
- `VaultFloatingApproval` (Form B): sticky bar above the composer, approval-only, shown when an
  unresolved approval card is off-screen; auto-hidden on expiry.

### Auto-resume (callback) — unified entry

On request resolution (approved / provided / denied / expired), the daemon enqueues a **callback
turn** to the request's `session_id` through the **same** path Agent Run / watch / scheduled
tasks use: a `session_turns` spec with `callback_session_id` + `source_kind="callback"`
(`core/session_turns.py`). The agent's turn ends when it makes the request; it is woken with the
outcome. No agent-side `--wait` block and no hand-rolled watch. A `--no-callback`-style opt-out
mirrors Agent Run.

## Phases

- **P1 — Dialog unification** (frontend, no behavior change): extract `VaultSecretDialog` +
  `VaultApprovalDialog`; rewire Vaults page + `SecretRequestCard`. Shippable on its own.
- **P2 — Chat cards (Form A)**: `VaultRequestCard` in the transcript, SSE-driven, session-scoped,
  opening the P1 dialogs; access/sign/provision variants + resolved states.
- **P3 — Floating bar (Form B)**: approval-only, off-viewport via IntersectionObserver,
  expiry auto-dismiss.
- **P4 — Auto-resume callback** (backend): enqueue a callback turn on resolution via the
  session_turns callback entry; opt-out flag; agent-facing CLI copy updated to stop telling the
  agent to poll/visit the Vaults page.
- **P5 — IM degrade**: request notification + deep link on IM platforms.

## Evidence plan

- Unit: dialog components render both modes; `VaultRequestCard` state machine (pending →
  resolved/expired); callback-turn enqueue on resolution (backend test alongside existing
  `tests/test_vault_*`).
- Contract: request resolution emits exactly one callback turn to the right session; no
  duplicate on re-resolve.
- Manual: web chat card → dialog → approve → agent auto-resumes; scroll → B floats → expiry
  dismisses; IM deep-link.
