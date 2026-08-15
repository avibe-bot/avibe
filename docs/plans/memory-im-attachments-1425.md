# IM attachment capture via EverOS multimodal ingest

Issue: [#1425](https://github.com/avibe-bot/avibe/issues/1425)

## Goal

Let an eligible attachment sent in a bound one-to-one IM conversation enter the
existing durable Memory capture pipeline without changing ordinary Agent file
delivery. Avibe acquires each native attachment once, pins only admitted content
inside the existing private Memory bundle root, and sends the same closed
`ContentItem` kinds that the Workbench path already uses to EverOS.

This is a multi-platform, provider-neutral capability. Platform adapters expose
bounded acquisition primitives; shared core owns admission, lifetime, limits,
redaction, and capture behavior.

## Locked product decisions

- IM attachment capture requires an explicit, complete
  `memory.processing.multimodal` endpoint. Workbench keeps the released implicit
  main-LLM inheritance for one compatibility cycle; loading an old config must
  not copy or persist that inherited API key.
- A complete multimodal endpoint is the opt-in. There is no second attachment
  toggle.
- A mixed turn degrades per attachment. Eligible text and every valid attachment
  survive independently; one unsupported, oversized, or failed download never
  rejects the turn. If a selected attachment later fails descriptor/hash
  verification during durable pinning, the same capture is retried once as
  text-only; an attachment-only turn retains the closed pin failure.
- The first release keeps the existing Avibe allowlist in
  `core/memory/modality.py`: bitmap images, PDF, supported audio, direct text,
  CSV, VTT, HTML, and EML. Office/iWork/ODF/RTF, SVG, and video remain excluded
  even where EverOS can parse them.
- The Memory consumer alone applies the limit of 8 files, 25 MiB per file, and
  100 MiB per bundle. Ordinary Agent attachment behavior is unchanged.
- Multimodal preflight sends a tiny generated image containing no user or
  conversation data.

## Admission contract

Adapters add a literal-true native fact for an ordinary human attachment turn,
separate from `is_ordinary_text`. `core/memory/admission.py` remains the only
authority. Memory may retain an IM file only after all of these properties hold:

- Memory is enabled and explicit multimodal configuration is complete;
- platform, stable native message id, and stable session id are recognized;
- the event is a one-to-one DM from a human and is positively classified as an
  ordinary attachment shape;
- the user is bound and enabled; and
- the principal and the default v3 Memory project resolve successfully.

Missing or non-boolean facts fail closed. Groups, channels, edited, forwarded,
shared, rich, system, bot, and self-authored events remain ineligible. Admission
must complete before Memory retains a lease or performs Memory-only acquisition.
The Agent path may still use the same materialized file under its existing rules.

An attachment-only turn with no surviving attachment is skipped. A mixed turn
with no surviving attachment may still capture eligible text. Admission and
attachment failure never annotate or fail the chat turn.

## Acquisition and lifetime contract

`core/handlers/inbound_attachments.py` owns shared native-file materialization.
It calls the platform's `BaseIMClient.download_file_to_path` implementation,
uses sanitized names, removes partial files, and publishes only an opaque,
reference-counted local lease. Consumers never receive a native URL, token,
encryption material, or mutable `MessageContext` as their durable contract.
Lease files live below the fixed private
`<effective-home>/attachments/im/<opaque-lease-id>/` root. A rejected or
unclaimed batch is removed after its final reference; ordinary Agent delivery
adopts its existing local-file lifetime, while a retained Memory reference can
finish pinning first. Empty and failed batches are never preserved by adoption.
The materializer keeps no-follow root and lease descriptors open for the lease
lifetime. It pre-creates each partial file with descriptor-relative
`O_EXCL | O_NOFOLLOW`, passes a duplicate file descriptor to bounded platform
writers, and performs normalization, publication, and cleanup relative to the
verified lease descriptor. Replacing the directory entry during materialization
fails closed and cannot redirect writes outside the private lease.
Each published record binds its original device/inode. Memory selection and
durable pinning reopen that record relative to a duplicated retained lease
descriptor and reject inode mismatches. The record also binds a materialization
SHA-256 that durable pinning verifies while copying, so neither public pathname
replacement nor same-inode modification can substitute bytes after acquisition.

The message handler materializes before scheduling Memory capture. Agent delivery
and Memory retain independent lease references, so Agent cleanup, retry, or
durable-delivery failure cannot race the Memory pin. Downloads are sequential or
at most two concurrent and keep the existing 30-second per-file timeout.

Slack, Discord, and Feishu/Lark use their existing authenticated streaming path
downloads with declared, response-header, and streamed-byte limits. Telegram is
changed to a bounded-to-disk Bot API download; it must not buffer the entire file
before checking the limit. WeChat checks declared size before acquisition and
bounds ciphertext/decrypted output while writing; a post-download-only check is
not sufficient. The shared materializer normalizes non-negative integer declared
sizes and rejects an over-limit item before creating a partial or invoking any
platform adapter.

`core/memory/im_attachments.py` accepts only shared-materializer leases. It checks
declared size before retaining a Memory consumer and checks final size, MIME,
extension, and magic after acquisition through the closed modality table. It
produces immutable `CaptureAttachment` values for surviving files. Missing or
non-string native MIME metadata normalizes to `application/octet-stream` at this
shared boundary. Text-like formats stream their complete bounded file through
incremental UTF-8 and NUL validation; checking only the leading magic sample is
insufficient. M4A admission parses brands only from the declared,
four-byte-aligned `ftyp` brand table; brand-like bytes in later ISO-BMFF boxes do
not admit excluded video.

`core/memory/attachments.py` accepts either the fixed Workbench upload root or a
fixed leased-IM source handle. Both paths keep no-follow opens, owner and mode
checks, copy-time size enforcement, hashing, atomic bundle publication, existing
free-disk checks, and crash reconciliation. Source kind is not persisted; bundle
manifest v1 and the store schema do not change.

## Provider and degradation contract

Prepared captures use the existing path:

```text
MemoryModule -> durable outbox -> coordinator -> EverOSPort.add(ContentItem[]) -> flush
```

The outbox and logs may contain only sanitized display name, closed content kind
and extension, byte count, bundle-relative metadata, keyed source digests, and
closed skip reasons. They must not contain native URLs, credentials, encryption
material, raw attachment bytes, absolute source paths, filenames supplied for
telemetry, or adapter exception text.

Absent multimodal configuration or parser capability skips IM attachments before
pinning, enqueueing, or calling `/add`. Each rejected item degrades independently.
Avibe emits one scrub-safe `memory_attachment_capture_skipped` event per turn with
platform, count, and one closed reason only.

Configured adds retain the existing retry and ambiguity semantics. Deterministic
EverOS `UNSUPPORTED_FORMAT` and `CAPABILITY_UNAVAILABLE` attachment outcomes are
terminal, release their bundle, and cannot open an endless processing fault.
Transient and ambiguous outcomes retain the bundle exactly as the current capture
coordinator does.

The sidecar gains no route. Its existing exact `POST /api/v2/memory/add` envelope
remains unchanged; `_valid_workbench_attachment` becomes pinned-attachment
validation without accepting platform URLs or widening the confined provider root.

## Configuration, preflight, and status

Add optional all-or-none values at:

```text
memory.processing.multimodal.base_url
memory.processing.multimodal.model
memory.processing.multimodal.api_key
```

The block follows the released rerank behavior: an absent old key loads and saves
without shape churn, an empty block normalizes to absent, partial API input is
rejected, and a malformed persisted optional block disables only multimodal with a
warning so startup continues. API keys remain write-only.

Only allowlisted child environment variables carry
`EVEROS_MULTIMODAL__BASE_URL`, `EVEROS_MULTIMODAL__MODEL`, and
`EVEROS_MULTIMODAL__API_KEY`. Generated TOML contains the confined attachment root
and `file_uri_max_bytes = 26214400`, but no credential. Saving a changed endpoint
runs bounded preflight and rolling sidecar reconciliation; it does not rebuild
embeddings or restart Avibe.

`MemoryPreflightDiagnostic.side` adds `multimodal`. Its real compatibility probe is
a generated 64x64 PNG request with no user data. Missing optional configuration does
not block Memory enablement. Insight-reader and preflight redaction include the
independent multimodal URL and key; a configured parse may produce a bounded,
redacted `multimodal_llm` record, while an unconfigured skip produces no provider
add or call-log row.

The settings API carries the optional Multimodal endpoint with the same write-only-key
and clear semantics as rerank. Either optional endpoint can be cleared while Memory
remains enabled; required LLM and embedding keys retain the enabled-state clear gate.
The Slack closed-loop slice flips the implementation stage gate and reveals the
visible card. Status continues to project EverOS
`multimodal_llm`, `parser`, and `disabled_features` and adds a concise attachment
capture availability line. Configuration remains UI-only; no CLI config flags are
added.

### Review-loop closure decision

After review of head `cf66a808`, the orchestrator approved one whole-model closure
for the repeated multimodal health-probe root-cause class. The recurrence came from
environment provenance ambiguity: the same `EVEROS_MULTIMODAL__*` values can mean
either an explicit IM opt-in or the one-cycle Workbench compatibility fallback.

- An Avibe-private marker is present only for a complete explicit multimodal
  triple. The probe child checks multimodal only when that marker is present;
  the normal sidecar keeps the fallback variables for Workbench compatibility.
- Fixed synthetic provider health checks initially ran fully concurrently under a
  fixed 20-second child deadline. The complete model below supersedes that boundary:
  endpoints sharing provider credentials must not overlap, so the child deadline is
  derived from the largest serialized group instead of assuming one concurrent wave.
- Attachment capture reports ready only from a current available runtime health
  observation. Cached stale health and unavailable health report unavailable;
  absent explicit configuration still reports not configured.

This closure adds no persistent field, sidecar route, or provider payload shape.

### Processing health probe model

This is the complete contract for settings compatibility checks, child processing
health, and attachment readiness. It closes root-cause classes A-E, which were all
gaps in the probe model rather than independent endpoint bugs.

- **Endpoints and gating.** Saving processing settings runs the existing bounded
  compatibility preflight for LLM and embedding plus each complete optional rerank
  or multimodal endpoint. Runtime processing health runs fixed authenticated probes
  for LLM and embedding, complete rerank, and multimodal only when the private
  `AVIBE_MEMORY_MULTIMODAL_EXPLICIT=1` marker proves the persisted multimodal triple
  was explicit. The normal sidecar may still receive inherited
  `EVEROS_MULTIMODAL__*` values for the one-cycle Workbench fallback, but those values
  alone never admit a multimodal health probe.
- **Grouping and concurrency.** Runtime health groups endpoints by the exact resolved
  `(base_url, credential identity)` pair. Requests within one group run serially so a
  shared provider or key is not subjected to overlapping health traffic. Different
  groups run concurrently. The credential identity is compared only in private
  process memory and is never logged or projected.
- **Deadlines.** Each runtime provider request remains bounded at 8 seconds. The
  parent child deadline is `8 seconds * largest provider group size + 2 seconds`,
  structurally capped by the four endpoint model at 34 seconds. Independent groups
  therefore receive 10 seconds; the worst case with all four endpoints in one group
  receives 34 seconds. This bounded derived deadline supersedes the earlier fixed
  20-second composition because serialization is now required to avoid provider
  concurrency and rate-limit failures. Settings compatibility preflight remains a
  separate sequential flow with its existing 5-second per-endpoint bound.
- **Synthetic payload.** Text, embedding, and rerank probes use fixed literals only.
  Multimodal checks use the same generated opaque 64x64 RGBA PNG (153 bytes, no user
  data) in a PNG data URI plus the fixed `Reply with OK.` prompt. Tests pin its PNG
  dimensions and exact byte digest.
- **Readiness and freshness.** Attachment capture can report `ready` only when an
  explicit multimodal config exists, the current runtime source status is
  `available`, and the projected `multimodal_llm` and parser capabilities are
  enabled. An absent config reports `not_configured`; stale or unavailable runtime
  health reports `unavailable` even when cached capability values were previously
  healthy.

### Slack activation contract

Delivery slice 4 makes Slack the reference platform and flips
`IM_ATTACHMENT_CAPTURE_AVAILABLE`. The platform allowlist remains closed to Slack
until slice 5 adds contract evidence for the other four adapters.

- Slack `message` and `app_mention` events publish a separate literal-true
  `is_ordinary_attachment` fact only for a native, human `file_share` shape with
  plain composer blocks. The fact does not reuse or weaken ordinary-text
  classification, and edited, forwarded, bot, rich, system, and shared shapes
  still fail closed.
- The message handler performs the existing shared Agent materialization first.
  It asks admission whether Memory may retain that materialized batch, then gives
  Memory an independent lease reference. A retain or scheduling failure is
  best-effort and cannot reject the Agent turn.
- The retained reference lives until the asynchronous Memory capture finishes.
  The Memory module pins through the descriptor-backed lease before enqueueing;
  the ordinary Agent consumer independently adopts its source files. This keeps
  one native download while preventing Agent cleanup or durable-delivery failure
  from racing the Memory pin.
- Capture reads current attachment readiness only after the closed DM admission
  gates pass. `not_configured` and `unavailable` retain eligible text but produce
  no attachment pin, provider attachment, or call-log row. Attachment-only turns
  with no surviving item are skipped.
- Per-attachment selection uses the closed format and limit table. A rejected
  sibling does not remove eligible text or valid siblings. Skip telemetry is one
  structured event containing only platform, total rejected count, and a closed
  reason. When one turn has different rejection classes, the reason is the fixed
  `mixed_rejections` value rather than attachment-specific detail.
- The hermetic Slack scenario drives the shared materializer, admission, durable
  pin/outbox worker, fake EverOS add/flush, redacted multimodal call log, and
  `vibe memory search`. It proves a single adapter download and zero temp or
  Memory-bundle residue after successful processing.

### Capture call-site contract

Memory capture stays best effort and must not become part of user-visible Agent
dispatch latency. The call site therefore branches by turn shape instead of moving
all capture work to one late stage.

- Text-only human turns use the original early capture path immediately after the
  stable base session is resolved and before Agent routing. Attachment turns alone
  use the delayed path, and the only legal reason for that delay is waiting for the
  shared materializer to produce its immutable descriptor-backed lease.
- The canonical Memory session anchor is copied from `base_session_id` immediately
  after session resolution, before any subagent or routing-agent namespace can
  rewrite the Agent session id. Both the early capture and the delayed lifecycle
  fence/capture request receive that saved canonical value.
- **Bounded-wait invariant.** No deadline-bound provider or subprocess read may
  occur in a span that blocks any bounded waiter. The bounded waiters here include
  both the user-visible Agent dispatch path and `/new`'s five-second lifecycle
  budget. Attachment eligibility must therefore be decided from local facts before
  any health read: platform and message shape, binding/config completeness, and the
  explicit multimodal configuration generation. Moving a remote read to a different
  call site does not satisfy this invariant if either bounded waiter still depends
  on its completion.
- Multimodal opt-in is an authoritative, generation-bound fact. The synchronous
  segment snapshots the stable Runtime configuration generation that proves an
  explicit endpoint and carries it on the immutable capture request. Immediately
  before enqueue, the replacement gate compares that generation with the current
  Runtime generation; absence or mismatch fails closed for attachments, including
  an opt-out or endpoint replacement that completed while readiness was being read.
  A missing IM generation projects `not_configured` immediately and retains eligible
  text without performing a live health read. Workbench alone keeps its one-cycle
  implicit compatibility path. Eligible text remains independently best effort.
- **Reservation boundary.** While holding per-session `SessionTurn` lifecycle
  admission, the handler performs only an O(1), non-blocking, local registration of
  an exact-session Memory capture ticket. Registration records FIFO order but never
  waits for an earlier capture and never reads a provider or subprocess. Once the
  ticket and task ownership are established, the handler releases `SessionTurn`
  immediately and continues durable Agent admission.
- **Concurrent captures.** Tickets for the same canonical session execute in
  registration order. A background task waits for its predecessor only after
  `SessionTurn` has been released, then performs readiness, attachment selection,
  bundle pin/copy, and queue commit under the existing exact-session execution
  fence. Tickets for different canonical sessions have independent tails and remain
  concurrent. Order is represented by ticket data, never by holding the dispatch
  lock while slow work runs.
- **Lifecycle barrier.** `/new` first acquires `SessionTurn`, then registers a
  lifecycle barrier behind the current exact-session ticket tail and waits for that
  snapshot with the existing bounded lifecycle deadline. Holding `SessionTurn`
  prevents later turns from registering while the barrier drains, so every ticket
  before the reset is included and the wait converges; captures admitted after the
  reset queue behind the barrier. The reset and final flush therefore cannot pass an
  older registered capture, while Agent dispatch never waits for capture execution.
- **Single ownership, in process.** Before task tracking, the handler owns the
  reservation and any retained materializer lease. Successful scheduling transfers
  both to the background task; every exception, cancellation, or scheduling failure
  completes the reservation and releases the retained lease together. Once tracked,
  the task completes its ticket in `finally` and its done callback releases the
  lease. At shutdown, Controller closes the loop-owned capture-registration gate and
  then cancels and joins every tracked capture in the same event-loop coroutine,
  without the generic five-second cleanup cutoff. The handler rechecks the gate
  after any `SessionTurn` wait and immediately before the no-`await` reservation,
  retain, and task-registration segment. Once closed, that atomic segment cannot
  produce another reservation, lease, or task, so the sweep operates on a closed set
  and converges independently of every IM client's shutdown order. This
  process-local cleanup is best effort: no exit path may have two owners, but an
  abrupt process termination can bypass all callbacks.
- **Authoritative durable cleanup.** Startup recovery is the final correctness
  guarantee for every termination path, including crashes and `SIGKILL`.
  `MemoryCoordinator.recover_after_boot()` takes the database attachment-reference
  snapshot inside the admission fence and `_reconcile_attachments()` invokes the
  idempotent `AttachmentStore.reconcile(referenced, releasing)` operation. It removes
  all staging remnants and every bundle not referenced by the database. Therefore an
  attachment lease or bundle abandoned before queue adoption is bounded to the
  process lifetime plus the next Memory startup recovery, rather than permanently
  retained.

## Scenario contract

The canonical capability is `memory_im_attachment_capture` under
`tests/scenarios/memory_im_attachment_capture/`:

- `MEMORY-IM-ATTACH-001`: a bound Slack DM image/PDF is downloaded once, pinned,
  added, flushed, extracted, and retrievable through `vibe memory search`;
- `MEMORY-IM-ATTACH-002`: group and unbound denial performs no Memory acquisition;
- `MEMORY-IM-ATTACH-003`: absent multimodal configuration captures eligible text
  only and creates no attachment provider/call-log activity; and
- `MEMORY-IM-ATTACH-004`: count, per-file, total-size, unsupported-type, and partial
  download failures preserve valid siblings and leave no temp or bundle leak.

The closed-loop harness uses a stub platform client, a real test-owned bundle path,
and local fake OpenAI-compatible providers. It must not use live credentials,
running Avibe services, real user paths, or production state.

## Delivery slices

1. Contract and scenario catalog, including the stale Workbench-copy and IM
   non-goal corrections in `memory-plugin-system.md`.
2. Optional multimodal config, child environment, preflight/redaction, UI, and
   status. IM capture stays gated: `IM_ATTACHMENT_CAPTURE_AVAILABLE` remains false,
   the settings response hides the card, and configured capture cannot report
   `ready`; absent configuration retains the locked `not_configured` projection.
3. Shared leased materializer, bounded Telegram/WeChat acquisition, and pin-source
   generalization. IM capture stays gated.
4. Attachment classification/admission and the Slack closed loop with call-log
   proof. This slice flips `IM_ATTACHMENT_CAPTURE_AVAILABLE` only after the capture
   path and its closed-loop evidence land, revealing the endpoint card and enabling
   health-derived readiness without adding a user-facing toggle.
5. Discord, Telegram, Feishu/Lark, and WeChat enablement and contract tests, plus
   final user documentation and manual verification matrix.

Each PR is based on the latest merged `master`; there are no stacked PRs. Shared
files with the #1424 lane receive only additive, narrowly scoped changes. If that
lane lands first, this work rebases and re-runs the complete review loop before the
next slice.

## Residual manual checks

Hermetic tests prove the product contracts without live platform credentials.
After all five slices merge, the orchestrator's integration pass should verify one
eligible image and one rejected file on every configured IM platform in the local
Incus regression environment, preserving accumulated product state.
