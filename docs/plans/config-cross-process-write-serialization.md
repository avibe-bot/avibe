# Plan: Config Write Safety — Patch Writes (#1458)

Status: ratified (owner decision 2026-08-15)
Tracker: GitHub issue #1458 (filed from PR #1455 review, discussion_r3788721089)
Supersedes: the draft version of this document referenced by PR #1465
(which existed only as an untracked file in the primary checkout — that
dangling reference is repaired by landing this document).

## Problem

`config.json` has full-snapshot read-modify-write writers in two
long-lived processes (UI API server, controller) plus potential
short-lived CLI processes. `CONFIG_LOCK` is process-local, so:

```
t0  UI:  load  → snapshot_UI        (contains old codex.auth_mode)
t1  CTL: load  → snapshot_CTL       (contains old slack token)
t2  CTL: mutate + save              ✅
t3  UI:  mutate + save              ← writes snapshot_UI: reverts CTL's t2 fields
```

Writes are already serialized (the Memory transaction flock covers each
`save()`); the defect is **lost updates** — non-Memory fields are written
from snapshots loaded outside the lock. Whole-file READ is fine (startup
and decisions need a consistent view); **whole-file WRITE is the defect**.

## Root-cause framing (owner decision)

The fix is a dataflow-shape correction, not locking or discipline:

> Writers must declare **patches** ("set these fields"), never
> **snapshot overwrites** ("here is my new version of the world").

A patch composes with concurrent changes (given one serialization of the
read-merge-write cycle); a snapshot overwrite silently reverts them.

## Options weighed

| Option | Verdict |
| --- | --- |
| **Patch writes** — typed mutators (`update_config_fields`) | ✅ **Ratified.** The fix. `save_config` is NOT yet patch-shaped: its base snapshot loads outside the file lock and several callers pass full snapshots — its read-merge-write cycle moves to stage ③ below. |
| Cross-process flock around whole RMW (generalize Memory transaction) | ✅ Landed as the serialization substrate (#1465); not sufficient alone — still needs patch-shaped writers. |
| Single-writer via controller IPC (declarative patches over the dispatch socket) | ⏸️ Parked. Removes the need for cross-process discipline AND would simplify the controller-memory reconciliation dance, but patch semantics + flock already eliminate the race; revisit when reconciliation pain grows. |
| Split `config.json` into per-section files | ❌ Rejected. Shipped-surface migration for a blast-radius reduction only — same-section races (codex auth vs marker, the confirmed conflict pair) remain. |
| Dirty-tracking `save()` (auto-merge changed fields) | ❌ Rejected. Invasive across all config dataclasses; in-place mutations of dict/list fields bypass `__setattr__` — correctness holes. |
| Move config into SQLite | ❌ Rejected. Disproportionate. |
| CI grep guard + AGENTS.md convention ("direct load→save is a defect") | ❌ Dropped (owner). Detail belongs at the chokepoint: the `V2Config.save()` and `update_config_fields` docstrings carry the warning where writers browse. No global doc, no guard. |

## Landed

- **#1465** (`config_write_transaction` / `update_config_fields` primitive
  + 5 narrow field writers migrated: relay marker, language ×2, opencode
  default_provider, rotate_session_secret; real two-process lock-contract
  test).

## PR #1513 implementation update (2026-08-22)

The remaining UI writers are now expressed through one client-side mutation
protocol. `ApiContext.mutateConfig()` accepts explicit leaf assignments and
the whitelisted `platforms.enabled` add/remove operation; it is the only UI
owner that serializes those mutations to the existing `/api/config` merge-patch
HTTP body. Wizard steps derive mutations from the fields they own, so a later
step cannot replay mount-time `agents`, platform sections, or runtime paths. The wizard also
records only list operations it actually submitted; the load-time enabled list is an observation,
not permission to re-enable a platform at Finish. Channel and guild settings are committed by
the Channels/credential steps, while Summary owns completion flags and final wizard list intent
only, so it does not replay the accumulated channel snapshot. After Finish,
Summary routes startup and bind-code handling from the lock-fresh config
response, not the wizard's local platform selection; a skipped or otherwise
unpersisted selection therefore resolves to the platforms actually enabled by
the transaction (including a valid Workbench-only setup).

On the server, `config_file_lock` is the single cross-process lock owner. The
generic `save_config` read/merge/validate/write cycle, `config_write_transaction`,
first-run creators, ordinary `V2Config.save`, Memory updates, and the WeChat QR
writer all use that lock (nested acquisition is re-entrant). The lock prevents
overlapping read-modify-write cycles; it does not make an explicitly submitted
stale snapshot safe, which is why the UI mutation boundary remains required.

The branch adds lock-boundary tests that prove both the transaction's load and
mutator wait behind the migration lock, plus focused UI tests for field ownership
and Wizard step isolation. This is the intended close-out shape for stage ③;
the remaining work is remote review/CI verification on the rebased branch.

## Remaining — stage ③ (the close-out checklist)

The original checklist below names the writer surfaces that needed ownership
and transaction coverage. A surface may use `save_config` with an explicit
patch (when it must retain API validation/runtime reconciliation) or
`update_config_fields` (for a typed in-memory mutator); the contract is the
same: the decision and write must observe one lock-fresh snapshot.

1. `vibe/api.py` `save_config` — serialize the WHOLE read-merge-write
   cycle (base load currently happens outside the file lock, so a
   controller write between load and save is overwritten); callers that
   pass a full snapshot rather than a partial payload are reclassified
   as snapshot overwrites and narrowed to the fields they own.
   Read-decide-write callers (compare-then-assign) keep their decision
   INSIDE the transaction mutator on the freshly loaded config.
2. `core/handlers/model_hub/service.py` — model_hub section save.
3. `vibe/api.py` codex auth save mirror + claude auth save mirror.
4. `vibe/api.py` remove-key V2Config clear.
5. `core/agent_auth_service.py` auth-mode persist (3 sites). The
   relay-marker pre-persist boundary is **retained as-is**: the marker
   must be durable BEFORE `_clear_codex_api_key_for_oauth()` destroys
   its only on-disk source, while the auth-mode mirror must only land
   AFTER the external codex files updated — one transaction cannot
   preserve both failure guarantees across an external-file write.
6. `vibe/api.py:7047` agent install cli_path update,
   `vibe/api.py:7325` avault install cli_path update,
   `vibe/api.py:11900` opencode default-provider clear. The clear site
   is read-decide-write (compare current default before clearing), so
   its comparison AND assignment run on the freshly loaded config
   inside the transaction mutator — not decisions outside, assignment
   inside — otherwise a concurrent default switch to another provider
   still gets cleared.
7. First-run config creators: `vibe/runtime.py:149-151` and
   `core/services/settings.py:83-87` both check-for-absence outside the
   file lock then `default.save()`. Migrate to an atomic
   create-if-absent transaction (load-or-default inside the lock, save
   only when the file was absent) so a delayed first-run snapshot cannot
   overwrite an initial settings save another process completed.

Non-goals: no schema change, no file split, no new IPC, no guards.

## Close criteria for #1458

Stage ③ merged → every cross-process `config.json` writer is
patch-shaped under the transaction → issue closed with a pointer to the
parked single-writer option. A green local run is not sufficient: close only
after the PR head has a clean merge state, zero unresolved review findings,
and passing CI on the final pushed commit.
