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
  rejects the turn.
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
The visible card is gated until the Slack closed-loop slice lands. Status continues
to project EverOS
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
