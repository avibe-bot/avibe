# Memory agent-owner split — regression close-out and acceptance

> **Safety boundary:** Run the live portion of this checklist only in the local
> Incus regression environment after the owner explicitly authorizes a regression
> update. Keep the long-lived `master` target online and preserve its existing
> product state. Never use `--remote`, `--reset-config`, or `--reset-all`, and
> never overwrite Avibe Cloud pairing. Do not restart the host `vibe` service; the
> runner manages the in-container `avibe-regression.service` only. After any
> authorized source update, verify service health before recording a result. This
> document is scrubbed: it contains no tokens, no full logs, and no raw
> principal/owner IDs, session IDs, or private channel IDs. Every example value is
> synthetic.

This is the PR C close-out for the
[`memory-agent-owner-split`](../plans/memory-agent-owner-split.md) plan. It
validates that agent-initiated memories are routed to a separate EverOS *user*
owner (Scheme 2), that both owners are searchable with visible origin labels, and
that the user owner's profile is never polluted by agent writes.

- Depends on: PR #1633 (read fan-out + labeled surfaces, PR A) and PR #1642
  (write path, PR B), both merged into `dev`.
- Affected scenario IDs: `MEMORY-SEARCH-008` … `MEMORY-SEARCH-016`
  (`tests/scenarios/memory_search/catalog.yaml`).

## 1. Verification layers

| Layer | What it proves | Status |
| --- | --- | --- |
| Unit / contract | Owner derivation, session disjointness, `ProviderSessionRef` round-trip, provider payload owner routing, fan-out merge/dedupe/partial, recovery closure, terminal-flush fan-out, diagnostics owner expansion, cross-caller isolation | **PASS (run in this worktree)** |
| Scenario | `MEMORY-SEARCH-008..016` mapped tests | **PASS (run in this worktree)** |
| Incus live artifact | End-to-end `vibe memory remember` (`provenance=agent`) → distilled → labeled dual-owner search + split profile + assistant-owner processing record, against a deployed build | **OWNER-DEFERRED** — residual manual acceptance, not a merge blocker (see §4) |
| Manual four-platform | Slack / Discord / Feishu(Lark) / WeChat surfacing of labeled memory | **OWNER-DEFERRED** — residual manual acceptance (see §6) |

The live Incus pass and the four-platform manual checks are a **residual manual
acceptance item that the owner explicitly deferred for this delivery** (owner
decision, 2026-08-22). They are neither a claimed result nor a merge/release
blocker: the code paths ship on the strength of the complete automated layers
below, and the live pass is a post-merge confirmation the owner may run at any
time using the safe runbook in §5. It is recorded here as deferred — not as a
completed test — so no unearned execution evidence is implied.

## 2. Environment and source identity

Captured on the close-out host at close-out time:

- Source under test: `dev` head `9b55b1d2e` — `feat(memory): route agent captures
  to assistant owners (#1642)`. PR C branch `test/memory-agent-owner-regression`
  is even with this head; it adds only this document and the plan pointer.
- Local Incus daemon: reachable via
  `INCUS_CMD="limactl shell avibe-incus-regression -- sudo incus"`; client/server
  `6.0.0` (macOS Lima VM `avibe-incus-regression`, Running). Direct macOS `incus`
  is not available — the `INCUS_CMD` prefix is mandatory.
- `master` regression environment: **absent**. `status --target master` reports
  `Project not found`; `reconcile` reports no worktree regression environments.
  There is therefore no accumulated product state to preserve, and creating a
  fresh local `master` is required for the live pass.
- Base image: the daemon holds only a cached upstream Ubuntu image; the runner's
  expected alias `avibe-regression-base-current` is **not** built. A live pass must
  run `build-base` before `up` (§5, step 0).

## 3. Security boundary (what this close-out did and did not touch)

- No secret was read, printed, requested in plaintext, or committed. The Vault was
  checked (`vibe vault list`/`tags`) and is empty; no ambient LLM or platform
  credential is exported in this environment.
- The automated evidence in §7 runs entirely against stubbed sidecars and
  test-owned temporary state. It never reaches the real host `~/.avibe`, external
  EverOS, or any model endpoint.
- The only local filesystem artifact created outside the repo tree is a gitignored
  `ui/dist/` placeholder needed to build the editable package into the test venv;
  it is confirmed ignored by `git check-ignore ui/dist` and touches no tracked
  file or product state.
- The host `vibe` service was not restarted. No Incus environment was created,
  reset, or deleted. No regression seed credential was read, requested in
  plaintext, or committed.

## 4. Owner-deferred: live Incus + four-platform manual acceptance

**Owner decision (2026-08-22): the live Incus regression and the four-platform
manual checks are deferred for this delivery.** PR C ships the acceptance record
and plan close-out now and runs the normal review loop; the live pass is a
post-merge confirmation the owner may run when convenient. This is a residual
manual acceptance item, **not** a merge or release blocker, and it is **not** a
completed test — no live execution evidence is claimed.

Why this is safe to defer:

- **The automated layers already cover every acceptance invariant except live
  EverOS distillation** (§7): 9/9 scenario tests `MEMORY-SEARCH-008..016` plus the
  638-test canonical memory suite pass on the exact `dev` head under test. Owner
  derivation, session disjointness, provider payload owner routing, fan-out
  merge/dedupe/partial-failure, recovery closure, terminal-flush fan-out,
  diagnostics owner expansion, and cross-caller isolation are all proven without a
  live environment.
- **The only invariant the live pass adds is the real-LLM distillation leg of
  invariant 9** — an Episode/Facts/Profile update under the assistant owner and
  recall returning the distilled fact with an agent-origin label. Distillation
  calls a real model, so it cannot be exercised with placeholder keys; that is why
  it belongs in a live pass rather than the automated suite.
- **No live environment exists to preserve.** As recorded in §2, there is no
  `master` regression environment and no accumulated product state; deferring
  leaves nothing stale.

When the owner chooses to run the deferred pass, §5 (memory-only, needs only real
`ANTHROPIC_API_KEY` + `OPENAI_API_KEY` in `.env.regression`) and §6 (four-platform,
needs real bound platform tokens) are the validated, safe runbook. Per project
policy those credentials are supplied by the owner in `.env.regression` and are
never requested in plaintext or committed.

## 5. Live Incus runbook (owner-deferred; safe to run post-merge, ~10 minutes after `up`)

This is the validated, safe runbook for the deferred live pass (§4). Run every
command with the `INCUS_CMD` prefix. It is validated against
`up --target master --dry-run`; the only input the owner supplies is real seed
credentials in `.env.regression`.

**Step 0 — prerequisites (one-time).**

```bash
export INCUS_CMD="limactl shell avibe-incus-regression -- sudo incus"
# Provide real LLM keys in .env.regression at the repo root, then:
python3 scripts/incus_regression.py doctor
python3 scripts/incus_regression.py build-base        # alias avibe-regression-base-current
python3 scripts/incus_regression.py up --target master
```

Expected: the runner prints one local UI URL (default `http://127.0.0.1:15130`),
project `avr-master`, instance `avibe-master`, runs `vibe runtime prepare
--strict`, and reports the service healthy. If `up` might outlive a turn when an
agent drives it, manage it with a Harness Watch on the runner command — never a
detached `&`/`nohup`.

**Health gate.** In the Web UI, open **Memory → Processing Record** and confirm
**Engine status: Healthy** and **Call log: Recording normally** before sending any
capture. If either is not true, record `INCONCLUSIVE` and stop.

The rest of the runbook exercises the split. Each step names the acceptance
invariant it covers and the visible pass condition. Use a unique, non-sensitive run
tag such as `MAOS-YYYYMMDD-NN` in every seeded value.

1. **Seed a user-owner control (invariant 1, 8; guards against a false negative on
   an empty environment).** From an ordinary human DM in any bound platform (or the
   Web chat), send a plain user turn containing a unique fact, e.g.
   `<run-tag> control the user's favorite lake is Placid`. Wait for it to appear in
   **Processing Record** under one principal. This is the user-owner baseline.

2. **Agent capture via the trusted internal path (invariant 1, 6).** Through a real
   agent session for the same principal, have the agent run
   `vibe memory remember "<run-tag> the user plans a release on the 23rd"`. The CLI
   returns the localized **"Memory queued."** confirmation — this is the accepted /
   enqueued signal for a `provenance=agent` capture. Then reach a supported terminal
   boundary for that session (end the session normally) so the terminal flush
   fan-out distills the assistant-owner buffer.

3. **Dual-owner search with origin labels (invariant 4, 9).** Run
   `vibe memory search "<run-tag> release"` (human output, no `--json`). Pass
   conditions:
   - the agent-recorded fact is returned, prefixed **`[Agent memory]`**;
   - the step-1 user fact, when it matches, is prefixed **`[User memory]`**;
   - an exact-duplicate across both owners collapses to a single
     **`[User + Agent]`** row (do not force this; it is only asserted when it
     occurs). In **Memory → Search** the same rows carry the localized
     `User memory` / `Agent memory` / `User + Agent` labels.

4. **Profile stays split, never interleaved (invariant 4).** Open **Memory →
   Profile** (or `vibe memory profile`). Pass condition: the real user's profile
   and the agent's profile render as two separately labeled blocks; the user block
   shows no content derived from the step-2 agent capture.

5. **Processing diagnostics retain the assistant-owner record (invariant 7).** In
   **Memory → Processing Record**, under the same caller's scoped view, find a
   processing entry for the step-2 capture. Pass condition: the assistant-owned
   capture is visible in the caller's scoped diagnostics (and in the admin view);
   it is not silently absent. No `AgentCase` is expected for a single fact — an
   assistant-owner cell with no assistant sender is the accepted design (§ plan
   4/5).

6. **Cross-user isolation (invariant 8).** From a second distinct principal, repeat
   step 2 with a different run tag, then run step-3 search as the first principal.
   Pass condition: the first principal never sees the second principal's
   agent-recorded fact. No client can pass an arbitrary owner ID — the owner is
   always derived server-side from the trusted principal.

7. **Historical / partial compatibility (invariants 3; smoke only).** Confirmed by
   the automated recovery and partial-warning tests (§7); no manual step is
   required. Do not hand-edit real on-disk state to force these paths.

Record `PASS` / `FAIL` / `INCONCLUSIVE` per step. An origin label that is missing,
wrong, or that interleaves the profile blocks is `FAIL`. A step whose Processing
Record evidence never becomes observable within the operator stop threshold (use
the same 30-minute scale as the existing attachment runbook) is `INCONCLUSIVE`, not
`FAIL` — a shared preserved queue can delay without proving a defect.

## 6. Four-platform manual items (owner-deferred)

These are part of the deferred residual acceptance (§4). They require real bound
platform accounts and cannot be exercised without platform credentials in
`.env.regression`. They are surfacing checks, not new logic: the labeled-rendering
behavior is shared core and identical across platforms, and is already proven at
the automated layer (§7).

- **Slack / Discord / Feishu(Lark) / WeChat:** from an ordinary human DM on each
  bound platform, drive one agent `remember` (step 2) and one dual-owner search
  (step 3), and confirm the returned/injected memory keeps its origin label and
  that the platform formatter does not strip it. WeChat and Lark: verify the label
  survives their formatter path specifically.
- The Web UI **Memory → Search / Profile** panels are the canonical labeled
  surface; the four IM checks only confirm no platform formatter regresses the
  label. If platform credentials are unavailable, record each platform as
  `BLOCKED (no credential)` — this is expected and does not fail the close-out.

## 7. Automated evidence (run in this worktree, `dev` head `9b55b1d2e`)

Reproduce with `uv run --frozen pytest -q <targets>` from the worktree root (a
gitignored `ui/dist/` placeholder is required for the editable build).

**Scenario mapping — `MEMORY-SEARCH-008..016`, all PASS (9/9):**

| ID | Test |
| --- | --- |
| 008 | `test_memory_store.py::test_assistant_owner_derivation_and_read_session_refs_are_stable_and_disjoint` |
| 009 | `test_memory_module.py::test_dual_owner_search_is_concurrent_and_merges_score_dedupe_and_origins` |
| 010 | `test_memory_module.py::test_agentic_recall_interleaves_per_leg_rank_with_only_one_agentic_leg` |
| 011 | `test_memory_everos.py::test_add_payload_owner_matches_provenance_routed_session` |
| 012 | `test_memory_store.py::test_owner_scoped_session_recovery_returns_trusted_caller_scope_after_reopen` |
| 013 | `test_memory_module.py::test_terminal_flush_closes_user_and_agent_sessions_under_one_deadline` |
| 014 | `test_memory_insight.py::test_assistant_owned_capture_is_visible_in_scoped_and_admin_diagnostics` |
| 015 | `test_memory_store.py::test_capture_provenance_routes_provider_owner_without_changing_caller_audit_scope` |
| 016 | `test_memory_module.py::test_agent_remember_round_trips_through_dual_owner_search` |

**Canonical memory suite — PASS (638 passed, 5 unrelated SAWarnings):**
`tests/test_memory_admission.py`, `tests/test_memory_store.py`,
`tests/test_memory_module.py`, `tests/test_memory_everos.py`,
`tests/test_memory_insight.py`, `tests/test_internal_server.py`,
`tests/test_memory_cli.py`, `tests/test_memory_project_ids.py`,
`tests/test_memory_sidecar.py`.

These cover every acceptance invariant except the live-distillation portion of
invariant 9, which is exactly what the §5 runbook is for.

## 8. Known limitations and rollback

- **`list_episodes` stays user-owner-only** (plan §6, documented limitation);
  assistant-owner episode listing is a follow-up, not a regression.
- **No historical migration** (plan §8): pre-split agent memories physically live
  under the user owner and surface labeled as user memory. Nothing reclassifies
  them.
- **No per-agent owners** in this iteration; one `-agent` owner per principal.
- **No paraphrase-level dedupe**; only normalized exact-match collapse to
  `User + Agent`.
- **Rollback exposure (plan §4):** downgrading past PR B keeps already-*delivered*
  assistant memories under `u-…-agent` (invisible to old code until re-upgrade),
  but assistant rows still *pending* in the queue at rollback are dead-lettered by
  the old sidecar's 403 path — a bounded, accepted degradation, not a crash. New
  *user* rows are byte-identical to pre-split rows and roll back cleanly.
- **AgentCase warning:** assistant-owner cells log a per-cell
  `agent_case_skipped_no_assistant` warning by design; it is not an error.

## 9. Failure log locations

If a live step fails, collect (scrub before sharing — no tokens, no raw IDs):

- Runner output from the `up`/`build-base`/`status` command.
- In-container service log:
  `python3 scripts/incus_regression.py logs --target master`, or inside the
  instance `/home/avibe/.avibe/logs/vibe_remote.log`.
- Memory engine state: **Memory → Processing Record** (Engine status, Call log),
  and the sidecar state under the in-container `/home/avibe/.avibe/state/`.
- For an automated-layer failure: the failing `pytest` node ID and its assertion.

## 10. Acceptance checklist

| # | Item | State |
| --- | --- | --- |
| 1 | PR A #1633 (read fan-out + labeled surfaces) merged to `dev` | ✅ Done |
| 2 | PR B #1642 (write path → assistant owner) merged to `dev` | ✅ Done |
| 3 | Scenario tests `MEMORY-SEARCH-008..016` pass on the exact head | ✅ 9/9 (§7) |
| 4 | Canonical memory suite passes on the exact head | ✅ 638 passed (§7) |
| 5 | Security boundary held: no secret read/printed/committed, no env mutated | ✅ (§3) |
| 6 | Acceptance record + safe live runbook documented | ✅ (this doc §5–§6) |
| 7 | Live Incus end-to-end distillation pass | ⏸ Owner-deferred (§4) |
| 8 | Four-platform manual surfacing pass | ⏸ Owner-deferred (§4, §6) |

Items 7–8 are the residual manual acceptance the owner deferred for this delivery
(§4). They are not merge/release blockers and carry no execution evidence yet.

## 11. Result record (to be completed if/when the deferred live pass runs)

Record the run tag, source commit, service-health result, the per-step
`PASS`/`FAIL`/`INCONCLUSIVE` outcomes for §5 steps 1–7, and the §6 per-platform
outcomes (including any `BLOCKED (no credential)` reason) in the owning PR or
acceptance report. Do not paste raw payloads, credentials, raw owner/principal IDs,
or unsanitized logs.

Close-out status at authoring time: **automated layers PASS (9/9 scenario + 638
canonical); live Incus + four-platform manual layers OWNER-DEFERRED (2026-08-22),
documented as a safe post-merge runbook — not a merge blocker and not a completed
test.**
