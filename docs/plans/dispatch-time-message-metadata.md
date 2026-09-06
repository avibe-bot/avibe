# Dispatch-Time Message Metadata

## Contract

User content, sender identity, and execution context have separate owners. Message
snapshots remain the source of truth for display, Memory, analysis, and training.
Delivery retains undecorated dispatch content for durable admission and recovery.
Backend adapters render one shared metadata format immediately before writing a
native input, after waiting for runtime readiness.

`[Now: ...]` is the local wall-clock time at that native dispatch, including the
UTC offset. It is not the sender's timestamp or the admission timestamp. The
global time and identity switches apply when rendering. Harness source-session
provenance remains independent of those switches. No formatted prefix is written
back into canonical content. Native transcripts and reconciliation receipts may
contain the actual rendered input.

## Scope

- Keep metadata structured through normal starts, queued batches, and steering.
- Share rendering across Codex, Claude, and OpenCode, preserving exact native
  reconciliation evidence and original user text.
- Preserve released queued inputs without duplicating their legacy metadata.
- Assert raw Message and Memory content independently of rendered backend input.
- Preserve the existing schema, delivery ownership, and ambiguity policy.

## Validation

Focused handler, durable-delivery, and backend tests must prove that waiting does
not freeze the clock, every real input path receives configured metadata, and
canonical content stays unchanged. Test sender/source restoration, merged input,
configuration switches, native reconciliation, and released pending deliveries.
No developer runtime restart is required for these tests.

## Review Decisions

At head `20a7d050b`, the first findings-bearing review identified three independent
boundary gaps: cached display switches, incomplete released attachment-error
formats, and steering preparation failures after durable attempt admission.
Keep the existing model and close each gap at its owner: refresh the controller's
mtime-guarded config at the shared render boundary, recognize released wire titles
and empty-body formats without rewriting content, and settle pre-write failures
as definitive refusals through the existing batch fallback. Only errors after
entering the backend adapter retain the conservative unknown-outcome policy.
