# Dispatch-Time Message Metadata

## Contract

User content, sender identity, and execution context have separate owners. Message
snapshots remain the source of truth for display, Memory, analysis, and training.
Delivery retains undecorated dispatch content for durable admission and recovery.
Backend adapters render one shared metadata format immediately before writing a
native input, after waiting for runtime readiness.

`[Now: ...]` is the local wall-clock time at that native dispatch, including the
UTC offset. It is not the sender's timestamp or the admission timestamp. The
global time and identity switches apply when rendering. No formatted prefix is
written back into canonical content. Native transcripts and reconciliation
receipts may contain the actual rendered input.

Every input no person typed starts with one `[Harness: <source>]` line, before
the clock line and independent of both switches, so a model can never read a
Watch prompt, a Task, another Agent's message, or a callback as the user's own
words. The source comes from the dispatch provenance the scheduler records
(`task_trigger_kind` and the run's `source_kind`), never from the text; every id
and name in it is one sanitized line (every line break and control character
becomes a space), and a definition name is quoted. A person's input never carries
it, and one whose text begins with `[Harness:` always carries the person's
identity line, even with the identity switch off, so the marker cannot be forged. A merged batch of queued Harness inputs is labelled once when every Delivery
has the same source, else source by source in the batch's FIFO order, which is
the order of its `---`-separated bodies, so no body is attributed to another
Delivery's sender. Acceptance drops each Delivery's own snapshot, so the merged
Message keeps every merged Harness input's sender facts (`merged_harness_sources`)
and a restart that recovers the batch renders the same line. The sources are:

| Input | Line |
| --- | --- |
| Watch follow-up | `[Harness: Watch <watch id> "<name>"]` |
| Scheduled Task message | `[Harness: Task <task id> "<name>"]` |
| Task on-failure Agent turn | `[Harness: Task <task id> "<name>" on-failure]` |
| Webhook | `[Harness: webhook <definition id> "<name>"]` |
| `vibe agent run` from an Agent shell | `[Harness: agent run <run id> from #<caller session>]` |
| `vibe agent run` outside an Agent shell | `[Harness: agent run <run id> from the CLI]` |
| Callback of a finished run | `[Harness: callback of run <run id> from #<run's session>]` |
| Vault request outcome | `[Harness: Vault request <request id>]` |
| `hook_send` run queued by a release before `vibe hook send` became an Agent run | `[Harness: hook send <run id>]` |
| No recoverable provenance | `[Harness: background input]` |

This line replaces the released `From: #<session>` line, which named only the
source Session of agent runs and callbacks. The Harness routing block of the
injected system prompt says what the line means.

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

The second findings-bearing head, `d7635351c`, repeated the legacy-parser class:
one finding showed that a greedy optional identity match could consume the start
of the immutable user body when only a timestamp had actually been added. The
two-head circuit breaker was evaluated before another patch. Full inventory:
`20a7d050b` had three findings (config reload, legacy body formats, pre-write
steering settlement); `d7635351c` had one (legacy prefix-boundary selection).
The first and third classes remain resolved. The orchestrator decision is to retain the
data model and close the parser's finite protocol: test every released time/user
switch combination against bodies beginning with those same prefix shapes, and
accept a prefix candidate only when its remainder matches the immutable body.
This replaces greedy boundary selection; it does not weaken body validation or
expand historical rewriting. Native dispatch and persistence owners stay unchanged.
