# Projection validation without discarded copies

## Goal and scope

Append validation must reject the same malformed transcript/candidate before persistence without allocating a detached projection which it immediately discards. This is a focused follow-up to bounded runtime transcript loading, not removal of public projection copies.

Only `core/agent_core/harness/projection.py` changes production behavior. The loop, transcript store, runtime closure, job polling, provider serialization and reasoning signatures are unchanged.

## Ownership contract

`_borrowed_context_view` performs the existing ordering, shape validation, call/result association, edit validation, checkpoint selection and kept-input validation. It builds a temporary view without changing any input row, payload, state or message. Its result is not a public read-only type: it contains mutable objects owned by the input rows. There are exactly two intended uses:

1. `validate_message_append` adds its transient candidate, builds and discards this view synchronously. It does not persist or return it.
2. `context_view` builds it and deep-copies the units and state before returning. `project` continues to use this public boundary and separately detaches rehydrated messages. `open_tool_calls` continues to detach pending row/call pairs.

Do not add a public copy-disable option or pass the borrowed graph to a hook, provider, recovery callback or runtime capsule. No cross-call cache is introduced.

Public behavior remains:

- Unit entries, canonical messages, nested tool arguments, entry payloads, hosted-native payloads, hosted count maps, transient usage maps and state retain their existing detachment.
- Within one unit graph, the returned row's message and the corresponding projected message preserve their existing alias relations, except where an edit intentionally replaces the projected result.
- Rehydrated messages and recovery's open-call pairs remain detached.
- `ContextView.compaction` remains a borrowed row, as before. This change neither fixes nor broadens that existing alias; the whole ContextView is not claimed deeply detached.
- Ordering, inclusive fork cuts, checkpoints, pinned turn inputs, late recovery results, interrupted results, context edits, refusals and state are unchanged. Signed reasoning and tool calls are not rewritten.

## Acceptance tests and why they are needed

Existing tests checked projection content and some state isolation, but did not protect against exposing the private borrowed graph or allocating copies on the discarded validation path.

- A deepcopy spy at the projection boundary proves append validation creates no detached graph. This regression fails on the previous code with three deepcopy calls; it is an allocation-path assertion, not a timing threshold.
- Public-boundary mutation tests cover `context_view`, `project`, rehydrated messages and `open_tool_calls`. They mutate nested fields and verify canonical rows and fresh public outputs remain unchanged; then mutate source fields and verify earlier detached outputs remain unchanged. Usage TTL maps are checked explicitly because they are omitted from dataclass comparison and canonical serialization.
- An edited-result test distinguishes the detached canonical result row from its replacement message and pins the unchanged compaction alias.
- The existing malformed-row and mutated-argument matrices also exercise append validation, including invalid checkpoint/edit/state payloads. Candidate-specific cases cover valid input/response/late result, duplicate pending calls/results/sequences, unknown call ids and result-name mismatches, with non-mutation on both success and exception.
- Projection consumers are checked by the existing recovery, context, compaction, fork, runtime-window and transcript tests. Provider-request byte/hash equivalence belongs in the paired benchmark as well as existing semantic assertions.

## Measurement protocol and limits

Use a separately authorized isolated local regression environment, never synthetic owner-desktop load. Compare a pinned baseline and candidate with the same dependency lock, interpreter and synthetic fixture. Include a short tail and long checkpointed ancestry, varying visible tail size independently. Measure append validation separately from public `context_view`, `project`, anchor lookup and complete request preparation; the latter public paths intentionally still copy.

Use sequential baseline/candidate/baseline samples after warm-up, preserve raw timings and input/output hashes, and keep allocation instrumentation separate from the wall-time pass. Record process CPU, GC counts and probe peak RSS; probe RSS is not service RSS. Verify rows remain unchanged and accepted/rejected outcomes and provider-request serialization agree. Do not infer normal-workload GC causality from this optimization.

Before claiming performance improvement, require the validation-path difference to exceed baseline repeat noise and inspect any public-path regression. If copies discarded by validation are immaterial after bounded runtime loading, report that result rather than extending this PR to message ownership or signature changes.

## Delivery checklist

- [x] Preserve the public ownership boundary and private-only borrowed graph.
- [x] Add a regression that fails on the previous implementation for the intended reason.
- [x] Complete focused hermetic consumer tests and changed-file lint.
- [ ] Run the paired measurement in an explicitly allocated regression slot.
- [ ] Complete exact-head review and required CI when submission is authorized.

Local evidence: on the pre-change production code, the new allocation-path test fails with three deepcopy calls and the other 65 projection cases pass. With the change, the eight-file projection/recovery/context/compaction/fork/runtime/transcript selection passes 569 cases, and the loop suite passes 209. After adding one more invalid-candidate case and reverse payload/state isolation assertions, the final projection selection passes 67 cases. Changed-file Ruff and `git diff --check` pass. These are hermetic functional checks, not performance measurements; the paired regression benchmark and review/CI remain pending.
