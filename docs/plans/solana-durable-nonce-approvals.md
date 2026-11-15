# Solana approvals beyond recent-blockhash expiry — 3.2.7

**2026-11-14 · 产品伙伴 selected A + B; C deferred.**

## Recommended scope

Ship **A: guidance + recipe (4–6 engineer-hours)**; optionally **B: conditional advisory (+3–5h)**. Defer **C: nonce-aware classification (+8–12h)**. No policy, custody, request-expiry or callback changes. Avibe never holds SOL, creates accounts, broadcasts, or refreshes approved bytes. Applies to standard `always_ask` (including HD accounts) and protected Solana transactions; headless standard stays unchanged.

## A — Agent workflow

1. Inspect protection and selected account's `always_ask`. When approval is required or delay uncertain, build with a durable nonce **before** requesting signatures.
2. Through caller-owned Solana tooling and authorized funds, the Agent creates/initializes a System-owned nonce account and funds its RPC-reported rent-exempt minimum; budget fees separately. Use a Vault Solana signer as authority. A seeded account (`CreateAccountWithSeed` + `InitializeNonceAccount`, Vault signer as base) avoids a new account keypair; never export wallet keys. Check existing account identity/state before reuse. **Bootstrap itself needs signing:** without an existing nonce, arrange prompt approval/submission or owner provisioning; never lower approval policy. This design authorizes no spending.
3. Fetch the account on the intended cluster; verify authority. Put its stored nonce in `recent_blockhash`; put exact System `AdvanceNonceAccount` at instruction **0**, before transfers/compute-budget instructions. Include authority, payer and every required signer. Provide a nonce-aware SDK recipe using legacy messages; do not silently rewrite third-party prebuilt transactions.
4. Serialize once; pass exact message hex to Vault, with matching `solana-transaction-v1` context for protected signing. After approval, retrieve the existing request's signature, assemble and submit **those same bytes**. Never rerun pending `sign` or replace its blockhash.
5. Keep one outstanding operation per nonce account. Recheck nonce/authority before submission; account changes or consumption invalidate it. Reconcile signature status/nonce state after uncertain broadcast before preparing another payment. Execution failure can consume nonce and fees. New bytes require fresh approval. Nonce validity does not extend Avibe request expiry.

**Surfaces:** `skills/use-avibe-vault/SKILL.md`; `vibe/cli.py` sign help and list/find Solana note; EN/ZH CLI and public Vault docs. Help: “For Solana transactions requiring approval, use a durable nonce; a recent blockhash may expire while approval is pending.” Correct “standard signs inline”: standard can require approval too.

## Optional Avibe help

**B:** Agent CLI/API: “If this message uses a recent blockhash, it may expire while awaiting approval. Use a durable nonce for delayed submission.” `request.solana_lifetime_hint` is an English read-projection field, never persisted/signed. Approver UI EN: “Solana transactions are usually valid for only a minute or two. If you approve later, this one may have expired and will need to be requested again. Transactions that use a durable nonce are not affected.” ZH: “Solana 交易通常只在一两分钟内有效。批准晚了，这笔交易可能已经过期，需要重新发起；使用持久 nonce 的交易不受此限。” Product approved these reader-specific strings. Only `sign` + `ed25519` + `pending`, any payload/tier; hide on settlement. No parsing, countdown or extra confirmation; preserve trusted sandbox review. Official Solana confirmation guidance describes 151 hashes / typically 60–90s; commitment can reduce remaining time, so the UI estimate is not a guarantee.

**C:** Targeted hints need a bounded legacy/v0 classifier; `storage/vault_solana.py` currently deliberately treats messages as opaque. Recognize only exact first-instruction nonce advance with resolvable accounts. Unknown/malformed/unresolved means **unknown**, not safe or rejected. No RPC or caller-supplied nonce flag. Recognition proves structure, not live validity; this is larger than copy.

## Tests and delivery

A: help assertions; offline recipe fixtures for ordering, exact bytes, signer coverage/context equality, seeded bootstrap and old-signature rejection after nonce changes. **No live delayed-submission run; Product explicitly does not require a validator for merge.** B: pending CLI/API paths including policy-race handoff, both tiers/HD, EN/ZH card and unchanged continuation/signatures; repository-native UI frame review. C adds adversarial legacy/v0/ALT/truncation fixtures. No production funds. Hours exclude review/CI waits.

**Basis:** `vault-signing-policy.md`, `vault-solana.md`, Vault Skill/current implementation; official Solana “Durable Nonces” documentation checked 2026-11-14 (also flags possible future deprecation, not a replacement for this slice).
