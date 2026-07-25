# Memory Architecture Deepening

> Status: complete
>
> Follow-up architecture work on the Memory system delivered by
> [PR #1006](https://github.com/avibe-bot/avibe/pull/1006). Product behavior is
> unchanged throughout: every item here is a structural change behind existing
> interfaces, verified by the existing Memory test suite.

## Background

The Memory system landed with one declared port — `MemoryProviderPort` in
`core/memory/everos.py`, published alongside `FakeMemoryProvider`. That single
seam is why `tests/test_memory_module.py` needs one `monkeypatch` across 1,612
lines.

The two dependencies that did not get a port cost the opposite:
`tests/test_memory_runtime.py` spends 2,151 lines on 58 `monkeypatch.setattr`
calls and 43 distinct private-attribute pokes, and re-declares the same
`_Process` and `_Artifact` fakes ten times each.

Three things are already deep and are explicitly out of scope:

- `core/managed_runtime.py` — a real seam with three adapters
  (`GitRuntimeManager`, `MemoryArtifactManager`, `EngineRuntimeManager`).
  Manifest parsing, arch selection, checksum verification and safe extraction
  are shared, and Memory reuses them correctly.
- `MemoryModule` — six methods over 1,046 lines.
- `modules/im/message_facts.py` — the five per-platform ordinary-text predicates,
  consolidated in `2a504b9e`.

## Goal

Raise the depth of the Memory modules that callers and tests actually cross,
without changing product behavior:

- the interface becomes the test surface, so tests stop reaching past it;
- rules that exist in more than one place collapse into one;
- adding a platform, a status counter, or a runtime method stops requiring
  edits in N files.

## Solution

Six changes, each its own commit, in order. Each keeps
`tests/test_memory_*.py` and `tests/test_ui_memory_routes.py` green.

### 1. Ports for the process and artifact dependencies

`MemoryRuntime` constructs `EverOSProcess` and `EverOSPort` internally, so tests
substitute them by patching module globals and private attributes.

The interfaces `MemoryRuntime` actually needs are small:

| Port | Members | Implementation behind it |
|---|---|---|
| `EverOSProcessPort` | `running`, `starting`, `start()`, `stop()`, `processing_healthy()` | `EverOSProcess`, 990 lines |
| `MemoryArtifactPort` | `resolve_python()`, `status()`, `ensure()`, `provider_root_format()`, `artifact_fingerprint()`, `compatible_provider_root_formats()`, `set_provider_root()`, `set_activation_coordinator()` | `MemoryArtifactManager`, 668 lines |

Declare both as `Protocol`s next to their implementations, publish a fake with
each, and let `MemoryRuntime` accept a process factory plus a typed artifact
port. The defensive `getattr(self._artifact_manager, "...", None)` checks in
`runtime.py` exist only because the interface was undeclared; a port removes
them.

### 2. Collapse `UnavailableMemoryRuntime`

`UnavailableMemoryRuntime` mirrors ten methods of `MemoryRuntime` by hand and
answers one condition — "the store could not be opened" — three ways: raising
`MemoryStoreUnavailableError`, returning `{"status": "failed"}`, and returning
`{"ok": False}`. It is returned as a bare union with no `Protocol`.

Make store acquisition lazy inside `MemoryRuntime`, make "unavailable" one of its
internal states, and report it through the existing `OperationFailed` result.

### 3. Queue lifecycle behind `MemoryStore`

`MemoryStore` exposes 30 public methods to exactly two callers. Fourteen of them
form the delivery and fault-breaker state machine, whose rules live in
`worker.py`: the store does not enforce that a claimed row is settled, or that a
flush marked in flight reaches a verdict.

Collapse those primitives into transition methods that take an outcome.

`compact_terminal_tombstones` has no caller outside the store, but its retention
rule has no other direct test, so it stays public rather than losing coverage.

### 4. One status bucket contract

`memory_status_buckets` (`core/memory/presentation.py`) and
`memoryStatusBuckets` (`ui/src/lib/memoryStatus.ts`) implement the same
six-bucket rule once per language, each with its own dedicated test, while
nothing verifies that the backend emits those six counter names.

Emit the buckets from `MemoryModule.status`, delete the TypeScript copy, and
replace the four ad-hoc result type guards in `SettingsMemoryPage.tsx` with the
discriminated result the backend already returns. `vibe/cli.py` already consumes
the Python one.

### 5. `core/memory/admission.py`

Capture admission is decided across `Controller` (six methods, ~130 lines),
`session_turns`, `ui_server` and the adapters, carried by a nullable
`is_ordinary_text` bool on `MessageContext` that nothing enforces. The Workbench
predicate sits in `vibe/ui_server.py` while its five siblings live in
`modules/im/message_facts.py`.

Move the admission methods into `core/memory/admission.py` taking a facts record
and returning the existing `CaptureRequest | CaptureSkipped`, and move the
Workbench predicate next to the others.

This finishes an intent the Memory contract already states — "platform adapters
classify native events but do not own Memory business logic" — rather than
revising it.

### 6. Memory read module in the UI

`SettingsMemoryPage.tsx` is 1,122 lines with 32 `useState` calls, where five
panels each re-implement fetch, loading, error-code mapping and result
discrimination. The six memory routes plus seventeen helpers are inline in
`vibe/ui_server.py`.

Extract one read module the panels call, split the panels into their own files,
and lift the routes into a module of their own.

## Todo

- [x] 1. Ports for `EverOSProcess` and `MemoryArtifactManager` — `d13fef20`
- [x] 2. Collapse `UnavailableMemoryRuntime` — `a2afdadb`
- [x] 3. Queue lifecycle behind `MemoryStore` — `3989a8e8`
- [x] 4. One status bucket contract — `f170ddb3`
- [x] 5. `core/memory/admission.py` — `3546c638`
- [x] 6. Memory read module in the UI — `a515f053`

## Validation

Per commit:

- `uv run pytest tests/test_memory_*.py tests/test_ui_memory_routes.py`
- `ruff check` on changed Python files
- `cd ui && npm run build` for commits touching `ui/`

Full-suite gates stay on GitHub CI.

## Known pre-existing flake

`tests/test_memory_runtime.py -k activation` fails roughly 1 run in 30 with
`FileNotFoundError` on `memory.sqlite-shm` reaching
`_recover_interrupted_clear` as `memory_clear_failed`. Measured at the same rate
on `09e43029` (before this work) and after commit 3989a8e8, so it is not caused
by these refactors. It involves real SQLite in WAL mode, the drain task, and the
cross-thread `future.result(timeout=90)` handoff in
`_coordinate_artifact_activation`. Worth its own fix; out of scope here.

## Follow-ups this work deliberately left open

- **`modules/im/slack.py` hardcodes `is_ordinary_text=True`** on the native
  slash-command context. Reads as drift from `2a504b9e` rather than intent, and
  the Memory contract treats a slash command as a control event. Latent, not
  live: slash commands never reach `MessageHandler.handle_message`. Changing it
  is a product-behavior decision.
- **Three failure conventions for an unavailable store** remain
  (`MemoryStoreUnavailableError`, `{"status": "failed"}`, `{"ok": False}`).
  Unifying them onto `OperationFailed` would turn the 503s in
  `core/internal_server.py` into 200s, so it needs a product decision.
- **The activation flake** described above.
- **Profile and search still repeat the item-list markup.** Second occurrence,
  not third; extract on the next repeat.
