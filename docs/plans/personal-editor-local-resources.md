# Spec: Personal Editor local-resource access — binding state machine rewrite

Status: APPROVED (owner signed off D1/D2 defaults + D3 naming). Supersedes closed PR #1562.
Branch: `personal-editor-local-resources`
PR title: `fix(auth): personal editors use local resources (binding rewrite)`

## 1. Background — why we're rewriting

PR #1562 added the first cross-process authorization signal (`instance_kind`) but never
wrote down the concurrency model for it. Five bot-review rounds later the thread count went
7 → 9 → 11 → 15 → 19: every patch inlined a partial answer, and the last two rounds produced
self-inflicted regressions (a gate that permanently locks out legacy no-kind pairings; a lock
acquired before SQLite init that creates an ABBA deadlock).

Root cause (one sentence): **durable binding state was added, but "who is the single writer,
what is the lock order, and which writes must be generation-fenced" was never defined as a
contract — so transient state keeps getting latched into permanent decisions across two
processes (controller + ui_server).**

Correct parts of the current work ARE kept (see §4). Only the binding state machine + the
authorization fencing is rewritten.

## 2. Goal

A Personal Editor gets full Agent/Project access on a Personal instance, independent of
Organization ACLs; Organization + unknown kinds stay fail-closed. This must hold:
- across the two-process boundary (controller / ui_server), and
- through every transient state (credentials temporarily missing, kind not yet backfilled,
  kind being reclassified by heartbeat, fresh install with no config, legacy no-kind pairing).

## 3. The three contracts (approved by owner)

### C1 — Single source of truth
A durable binding = `(instance_id, kind, generation)`, persisted in `state_meta`.
- Only a binding in `ready` state may admit the kind-specific (Personal) bypass.
- `reconciling` / `unpaired` / `unavailable` states FAIL CLOSED (no bypass).
- Kind is authoritative mutable provenance: unknown ≠ Personal ≠ Organization; a kind
  backfill or reclassification must go through a transition, never be silently relabeled.

### C2 — One writer + canonical lock order + generation CAS
- `generation` is monotonic and durable; every write (config, binding, authorization rows)
  does compare-and-swap: read the persisted generation inside the critical section
  immediately before writing; refuse if it no longer matches.
- Canonical lock order: **SQLite initialization FIRST, then the cross-process config lock**
  (`config_file_lock`, NOT the process-local `CONFIG_LOCK`). Never acquire config lock while
  `ensure_sqlite_state()` / `migration.lock` may be needed — this kills the ABBA deadlock.
- The whole read-generation → compare → config-write → transition is ONE cross-process
  critical section.

### C3 — One gate for every consumer
A single predicate `binding_is_ready(config, identity)` gates ALL authorization reads and
writes before they touch instance-kind logic:
1. interactive refresh (`_fetch_authorization_context`),
2. non-interactive metadata (`resource_user_context_from_metadata`),
3. Web Push authorization,
4. OAuth callback writes,
5. legacy inline-cookie migration writes.
No consumer reads `config.instance_kind` directly to make an authorization decision.

## 4. Scope — carried over vs rewritten

### Carried over verbatim (correct, concurrency-independent — do NOT rework)
- deferred-context migration + snapshot/provenance logic (`storage/resource_access_service.py`):
  the typed UNAVAILABLE/UNPAIRED/PARTIAL/READY reader, migration state machine, old-marker
  compatibility, sealed_unattributed.
- the 435 passing tests (they are the executable spec of all known invariants).

### Rewritten (the module that keeps leaking)
`vibe/remote_access.py` binding machinery + every authorization write/read gate:
- `_transition_instance_binding` → single cross-process critical section (C2),
- `_AUTHORIZATION_BINDING_EPOCH` (process-local) → removed in favor of durable generation (C1/C2),
- `_durable_binding_allows_cached_authorization` → replaced by the single `binding_is_ready` gate (C3),
- `pair()` ordering: provenance validated BEFORE one-time-key redeem (fail without redeem on
  unavailable provenance; treat **absent config as authoritative `unpaired`, not `UNAVAILABLE`**),
- `_fetch_authorization_context` + `_store_scoped_authorization` + revocation/denied path →
  all writes generation-fenced, including the 403 path (C3),
- non-interactive + Web Push + OAuth callback + legacy cookie paths → route through the gate (C3),
- legacy no-kind pairing stays usable (fail-open for the legacy path, not permanently locked out).

## 5. Files

- `vibe/remote_access.py` — binding state machine, transition hook, all gated write/read sites.
- `storage/remote_access_authorization_service.py` — durable generation storage + targeted
  invalidation/revocation (keep the `show_page` exemption contract).
- `storage/resource_access_service.py` — carried over, only touch if a gate needs the typed
  reader's result.
- `config/v2_config.py` — confirm `config_file_lock` is the single cross-process lock (no change
  expected; do not add locks).

## 6. Tests

Carried: all 435. New (each maps to a regression this PR actually hit):
1. legacy/invalid no-kind pairing stays usable (round-5 #1) — fail-open for legacy, not locked out.
2. fresh install with NO config → authoritative `unpaired`, pair() redeems successfully (round-5 #4).
3. lock order: binding transition does NOT acquire config lock before SQLite init (round-5 #2).
4. reconciling window: an OAuth-callback / legacy-cookie write is generation-fenced and cannot
   publish stale Editor claims under the new Personal kind (round-5 #3).
5. stale 403/revocation is fenced by generation (round-4 #4).
6. cross-process CAS: a stale writer cannot reverse a newer binding (round-4 #1, #2).
7. non-interactive + Web Push consumers return no Personal bypass while `reconciling`.

## 7. Delivery plan

1. (this doc) — owner approves.
2. New branch off latest `origin/master` (`84702f73`), fresh worktree (NOT the old #1562 worktree).
3. Carry over §4 "kept" code + 435 tests; rewrite §4 "rewritten" module per C1–C3.
4. Open a NEW PR (correct scope + evidence numbers); commit this spec to `docs/plans/`.
5. Close PR #1562 with a comment pointing to the new PR.
6. Re-point the durable watch from #1562 to the new PR; retire #1562's watch.

## 8. Open owner decisions (need your call before/at implementation)

- D1: keep the three prior decisions? (a) Org↔Personal reclassification keeps completed legacy
  authorizations REJECTED; (b) exact `show_page_email` grants survive kind transitions; (c)
  config-persist-OK but SQLite-reconcile-fails → durable `reconciling` + fail closed. — assumed YES.
- D2: legacy no-kind pairing (round-5 #1) — confirm we keep it usable via a legacy path (fail-open),
  rather than forcing these pairings to re-pair. — assumed YES, confirm.
- D3: PR/branch naming — propose branch `personal-editor-local-resources`, PR title
  "fix(auth): personal editors use local resources (binding rewrite)". — confirm or rename.
