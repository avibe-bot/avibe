# Terminal session state machine (root-cause refactor)

## Why

PR #659's terminal backend drew **four straight review rounds (7–10) of P2 bugs that are all
the same root cause**: session state was split across three collections kept consistent by
hand —

- `_connections: dict[str, TerminalConnection]`  (attached)
- `_detached_tmux_sessions: dict[str, float]`     (detached, value = detach time)
- `_reserved_sessions: set[str]`                   (mid-open)

…and the `max_sessions` cap was computed by summing all three with ad-hoc subtractions
(`- existing`, `- reconnecting_detached`). Every bug was "these collections got out of sync"
or "a transition wasn't atomic":

| Round | Symptom | Real cause |
| --- | --- | --- |
| 7 | detach not tracked when client exits | transition gap |
| 8 | cancel-after-spawn leak; concurrent-reconnect overwrite; dead session marked detached | three collections desync |
| 9 | replace doesn't clear detached; recheck before recording | desync |
| 10 | open-fail drops detached; superseded marked detached | desync + cap arithmetic |

A second, smaller entanglement: `_cleanup_connection()` did **two jobs at once** — released
the OS client (fd + signal) *and* mutated `_detached_tmux_sessions`. That coupling is why
every caller (open/close/reaper/shutdown) had to reason about bookkeeping side effects.

## The fix (one model, one owner per concern)

**1. One registry, one entry per id, explicit phase.**

```
_Phase = OPENING | ATTACHED | DETACHED
_Session(session_id, phase, persistent, connection?, detached_at?)
self._sessions: dict[str, _Session]      # the ONLY collection
```

- Capacity = `len(self._sessions)`. A reconnect (id already present) reuses its slot; only a
  brand-new id is checked against the cap. **No subtraction arithmetic** → the round-8/9/10
  cap bugs are gone by construction.
- One dict key per id → **double-counting is structurally impossible**.
- DETACHED→OPENING→ATTACHED is the *same* entry transitioning in place → no "stale second
  entry / forgot to clear the other collection" class.

**2. Separate OS teardown from registry bookkeeping.**

- `_teardown_client(connection, *, kill_session)` — fd + signal only, never touches
  `_sessions`.
- All `_sessions` transitions happen in `open` / `close` / `reap_idle` / `shutdown`, under
  the lock, as explicit moves with **compare-and-set identity checks**.

## How each prior bug becomes impossible

- **Concurrent reconnect**: `open` rejects when the slot is already `OPENING`; a second open
  can never mutate someone else's OPENING entry.
- **Superseded client marked detached**: `close` only transitions when
  `slot.connection is connection` (identity CAS); a newer connection owning the id is left
  untouched.
- **Open-fail drops a live detached session**: failure path (`_abandon_open`) reconciles the
  OPENING slot against `tmux has-session` — re-tracks DETACHED if the session is alive,
  drops the placeholder otherwise.
- **Cancel-after-spawn leak**: same failure path tears the spawned child down and reconciles.
- **Detach vs. shell-exit**: `close` records DETACHED only when `persistent and
  has-session`.

## Scope / safety

- Internal only. Public API (`open`→`TerminalConnection`, `close`, `resize`,
  `handle_websocket`, `shutdown`, `reap_idle`, `start_reaper`) is unchanged; `ui_server`
  and the WS pumps are untouched. No external reference to the old private collections.
- The accumulated terminal tests (round-trip, reconnect-replaces, reserve-during-spawn,
  ready-frame-fail, cancel-releases-slot, detach tracking, superseded, open-fail-preserves-
  detached) are the regression net; internal-state assertions are re-pointed at `_sessions`.

## Files

- `core/terminal_service.py` — the refactor.
- `tests/test_terminal_backend.py` — re-point internal assertions; keep behavior tests.

The symlink-over-target P1 (file_browser) is a separate, genuine fix handled in parallel.
