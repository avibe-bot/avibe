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

## Remaining — stage ③ (the close-out)

Migrate every remaining direct writer to `update_config_fields`:

1. `vibe/api.py` `save_config` — serialize the WHOLE read-merge-write
   cycle (base load currently happens outside the file lock, so a
   controller write between load and save is overwritten); callers that
   pass a full snapshot rather than a partial payload are reclassified
   as snapshot overwrites and narrowed to the fields they own.
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
   `vibe/api.py:11900` opencode default-provider clear.

Non-goals: no schema change, no file split, no new IPC, no guards.

## Close criteria for #1458

Stage ③ merged → every cross-process `config.json` writer is
patch-shaped under the transaction → issue closed with a pointer to the
parked single-writer option.
