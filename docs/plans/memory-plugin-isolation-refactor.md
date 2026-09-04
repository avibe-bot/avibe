# Memory Optional Package Isolation Refactor

> Status: complete (2026-08-29)
>
> Completion baseline: `origin/dev` at `8033662fa` (post-#1774)
>
> `docs/MEMORY.md` is the current product contract. This plan records the
> completed isolation waves and their retained boundaries.

## Decision

Memory runtime implementation now lives in an independently published,
optional Python package loaded in the Avibe process only when Memory is enabled.
EverOS remains the one Memory child process. This refactor does not add another
sidecar, UDS protocol, process supervisor, generic plugin host, or dynamic plugin
registration system.

Avibe keeps a small host integration surface:

- a main-path `MemoryCaptureAdapter` with one `offer` method;
- `DisabledMemoryAdapter` for disabled, missing, or failed optional packages;
- the released typed Memory configuration and destructive-write safety fences;
- the existing named internal Memory routes, CLI command set, Settings UI, and
  prompt behavior; and
- lightweight host-owned request/authentication contracts that do not import
  Memory runtime implementation.

This plan defines the companion-package boundary behaviorally: the optional
implementation package may be deleted or never installed without making Avibe's
main product unavailable. It does not mean removing every Memory label or
explicit user surface from the host.

## Why There Is No Second Memory Process

EverOS already runs as an isolated child under `EverOSSupervisor` and owns the
provider-facing work. Adding

```text
Avibe -> avibe-memory process -> EverOS process
```

would duplicate process lifecycle, health, IPC, restart, release, and shutdown
logic. It would also require a new request protocol for operations that already
use Avibe's internal HTTP routes.

The default target is therefore:

```text
Avibe process
  |
  +-- DisabledMemoryAdapter                    package absent or disabled
  |
  `-- Enabled adapter from optional package   package present and enabled
          |
          `-- existing EverOS child process
```

The optional package is trusted first-party Python code, not hostile code. The
adapter contract and tests protect main-path behavior from expected failure,
latency, queue saturation, and provider faults. They do not claim to sandbox an
arbitrary infinite CPU loop inside Python. A second process is considered only
after a reproducible fault shows that the in-process adapter cannot meet the
bounded contract.

## Completed Scope

The waves closed the following independence gaps without changing Memory product
behavior:

1. Controller no longer imports Memory implementation at module import time.
2. Disabled startup does not construct `MemoryRuntime`.
3. Disabled startup does not open `memory.sqlite` or create attachment, state,
   and lock directories.
4. The optional adapter owns Memory admission, capture tasks, capacity, and
   attachment scheduling.
5. New Workbench and delivery persistence does not write `_memory_*` metadata or
   depend on it for queue segmentation.
6. Memory task and destructive-operation shutdown paths are bounded.
7. Generic helpers and host contracts needed by archive, UI auth, config, and
   internal clients live outside the optional implementation.
8. Install and upgrade paths resolve `avibe-memory` as an optional independent
   distribution.

The refactor targets these gaps. It does not reimplement working Memory product
behavior.

## Behavioral Contract

### Disabled or absent

**Disabled = inert + truthful:** disabled paths never construct a Memory runtime,
status reflects retained fenced ownership, and every internal Memory endpoint
fails closed with its stable error shape for unknown lifecycle failures.

- A fresh home with Memory disabled imports no optional Memory implementation.
- It creates no Memory database, attachment directory, provider root, artifact
  state, or Memory lock.
- It starts no Memory task and no EverOS process.
- An enabled config with a missing or incompatible package degrades only Memory;
  chat, Agent dispatch, `/new`, archive, startup, and shutdown remain available.
- Explicit Memory routes return `memory_implementation_unavailable` with a stable
  user-facing explanation.

An existing disabled installation may need to reap an EverOS process left by an
older release. That cleanup must use existing generic runtime ownership metadata,
run asynchronously after core readiness, create no Memory state, and never block
startup. Fresh disabled installations still have zero Memory side effects.

### Enabled

- Capture remains best effort and outside Agent completion.
- Queue saturation drops capture without backpressure.
- `/new` and archive remain successful when Memory is slow or failed.
- Attachment pin/copy, admission, profile/search/list/remember, prompt routing,
  repair, deletion, and Processing Record behavior remain unchanged.
- Existing Memory config and provider data remain readable without a data move.

### Shutdown and replacement

- Capture registration closes immediately.
- Capture cancellation and destructive-operation settlement each have a finite
  total deadline.
- Expired volatile work is dropped, consistent with `docs/MEMORY.md`.
- The existing EverOS stop-proof and process-group reap behavior remains inside
  the optional implementation.
- Runtime replacement never waits for capture delivery and never runs two
  Memory runtimes against the same provider root.

## Main-Path Interface

The only interface used by message and session flows is:

```python
class MemoryCaptureAdapter(Protocol):
    def offer(self, event: MemoryEvent) -> None: ...
```

`offer` has a closed contract:

- it performs only bounded in-memory validation, lease retention, capacity
  reservation, and `put_nowait` or task registration;
- it performs no filesystem, provider, subprocess, or network I/O on the caller
  stack;
- it never waits and never raises;
- on rejection or queue saturation it releases every retained resource; and
- callers ignore its outcome.

`DisabledMemoryAdapter.offer` is an unconditional no-op.

Lifecycle cleanup is a composition concern owned by Controller startup/shutdown,
not part of this interface. Message handlers cannot close, restart, inspect, or
reconfigure Memory through the capture seam.

Explicit Memory operations do not use a generic `request(operation, payload)`
method. They keep the existing named internal routes and closed CLI command
surface.

## Events

There are three main-path events:

- `TurnAccepted`, offered after a human turn has a stable session/generation and
  admitted local attachments;
- `SessionReset`, offered after `/new` commits; and
- `SessionArchived`, offered after archive commits.

`vibe memory remember` is an explicit named operation and never becomes a fourth
event. Runtime replacement is internal optional-package lifecycle and does not
become a host event.

Events carry trusted host facts, not Memory decisions:

- authenticated author id and surface;
- canonical session id and generation;
- platform, conversation kind, and stable native identifiers;
- original/edit/forward/quote/system/unknown message kind;
- text, attachments, and the existing generic inbound attachment lease; and
- workdir only where the current admission contract already permits it.

Events do not contain Memory principal/project ids, `memory_enabled`, admission
results, provider state, `_memory_*` metadata, or Memory-specific reservation
objects. The enabled adapter derives all Memory facts and owns capture tasks.

## Message Facts and Delivery Merge

The current `is_ordinary_*` classifiers describe native message shape, not
provider behavior. They remain with the IM adapters but are renamed into core
vocabulary, for example `is_original_human_text` and
`is_original_human_attachment`. Unknown shapes remain fail closed.

New Workbench delivery snapshots use core-owned identity:

- `author_id` remains the authenticated local or remote subject and replaces
  `_memory_user_id` as the trusted author input;
- a first-class `message_kind` distinguishes original, quick reply, forwarded,
  edited, system, and unknown input; and
- `message_merge_identity` includes `message_kind` in addition to its existing
  `author_id`, author, source, type, scope, and native-message fields.

Web-push ownership is not automatically a Memory principal: Workbench
`author_id` is Memory-trusted only after strict direct-loopback or authenticated
remote HTTP admission. A third identity/authority P1 on this PR triggers the
circuit breaker and spec review before any further implementation.

This preserves the current safety properties:

- deliveries from different authenticated authors never merge;
- a quick reply never merges with an original human message;
- forwarded/edited/system/unknown input never widens original-human admission;
  and
- one merged Turn has one stable author and message kind.

`memory_cli_admitted` is derived at dispatch from authenticated author and
current Memory configuration; it is not persisted. The Memory adapter derives
its owner from `author_id` for Workbench and the authenticated sender for IM.

Legacy queued rows retain a compatibility reader for `_memory_*`. New writes
stop emitting the fields. The compatibility reader translates old identity into
the core merge key until all released queue rows are naturally retired; there is
no bulk database rewrite and no unsafe mixed merge.

## Attachment Contract

This isolation refactor preserves the current attachment contract:

1. Avibe materializes the file once for Agent delivery and owns the generic
   `InboundAttachmentLease`.
2. The enabled Memory adapter retains that lease inside `offer` before returning.
3. The adapter owns Memory capacity reservation, config-generation validation,
   pin/copy into private Memory storage, and final release.
4. Pin failure keeps the released text-only fallback semantics.
5. The disabled adapter never retains the lease.

`MessageHandler` no longer imports Memory admission or manipulates Memory
reservations, but it does not weaken file lifetime or integrity. Replacing pin
with an expiring path/reference is a separate product decision and is outside
this plan.

## Explicit Host Surfaces That Stay

Keeping these surfaces is deliberate. They are user-selected, do not run on the
main message path, and removing them would create more migration complexity than
isolation value.

### Configuration

Keep `MemoryConfig`, parsing, validation, `atomic_update_memory`, stale-write
protection, and the cross-process operation lease in Avibe. In particular, the
host continues to enforce:

- `enabled` and complete endpoint requirements;
- embedding identity comparison;
- exact `confirm_loss: true` admission;
- `transition_notice_pending`, applied embedding identity, and organization
  transition behavior;
- `legacy_needs_repair` / `repair_required`; and
- compatible loading of released config shapes.

The optional package receives a validated config snapshot. This plan does not
make the Memory mapping opaque and does not change config behavior.

### Internal routes, UI, and CLI

- Keep the current `/internal/memory/*` routes and named internal-client methods.
- Keep the current Settings Memory React page and API result shapes.
- Keep the closed `vibe memory` subcommands, parsing, exit codes, output, and
  i18n.
- Keep current browser auth, CSRF, HMAC proof, and principal derivation.
- When the optional implementation is absent, these routes return one stable
  unavailable result instead of importing it.

There is no iframe, same-origin proxy, dynamic CLI passthrough, or new Memory RPC
protocol.

### Agent prompt

Keep the registered `memory-context-prompt` contract in the host. Stable
`memory.enabled` configuration selects the Memory or Preferences prompt for the
Session; turn-scoped CLI authorization never selects prompt content. Prompt
construction never invokes the optional runtime.

## Host and Optional-Package Ownership

### Remains in `avibe-os`

- `MemoryConfig` and destructive config transaction safety;
- `MemoryCaptureAdapter` and `DisabledMemoryAdapter`;
- generic message-kind and delivery-merge facts;
- existing Memory UI, CLI, internal routes, auth, i18n, and prompt contract;
- lightweight request/response/error/project-id types required by those host
  surfaces; and
- a fixed optional-package loader at the composition root.

### Moves to `avibe-memory`

- capture admission and owner/project derivation;
- capture task registry, capacity reservation, writer, and pending flush state;
- attachment Memory selection, pin store, modality policy, and extraction;
- Memory SQLite schema/store and processing diagnostics;
- `MemoryRuntime`, `MemoryModule`, provider readers, repair/delete runtime logic;
- EverOS HTTP adapter, process, supervisor, provider-root ownership;
- artifact manager, runtime manifest, runtime build scripts, and release guard;
  and
- Memory implementation tests that exercise only the optional package seam.

### Leaf modules moved before package extraction

Deleting `core.memory` must not delete unrelated host behavior:

| Current leaf | Target owner |
| --- | --- |
| `core.memory.blocking` | generic cancellation-safe blocking helper under `core` |
| `core.memory.operation_lock` | host Memory config/destructive transaction module |
| `core.memory.ui_access` | host Memory UI authentication module under `vibe` |
| `core.memory.http_headers` | host internal Memory request contract under `vibe` |
| `core.memory.admission_metadata` | replaced by `author_id` + core `message_kind`; legacy reader only |
| processing-record/result/error types used by clients | lightweight host Memory contract |
| project-id input parsers used by routes | lightweight host Memory contract |
| `core.memory_telemetry` | optional package or generic attachment logging, depending on caller |

No new `memory_host` framework package is required. Put each leaf under its
existing functional owner.

## Installation and Upgrade Contract

The package split does not silently disable Memory for existing users.

1. Publish `avibe-memory` before the first `avibe-os` release that can load it.
2. Add an `avibe-os[memory]` extra that resolves a host-compatible
   `avibe-memory` release.
3. The post-split upgrade planner chooses the extra when either Memory is enabled
   or the optional package is already installed. Disabled core-only installs
   continue upgrading without it.
4. An immutable bundled-Memory upgrader can only install the first split core.
   After the new core starts, the existing UI background dependency reconciler
   waits for host readiness, reads the persisted enabled state, and reuses the
   exact-version Memory package install action under the shared upgrade lock.
   It persists a three-attempt budget per core version before mutation and
   schedules one ordinary service restart. Controller startup never installs or
   downloads Python packages. Disabled startup remains a zero-install path; a
   published core-only install exposes only a user-invoked companion bootstrap
   so Memory can be enabled later.
5. Later enabled upgrades fail before replacing the current installation if the
   compatible optional package cannot be resolved. The first-hop convergence
   fails closed with Memory unavailable and core Avibe healthy; it never changes
   Memory config or provider data. A later startup retries only within the
   persisted per-version budget; an explicit action remains available after
   exhaustion. A failed activation restart stays repairable and retries restart
   without reinstalling the already-exact companion; the surviving UI process
   continues reconciling its other managed dependencies.
6. A successful upgrade or first-hop convergence schedules the ordinary Avibe
   restart when the runtime is active. Install, upgrade, or restart failures are
   structured terminal results and do not block a later explicit attempt.
7. The existing Dependencies/Memory install action remains available to
   explicitly reinstall the current tool with the Memory extra.
8. EverOS runtime download/install remains an explicit Memory dependency action,
   but its manifest and implementation move with `avibe-memory`.

There is no automatic package rollback, rollback plan, package-lifecycle
reservation, quarantine, Gate 5 lifecycle verifier, or general recovery
bootstrap. The enabled-package startup convergence above is the one dependency
invariant needed to bridge immutable bundled-Memory upgraders. The
retained manifest, hash, fetch, verify, and backup safeguards protect published
Memory Runtime artifact availability; they do not mutate installed Python
packages or recover an upgrade attempt.

Publishable core and optional-package artifacts carry reciprocal exact-version
dependencies. The fixed lazy loader validates the factory and constructed runtime
surface directly. A missing or incompatible implementation selects
`DisabledMemoryAdapter` and reports `memory_implementation_unavailable` or
`memory_implementation_incompatible`; it never fails service startup.

## Completed Migration Waves

Each wave landed as a focused, green PR. The repository did not merge placeholder
scenario IDs or permanent xfails.

### Wave 0: Make disabled Memory truly off (complete)

Delivered:

- choose `DisabledMemoryAdapter` before importing or constructing Memory runtime;
- stop opening Memory store, attachments, artifact state, and operation locks
  when disabled;
- do not schedule Memory wake on fresh disabled installs;
- move `ui_access` and any other startup-required host leaf out of
  `core.memory`;
- bound capture cancellation, destructive settlement, and runtime close during
  shutdown; and
- preserve asynchronous cleanup of pre-existing owned EverOS processes without
  creating new Memory state.

Evidence added:

- `MEMORY-INDEP-013`: disabled fresh startup has zero Memory filesystem/process
  side effects;
- `MEMORY-INDEP-014`: disabled Controller startup succeeds while optional
  implementation imports are blocked; and
- `MEMORY-INDEP-015`: a frozen Memory task cannot make Avibe shutdown unbounded.

Completion evidence: these scenarios and existing `MEMORY-INDEP-001..012` pass. No enabled
Memory product behavior changes.

Compatibility: this wave introduced no persisted format change.

### Wave 1: Return message and session ownership to core (complete)

Delivered:

- rename archive to a core session operation and keep its Memory observation
  after commit;
- remove the Memory-named `/new` wrapper while preserving generation safety and
  best-effort final flush behavior;
- rename platform message-shape classifiers into core vocabulary;
- add `message_kind` to durable snapshot and merge identity;
- stop writing new `_memory_*` metadata and translate legacy rows on read;
- derive Workbench author from existing authenticated `author_id`;
- move admission, task tracking, capacity reservation, and attachment pin
  scheduling behind the enabled adapter; and
- preserve the exact attachment, prompt, config, and explicit-route behavior.

Evidence added:

- `MEMORY-INDEP-016`: new rows use author/message-kind merge identity and contain
  no Memory-private metadata;
- merge characterization for two remote principals, original plus quick reply,
  forwarded/edited input, scheduled delivery, and legacy/new queued rows; and
- attachment tests proving retain/pin/text-only fallback behavior is unchanged.

Completion evidence: removing or forcing the adapter to raise does not change chat,
`/new`, archive, queue grouping, or Agent attachment delivery.

Compatibility: the legacy metadata reader remains; no bulk data migration was
introduced.

### Wave 2: Extract the optional in-process package (complete)

Delivered:

- introduce the fixed lazy loader and enabled adapter implementation;
- move implementation modules listed above to `avibe-memory` without changing
  their internal process/storage design;
- move every host-needed leaf to its real owner before deleting `core.memory`;
- keep named admin routes and host presentation unchanged;
- keep one in-process runtime and one EverOS child; and
- fail closed to `DisabledMemoryAdapter` on absent package, import failure,
  incompatible protocol, or construction failure.

Evidence added:

- `MEMORY-INDEP-017`: core-only installation imports and runs chat, `/new`,
  archive, UI server, internal server, CLI help, and shutdown;
- explicit Memory route/CLI unavailable behavior when the package is absent;
- package import/construction failure containment; and
- core-plus-Memory parity for all existing Memory scenarios.

Completion evidence: the Avibe process imports no optional implementation when disabled;
blocking `avibe_memory` imports leaves all core scenarios green; enabled Memory
passes its existing suite against unchanged data.

Compatibility: the package move kept current data and config formats readable.

### Wave 3: Ship the package split and delete compatibility code (complete)

Delivered:

- publish the matched optional distribution and `avibe-os[memory]` extra;
- update forward upgrades to preserve companion-package presence;
- move runtime manifest/build/release guard ownership;
- add core-only and core-plus-Memory wheel matrices;
- remove `core.memory`, temporary adapters, new-write legacy metadata paths, and
  duplicate tests after the legacy read window; and
- update `docs/MEMORY.md` and user installation documentation.

Evidence added:

- the retired `MEMORY-INDEP-018` package-shape assignment remains historical
  lineage only and is not an implementation target;
- wheel-content assertions proving `avibe-os` contains no Memory runtime
  implementation or EverOS artifact manifest; and
- packaged install smoke for missing, disabled, enabled, incompatible, and
  upgrade cases.

Completion evidence: both package matrices pass CI; local Incus `master` regression passes
Workbench and Slack/Discord/Telegram/Feishu/WeChat capture; service health is
verified; no temporary fallback implementation remains.

The proposed Wave 3c rollback plan, lifecycle reservation, quarantine, Gate 5
verifier, and recovery bootstrap were abandoned and retired. Historical
scenario IDs
`MEMORY-INDEP-018` through `020`, `022`, and `023` remain unassigned and must not
be repurposed. Current executable import and package-shape evidence is owned by
the scenario catalog under `MEMORY-INDEP-021` and `MEMORY-INDEP-024`.

## Verification Summary

| Concern | Required evidence |
| --- | --- |
| Disabled | no implementation import, mkdir, SQLite, lock, task, or EverOS start on a fresh home |
| Missing package | Controller/UI/internal server/CLI import and core flows remain healthy |
| Main path | full queue, capture exception, provider hang, and adapter failure never block Agent completion |
| Session lifecycle | `/new` and archive commit with Memory frozen; stale generations are not attributed |
| Shutdown | capture/destructive/runtime cleanup stays within one finite total budget |
| Delivery | author/message-kind merge preserves current segmentation and legacy-row safety |
| Attachments | current lease, pin, integrity, fallback, and cleanup scenarios remain green |
| Config | stale write, confirm-loss, cloud transition, repair fence, and released-shape fixtures remain green |
| Explicit surfaces | UI, named internal routes, CLI output/exit codes, and prompt routing retain parity |
| Packaging | core-only, core-plus-Memory, enabled upgrade, disabled upgrade, terminal failure, and retry evidence |

## Complexity Guardrails

Do not add the following in this refactor:

- a second Memory process or new UDS protocol;
- `PluginHost`, `PluginManager`, manifest discovery, or directory scanning;
- a generic `request(operation, payload)` interface;
- dynamic UI, CLI, route, prompt, or configuration registration;
- iframe/proxy-based Memory UI extraction;
- opaque Memory configuration;
- a new event bus, durable capture outbox, or dual writer;
- weaker attachment lifetime semantics; or
- prompt or admission product changes hidden inside isolation work.

Shared plugin infrastructure is considered only when a second independently
shipped plugin has concrete, overlapping requirements. Process isolation is
considered only when a reproducible test proves that this in-process Adapter
cannot meet the bounded main-path contract.

## Definition of Done

The refactor is complete when:

- disabled fresh startup has zero Memory runtime side effects;
- deleting or never installing `avibe-memory` leaves Avibe fully operational;
- Memory import, construction, provider, and capture failures cannot affect core
  outcomes or make shutdown unbounded;
- core session and delivery state no longer depends on Memory-private metadata;
- attachment, config safety, prompt, UI, CLI, and enabled Memory behavior remain
  unchanged;
- existing enabled users receive the compatible package during forward upgrade,
  and a terminal failure does not prevent a later attempt;
- `core.memory` and every temporary migration adapter are deleted; and
- no second sidecar or generic plugin framework was introduced.
