# Memory independence: capture must not fence dispatch or destructive ops

Context: issue #1584 Memory-independence audit items C2 and C3.
This plan does **not** claim to close #1584 (C1 / WeChat identity is a sibling PR).

## Background

PR #1357 and the later Memory capture fence made Avibe's per-session
lifecycle lock a mutual-exclusion gate between:

1. durable turn start
2. in-flight Memory capture
3. destructive session transitions (`/new`, archive)

That gate keeps a reset or archive from racing a capture that would
attribute content to the wrong session generation. It also made Memory
latency a user-visible dependency: a slow sidecar add (30s) delays the
session's next turn, and a capture wedged past its timeout blocks the
session indefinitely. `/new` and archive wait 5s on the same lock and
then fail the user with `memory_session_lifecycle_busy`.

The product rule for this change: a hung or failed Memory capture must
never block message turns, `/new`, or archive. Losing at most one
turn's capture is acceptable; failing the user's action is not.

## Current fence semantics

### Layer A — Avibe session lifecycle lock (`core/session_turns.py`)

- `_session_lifecycle_locks[session_id]` is an unbounded `asyncio.Lock`.
- `_start_persisted_turn` awaits `acquire_lifecycle_admission` with
  **no timeout**, then parks the lease on
  `context.platform_specific["_turn_lifecycle_admission"]`.
- Text-only capture (`MessageHandler._schedule_text_only_memory_capture`)
  takes that lease (or acquires its own) and holds it until the capture
  task completes.
- Attachment capture acquires the same lock on the **turn path** after
  materialization, then releases it as soon as the capture task is
  scheduled. Generation safety for the download gap uses
  `session_lifecycle_epoch` / `session_lifecycle_epoch_matches`.
- `run_session_lifecycle` waits up to 5s for the lock. On timeout it
  raises `SessionTurnLifecycleBusyError` (`memory_session_lifecycle_busy`).
  On success it runs the operation, then increments
  `_session_lifecycle_epochs[session_id]`. A failed operation leaves
  the epoch unchanged.

Callers:

- `/new` wraps the reset in `SessionTurnManager.run_session_lifecycle`
  (`core/handlers/command_handlers.py`), then nests Memory's own
  `run_session_lifecycle` (flush + capture-admission lock).
- Workbench archive still wraps the terminal session write in
  `SessionTurnManager.run_session_lifecycle`. PR #1582 already made the
  Memory final flush best-effort *after* that write: a busy or failed
  Memory runtime cannot roll archive back. The remaining archive risk is
  only the shared turn-lifecycle lock raiser, not a second Memory fence.

C3 in this PR is therefore `/new` plus every remaining
`memory_session_lifecycle_busy` raiser. It does not re-work #1582's
archive flush. Fail-opening `SessionTurnManager.run_session_lifecycle`
clears the last archive raiser as a side effect of the shared lock.

### Layer B — Memory capture-admission lock (`core/memory/module.py`)

`MemoryModule.run_session_lifecycle` waits for in-flight
`capture_admission` tickets, then final-flushes, then runs the
operation. On timeout it raises `MemorySessionLifecycleBusyError`
(same code string). `/new` nests this **inside** layer A via
`controller.run_memory_session_lifecycle`. So a hung sidecar can
make `/new` wait 5s + 5s and still fail the user.

`run_session_scopes_lifecycle` has the same timeout raise. Archive
flush already swallows that exception (PR #1582). Maintenance-open
still raises; that path is not a hung-capture case and stays as-is.

### Why the fence exists (invariant we must keep)

A capture admitted against session generation **G** must not be written
as if it belonged to generation **G+1**. `/new` reuses the same raw
session anchor on IM surfaces, so a late add after reset mixes the old
conversation into the new one.

The epoch counter already exists for this (`session_lifecycle_epoch`,
consumed by attachment capture). Mutual exclusion was a stronger
implementation of the same invariant, not a second invariant.

## Chosen design

**Generation token replaces mutual exclusion as the correctness
mechanism.** The per-session lock remains only as a *best-effort
quiesce* so a nearly-finished capture can flush before reset. It is
never a dispatch fence, and it is never a reason to fail the user.

Rejected alternative: bound the turn-start wait at ~5s and proceed.
That still makes Memory latency a message-turn delay. The audit allows
dropping the turn-start acquire entirely; we do that.

### C2 — invert the turn-start dependency

1. `_start_persisted_turn` snapshots
   `session_lifecycle_epoch(anchor)` onto the context
   (`_turn_lifecycle_epoch`) and does **not** acquire the lifecycle
   lock. Dispatch never waits on Memory.
2. `MessageHandler` uses that snapshot when present; otherwise it
   samples the epoch when the session id is known (legacy / non-durable
   path). The snapshot is taken **before** any await a concurrent
   `/new` could win.
3. Capture tasks — text and attachments — acquire the lifecycle lock
   **inside the capture task**, never on the turn/`_handle_turn` await
   path. The next turn can start and reply while a previous capture
   still holds the lock.
4. After the capture task acquires the lock, it revalidates the
   snapshotted epoch. Mismatch logs and returns without calling
   `capture_user_memory`.
5. `drain_memory_capture_tasks` / `cancel_memory_capture_tasks` /
   `quiesce_memory_capture_tasks` stay as they are: shutdown still
   settles or cancels accepted captures.

### C3 — destructive ops fail open

`SessionTurnManager.run_session_lifecycle`:

- Still waits up to 5s for the lock (best-effort flush of an in-flight
  capture).
- On timeout: log a warning, advance the epoch (abandon in-flight
  captures for that session), **do not raise**, run the operation
  without holding the lock.
- On acquire: run the operation while holding the lock; on success
  advance the epoch (same as today). A failed operation still leaves
  the epoch unchanged so an in-flight turn is not abandoned by a
  failed `/new`.

`MemoryModule.run_session_lifecycle` and
`run_session_scopes_lifecycle` timeout paths: log a warning, skip the
final flush, run the operation. They no longer raise
`MemorySessionLifecycleBusyError` on quiesce timeout.

`/new` therefore always reaches `_reset_session`. Archive always
reaches the terminal session write. Nested fences cannot stack a
user-visible busy error.

### Abandonment

Advancing a session's epoch is the abandonment signal. Capture tasks
for that session are also cancelled so a hung sidecar await does not
resume into a write after `/new`. The epoch check after lock acquire
covers the case where a waiting capture wakes after the bump and was
not cancelled.

Residual: a sidecar `add` that already left the process may still
complete in EverOS after cancel. That write carries the pre-transition
`session_id` and is the one-turn loss the audit accepts. Tests assert
the Avibe-side decision (no `capture_user_memory` attribution after
the bump).

Epoch keys and capture buckets share one canonical session id when the
session handler is present: `/new` passes `memory_session_anchor` from
`get_base_session_id`, and capture uses `get_session_info`, which
returns that same `get_base_session_id`. When the fallback path yields
`memory_session_anchor=None`, `/new` skips the turn-lifecycle fence
entirely (pre-existing fail-open). Telegram's new-topic branch returns
before a mismatched pair is used.

Cancellation of in-flight captures happens only on the 5s timeout
path. A successful `/new`/archive only advances the epoch; the capture
task's epoch check discards stale work without killing a capture that
registered during the transition.

### Dead error surfaces

- `SessionTurnLifecycleBusyError` is no longer raised. The class is
  removed.
- `MemorySessionLifecycleBusyError` remains for the maintenance-open
  case on `run_session_scopes_lifecycle` (not a user `/new`/`archive`
  failure). Quiesce-timeout raisers are removed.
- `/new` no longer maps `memory_session_lifecycle_busy` to i18n.
  `error.memorySessionLifecycleBusy` is removed from `vibe/i18n`.
- Internal archive HTTP no longer special-cases the busy code; a
  leftover raise would surface as `session_archive_unavailable`.

## Correctness argument

Let `E(s)` be the in-memory generation for session `s`.

1. A turn admitted at generation `G` stores `G` on its context before
   any later await. Capture uses that `G`, not a later sample.
2. A successful `/new`/archive increments `E(s)` only after the
   destructive operation commits (acquired path). A timed-out
   `/new`/archive increments `E(s)` before the operation, because the
   operation will run anyway and in-flight captures must not land on
   the post-transition generation.
3. Capture writes only if `E(s) == G` after it holds the lifecycle
   lock. Therefore:
   - If `/new` acquired the lock, every lock-holding capture has
     already finished (or been cancelled on the bump). Attachment
     captures that had released the lock see the new epoch or a
     cancellation.
   - If `/new` timed out, `E(s)` is already `G+1` before reset, so a
     capture admitted at `G` cannot pass the check.
4. Turn start never waits on the lock, so a capture that never
   resolves cannot prevent the next `_start_persisted_turn` or the
   handler's agent dispatch. Capture admission acquire lives only
   inside the fire-and-forget task.
5. Shutdown still drains the same `_memory_capture_tasks` set. Moving
   the lock acquire into the task does not change membership or
   `drain_memory_capture_tasks` / `cancel_memory_capture_tasks`.

The replaced guarantee is exactly the original fence's purpose: no
cross-generation attribution. The dropped guarantee is "destructive
ops and the next turn wait until Memory is quiet." That wait is what
made Memory a messaging dependency.

## Hard boundaries

Do not touch (sibling PRs):

- `core/session_turns.py` `_hydrate_delivery_context`
- `_memory_admission_merge_identity` / `_collect_delivery_segment`
- `core/controller.py` `_memory_turn_facts`
- `modules/im/wechat.py`

Do not change `core/memory/admission.py` admission policy. Fencing
and serialization only.

## Tests

Scenario IDs (capability `memory_independence`):

- `MEMORY-INDEP-001` — hung capture does not block the next turn,
  `/new`, or archive (`memory_session_lifecycle_busy` never surfaces).
- `MEMORY-INDEP-002` — a capture admitted before `/new`/archive and
  completed after the transition is discarded.
- `MEMORY-INDEP-003` — idle-session capture still attributes; shutdown
  still drains accepted captures.

Executable evidence lives next to the existing lifecycle tests
(`tests/test_session_delivery_fsm.py`, handler/command/runtime
modules). Catalog: `tests/scenarios/memory_independence/`.

## Todo

- [x] Write this plan.
- [x] Drop turn-start lock acquire; snapshot epoch onto the context.
- [x] Move capture lock acquire into the capture task; epoch-check
      after acquire; cancel captures on epoch advance.
- [x] Fail-open `run_session_lifecycle` (session_turns + Memory
      module) on the 5s deadline; skip flush; proceed. Remaining C3
      surfaces after #1582: `/new` and the shared busy raisers, not
      archive flush.
- [x] Remove dead busy error surfaces (raise sites, `/new` i18n,
      archive HTTP special case) coherently.
- [x] Add MEMORY-INDEP-001/002/003 and update tests that encoded the
      old "busy fails the user" contract.
- [x] `ruff check` on changed Python files; run the touched test
      modules.
