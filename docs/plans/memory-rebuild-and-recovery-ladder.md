# Memory: EverOS-native rebuild and recovery ladder

## Background

Changing `processing.embedding.base_url` or `processing.embedding.model` can
currently leave Memory in a durable dead end. Avibe saves an
`embedding_change_pending` marker, but `MemoryRuntime.reconcile()` only settles
that marker when every vector-bearing surface is empty. Existing data therefore
causes the save to fail, the UI rolls the candidate configuration back, and
Restart engine remains fenced. The only documented escape is Clear all, which
deletes the user's memory.

This priority plan replaces that dead end with EverOS's supported index rebuild
and adds one destructive recovery floor. It is the implementation contract for
the first recovery workstream and incorporates the validated conclusions from
`docs/memory-everos-reuse-audit.md`.

The plan is intentionally deliverable as more than one PR. Rebuild-and-Apply is
the first priority; Factory reset follows as a separate recovery-floor PR. The
reuse audit is a backlog of independently justified optimizations, not extra
scope to fold into either recovery PR.

## Verified EverOS 1.2.3 contract

Avibe pins EverOS 1.2.3. The following details are load-bearing:

- Markdown under the EverOS root is the source of truth for the business
  LanceDB projections. `system.db` as a whole is **not** a rebuildable
  projection: it also contains `unprocessed_buffer` and other system state.
- `cascade rebuild` resets `md_change_state` first, then drops and recreates the
  business LanceDB tables, scans markdown, and drains the queue. That ordering
  makes an interrupted rebuild retryable and avoids an empty index paired with
  a completed queue. It preserves markdown and `unprocessed_buffer`. The pinned
  runbook still lists the older drop-before-reset order; the command source and
  this plan use reset-before-drop.
- A configured but unavailable embedding endpoint is not a keyword-only success
  mode. The embedding exception fails the markdown queue row before its
  LanceDB/BM25 row is written. `cascade rebuild` can still exit 0 because its
  drain result counts handled rows rather than proving every row succeeded.
- Consequently, CLI exit 0 means only that the rebuild command completed. After
  the sidecar starts, Avibe must use the existing `/health.cascade` projection.
  EverOS's `healthy` flag and `reasons` report operational health; retryable and
  permanent failed-row counts are informational and deliberately do not make
  that flag false. Avibe presents those failed rows as a separate data-quality
  warning. A nonzero `pending` count alone is normal queue telemetry. It must not
  promise keyword completeness or parse stdout for an exact "pending embeddings"
  count.
- EverOS checks that the OME lock appears free before destructive work and exits
  3 when it observes a busy root. That check is explicitly best effort and has
  a TOCTOU window. Avibe can guarantee exclusion of its own managed children;
  independently launched EverOS processes using Avibe's private root are outside
  that guarantee unless EverOS later holds an exclusive root lock for the whole
  rebuild.
- The packaged Memory artifact intentionally removes `bin/everos`. The supported
  invocation is the embedded interpreter:

  ```text
  <artifact-python> -I -m everos.entrypoints.cli.main cascade rebuild --yes
  ```

  `EVEROS_ROOT` selects the provider root, and the target embedding settings are
  supplied through the existing allowlisted child environment.

Evidence baseline: Avibe `d6c7cf2d` pins `everos==1.2.3`. EverOS source commit
`560fb80` matches release commit `48fc908` for the source tree; the release
commit adds only `CHANGELOG.md`. Where EverOS prose and implementation disagree,
this plan records the pinned implementation and calls out the documentation gap.

The checked-in source manifest intentionally has `release_state: unavailable`
and placeholder archive digests, so a source checkout cannot install a managed
runtime from it. Implementation and hermetic tests can use test/dev artifacts.
For a packaged release, the existing release workflow must build the three
supported artifacts (`darwin-arm64`, `linux-arm64`, and `linux-x64`), generate the
published manifest, and embed that manifest in the package; the placeholder is
not manually replaced in source. Its availability contract is ordering at
publication: runtime assets must be downloadable before the manifest-bearing
package becomes downloadable. Artifact admission must also prove the pinned
CLI/rebuild module exists; the current smoke test only imports the API
application.

## Goals

1. An embedding identity change preserves markdown content and never mixes old
   and new vector spaces.
2. A confirmed candidate configuration is durable before destructive work and
   remains the sole configuration to retry after a crash or failure.
3. Rebuild and reset reuse the existing lifecycle locks, claim fencing, artifact
   runtime, child environment, process-group termination, and health projection.
4. The UI always offers a truthful next action without a force path that bypasses
   unfinished in-process work.
5. The implementation remains one Memory-specific operation slot, not a generic
   job framework or a second process supervisor.

## Non-goals

- No EverOS source changes.
- No `cascade backfill` workflow or promise of complete keyword recall while the
  embedding endpoint is unavailable.
- No automatic rebuild, repair, or reset based on health.
- No Force restart. A failed claim quiesce means Avibe work may still be using
  the root; killing only the sidecar cannot make destructive work safe.
- No snapshot format change. Reducing Clear/backup payloads requires a separate
  migration and recovery design.
- No change to ambiguous delivery fencing, message batching, flush thresholds,
  recorder patching, or modality policy.
- No generic operation IDs, history table, progress journal, or stdout parsing.

## Recovery model

```text
Rung 1  Supervisor backoff       Existing automatic process recovery
Rung 2  Restart engine           Replace the managed sidecar only
Rung 3  Rebuild index            Recreate business projections from markdown
Rung 4  Factory reset            Delete all mutable Memory state and start fresh
```

Rungs 1-3 preserve source content. Rung 4 intentionally deletes it. A rebuild
failure retries rung 3; it never silently escalates to reset.

EverOS also provides `cascade sync` and `cascade fix --apply` for projection
repair. They are useful but are not process-recovery rungs:

- pathless `sync` scans the markdown tree and drains; it does not force-enqueue
  every completed row;
- `fix --apply` resets retryable failures' retry budgets and can repeat external
  embedding calls and cost;
- either command can exit 0 while failed rows remain, so health must be read
  afterwards;
- a live `sync` child must coexist with the sidecar and therefore needs its own
  role-aware ownership slot rather than overwriting `everos.sidecar.json`;
- `fix --apply` remains deferred until EverOS documents its online-safety
  contract; a later confirmed retry-budget reset may require sidecar quiescence.

That explicit "Repair index" operation is a focused follow-up. The recovery PRs
do not pre-build a second child manager or expose the two CLI commands as UI
buttons. EverOS's CLI help/runbook currently describes pathless `sync` as only a
drain even though the pinned `CascadeOrchestrator.sync_once()` implementation
scans and drains; the follow-up must pin or upstream-clarify that contract before
depending on it as product behavior. It must not ship `fix --apply` as a live
operation based only on implementation similarity.

## Confirmed embedding change

`PATCH /api/memory/settings` remains the sole candidate-configuration write
interface for this UI flow. It accepts an optional exact boolean
`confirm_rebuild` field in addition to the existing settings patch.

1. If `base_url` or `model` changes and confirmation is absent, return
   `409 memory_embedding_rebuild_required`. Do not save any part of the
   candidate. Requiring confirmation even for an apparently empty root avoids a
   cross-process check-then-save race in which an old-config capture arrives
   after the check. The strict-empty fast path below means an empty root does not
   actually launch a rebuild child.
2. The UI retains the draft, explains that the index will be recreated and that
   embedding calls may take time/cost money, then resends the same patch with
   `confirm_rebuild: true`.
3. Under the existing settings write lock, save the candidate and
   `embedding_change_pending=true` before scheduling rebuild. This persisted
   candidate is the rebuild input and the crash-retry source of truth. Before
   launching the CLI for a non-empty root, require a complete embedding endpoint
   even when the candidate is disabled; an unconfigured disabled EverOS runtime
   can otherwise mark rows complete with no vectors. A strictly empty root needs
   no CLI and may settle an incomplete disabled candidate while fenced.
4. Once confirmed, a rebuild failure must not roll the configuration back and
   the UI must not reload over the draft. The marker remains set and exposes a
   Retry rebuild action after restart.
5. An `api_key`-only change with no pending rebuild retains the ordinary
   save/reconcile/rollback behavior; it does not require rebuild because it does
   not change vector identity. While a rebuild marker is pending, credential
   corrections update the same candidate and remain behind that marker so Retry
   rebuild can use them. This path returns a saved-but-`rebuild_required`
   projection and does not call ordinary reconcile, roll back the candidate,
   automatically launch rebuild, or reactivate the old vector space.
6. The settings projection exposes a read-only `rebuild_required` boolean derived
   from the internal marker. It never exposes the marker as writable input.
7. While rebuild or reset is running, other Memory settings writes return
   `memory_operation_in_progress` rather than racing the candidate.

There is no candidate configuration in the rebuild RPC and no post-rebuild save
replay. Those would create two competing sources of truth.

## One retained operation

The Controller owns at most one retained Memory operation task with the minimal
projection `{kind: rebuild|factory_reset, state: running|failed, error}`.

- A mutating request validates and records durable intent before starting the
  retained task, then returns 202.
- A duplicate request for the same operation joins/returns the current state only
  while that task is running.
- A different Memory mutation while it is active returns
  `memory_operation_in_progress`.
- Request cancellation does not cancel the retained work. Service shutdown does
  join or cancel it and waits for managed child cleanup.
- Success removes the ephemeral operation state. Failure retains only a generic
  error for status display; the persisted marker is the durable retry signal
  after a crash. The next explicit Retry atomically replaces a failed projection
  with a new running task. A failed projection is not an active-operation lock.
- The existing Memory status response carries this small projection. There is no
  separate job endpoint, operation ID, progress percentage, or operation history.

## Rebuild index

Both confirmed embedding changes and the manual Rebuild index action call the
same no-argument Controller operation. The Controller reloads the persisted
configuration; request bodies never carry provider credentials. The manual path
also persists the marker before scheduling so a service crash has the same Retry
rebuild fence as a settings-driven operation.

Under the existing reconcile lock and `MemoryModule.destructive_lifecycle()`:

1. Fence new claims and definitively join in-process Memory work. If quiescence
   cannot be proven, fail without touching the provider root. There is no force
   bypass.
2. Stop the supervised sidecar and prove the Avibe-owned process group is gone.
3. Evaluate the existing strict data-state check. If it proves the state empty,
   return the synthetic result `completed_empty`, skip the rebuild child, and stay
   on the new destructive-cutover path. Do not call ordinary reconcile: its
   endpoint preflight and rollback semantics protect a still-healthy old sidecar,
   which is no longer the authority after confirmation. If the check is
   indeterminate because a root or queue surface is unsafe/unreadable, fail
   closed without launching destructive work.
4. Only when the strict check positively reports data, launch the exact
   pinned-artifact command above with a bounded timeout. Do not persist or parse
   stdout/stderr.
5. Extend the existing managed-child ownership record with a role:
   `sidecar|cascade_rebuild`. A missing role means a legacy sidecar and uses the
   existing legacy argv/socket/root/uid classifier without requiring a role
   environment variable. New role-bearing children require an exact role-specific
   argv predicate plus same uid, `EVEROS_ROOT`, and
   `AVIBE_MEMORY_CHILD_ROLE`. Unknown or unverifiable roles fail closed. This is
   one role-aware owner, not a parallel supervisor.
6. Interpret child results as `completed` (0), `root_busy` (3), `interrupted`
   (130/cancellation), `timed_out`, or `failed`. Together with the no-child
   `completed_empty` result, this is a closed result set. Every
   timeout/cancellation path terminates and reaps the owned process group before
   releasing ownership.
7. Only `completed` or `completed_empty` proves enough to settle
   `embedding_change_pending`. Clear the marker durably before attempting
   candidate activation. Start the candidate
   sidecar and resume claims when Memory is enabled, without reusing
   the ordinary healthy-replacement endpoint probe: after destructive cutover
   there is no old sidecar to preserve, and that probe would prevent the
   documented degraded startup during an endpoint outage. Structural
   config/artifact validation still happens before destructive work. When the
   persisted candidate is disabled, complete the rebuild and settle the marker
   but leave claims and the sidecar disabled. Read normal health after an enabled
   startup. A false cascade health verdict or non-empty reasons produce an
   operational degradation; failed-row counts produce a separate data-quality
   warning. Neither is a rebuild failure or a fabricated vector count, and a
   transient `pending` count remains telemetry rather than an immediate alert.
   If sidecar startup then fails, the rebuild remains settled and ordinary
   supervisor backoff/Restart engine is the next rung; do not recreate the marker
   or destroy the new projections again.
8. On every other result keep the marker, claims fence, and sidecar-down state.
   Exit 3 is reported as root busy and never points directly to Factory reset.

The ownership guarantee covers Avibe-managed children. The provider root is
private and mode 0700, but Avibe cannot exclude a separately launched same-root
EverOS process across EverOS's lock-probe race.

### Boot and retry semantics

- A pending marker is a vector-space fence whether Memory is enabled or disabled.
  Boot must reconcile managed child ownership before returning, keep claims
  fenced, and never start the ordinary sidecar while the marker remains. For an
  absent or corrupt record, process discovery recognizes a rebuild child only by
  same uid, exact rebuild argv, exact `EVEROS_ROOT`, and the exact role
  environment, then reconstructs ownership and reaps its entire group. Ambiguous
  or unverifiable candidates fail closed.
- If boot cannot prove the recorded or discovered child group dead, it retains
  the record where available and the marker, starts neither worker nor sidecar,
  and exposes `memory_rebuild_failed` plus `rebuild_required` for a later retry.
- Boot does not start destructive work automatically. It exposes
  `rebuild_required`; an explicit Retry rebuild or a newly confirmed settings
  save schedules the retained operation from the persisted candidate.
- A disabled candidate may be rebuilt explicitly so the marker can settle, but
  successful completion remains disabled and starts no sidecar.
- Failure, cancellation, timeout, or root-busy leaves the same candidate and
  marker intact. A later retry reruns EverOS's convergent rebuild command.

## Factory reset

Factory reset is a Controller-owned replacement of the whole mutable Memory
aggregate, not a method on possibly corrupt stores or maintenance journals.

1. Reuse Clear's CSRF/user identity and signed internal user-key chain. Accept
   only the exact body `{"confirm": true}`.
2. Validate the active pinned artifact before any data deletion. If it is
   invalid, fail without deletion and direct the user to the existing Repair
   runtime action. Do not combine artifact installation and data reset into one
   opaque operation.
3. Persist `embedding_change_pending=true`, then retire the old Runtime: stop and
   join workers, maintenance tasks, the sidecar/rebuild child, and any recorded
   orphan. If owned-tree death cannot be proved, fail with `data_deleted=false`.
4. Through the confined filesystem boundary, delete exactly
   `<effective_home>/memory` and `<effective_home>/state/memory`. This includes
   provider markdown/index/system state, queue, attachments, call log, ownership
   record, clear/restore journals, snapshots, and backups. The artifact under
   `<effective_home>/runtime/memory` and persisted settings are retained.
5. Construct a fresh `MemoryRuntime` and publish it atomically through the
   Controller, but do not call ordinary reconcile while the marker is set. Use a
   dedicated factory-reset cutover under the Controller operation gate: the empty
   new roots are already the authority, so an enabled candidate starts and waits
   for ordinary readiness while the marker and claims fence remain closed; a
   disabled candidate starts no sidecar and settles as disabled.
6. Clear the durable marker and open claims only after that fresh cutover settles.
   If activation fails after deletion, return `data_deleted=true`, keep the
   marker and claims fence, and expose Retry/repair rather than claiming the reset
   preserved data.

All mutating Controller entry points must use the same operation gate or resolve
the current runtime only after acquiring it, so a stale operation cannot write
through an object retired by reset.

The guarantee is intentionally bounded: Factory reset can recover from any
state Avibe wrote inside the two mutable roots, provided the OS permits deletion,
the artifact is valid/installable, configured providers satisfy ordinary startup
requirements, and no external same-root process is racing Avibe.

## API and UI

- New error codes: `memory_embedding_rebuild_required`,
  `memory_rebuild_failed`, `memory_factory_reset_failed`, and
  `memory_operation_in_progress`.
- `POST /api/memory/runtime/rebuild` accepts exact `{"confirm": true}` and uses
  signed user proof internally. It has no config or force fields.
- `POST /api/memory/runtime/factory-reset` has the same exact confirmation and
  authentication shape.
- Memory settings removes the data-exists edit lock from embedding identity
  fields. The first identity-changing save always prompts; confirmation resends
  the unchanged draft. Settings GET exposes `rebuild_required` for crash retry.
- Memory settings adds one Rebuild index action beside Clear all. Rebuild failure
  offers Retry; there is no Force restart.
- The Dependencies Memory runtime action keeps artifact Repair as its default and
  exposes Factory reset as a separately confirmed destructive action.
- Existing status/processing-record UI distinguishes an operational warning
  (`cascade.healthy=false` or reasons present) from a data-quality warning
  (`failed_retryable` or `failed_permanent` is nonzero), even when the top-level
  health status is `ok`. A transient `pending` count alone does not turn the
  badge red.
- All copy lives in the existing English/Chinese i18n catalogs.

## Tests

Required focused coverage:

1. Unconfirmed identity change does not save; confirmation saves candidate and
   marker before scheduling; failure keeps both. API-key-only behavior remains
   ordinary without a marker, while a pending marker accepts the credential into
   the candidate without reconciling or clearing the fence.
2. Empty fast path launches no child. Non-empty rebuild fences claims, stops the
   sidecar first, launches the exact argv/env, and never overlaps owned children.
3. Exit 0/3/130/nonzero/timeout/cancellation map to typed results; cleanup proves
   the process group is gone. Only exit 0 or a strictly proven empty state settles
   the marker; an indeterminate state launches no CLI and remains fenced.
4. Ownership compatibility covers a legacy no-role record with a legacy no-role
   child environment, exact rebuild roles, absent/corrupt records with process
   discovery, wrong argv/root/uid/role, orphan reaping, ambiguous discovery, and
   surviving groups.
5. Rebuild exit 0 followed by operational cascade failure versus failed-row
   backlog surfaces the two distinct warnings; tests do not assert keyword
   completeness or parse CLI output. A post-settlement sidecar start failure
   leaves the marker clear and enters ordinary restart recovery.
6. Boot with a pending marker reaps a sidecar or rebuild orphan, starts neither
   worker nor sidecar, and exposes Retry. Enabled and disabled retries both rebuild;
   only the enabled case starts a sidecar after completion.
7. Reset corruption matrix covers a corrupt queue DB, mangled provider root,
   unreadable clear/restore journal, sticky marker, and supervisor-down state.
   It verifies both mutable roots are removed and a fresh Runtime is published.
8. Reset refuses deletion when retirement/artifact validation fails and reports
   whether data was deleted when later activation fails.
9. Internal/UI routes enforce CSRF, signed user proof, exact confirmation bodies,
   running-operation conflicts, retained-task cancellation behavior, failed-state
   replacement on Retry, and no secret fields in operation payloads.
10. UI tests cover draft retention, confirmation replay of the same patch,
   operation polling, disabled concurrent actions, retry, cascade degradation,
   and the destructive reset disclosure.

All process and filesystem tests use test-owned homes, fake artifact interpreters,
and no network or local Avibe service.

## Delivery sequence

### PR A - Rebuild-and-Apply (first priority)

- [ ] Correct the settings transaction and durable candidate semantics.
- [ ] Add the role-aware one-shot EverOS rebuild child.
- [ ] Require a complete embedding target for destructive work and verify the
      pinned CLI module at artifact admission.
- [ ] Add the Controller-owned retained rebuild operation and status projection.
- [ ] Add authenticated rebuild routes, Memory UI confirmation/Retry, and i18n.
- [ ] Add focused settings, Runtime, ownership, route, degraded-health, and UI
      tests.
- [ ] Update `docs/MEMORY.md` and `docs/MEMORY_ZH.md` for rebuild behavior.
- [ ] Before release, verify the existing workflow makes all three managed-runtime
      artifacts downloadable before it publishes the generated-manifest-bearing
      package; this is a release gate, not a unit-test prerequisite or a manual
      source-manifest edit.

### PR B - Factory reset recovery floor

- [ ] Extend the same Controller operation slot with Factory reset.
- [ ] Serialize mutating Memory entry points across fresh-Runtime replacement.
- [ ] Confined-delete the two mutable roots and publish/reconcile a fresh Runtime.
- [ ] Add authenticated reset routes, Dependencies UI disclosure, and i18n.
- [ ] Add the corruption matrix, pre/post-deletion result tests, route/UI tests,
      and Factory reset user documentation.

Later PRs are tracked in the reuse audit: explicit live projection repair,
snapshot-payload research, durable EverOS delivery receipts/batching, recorder
seams, modality drift checks, and upstream idle flush.
