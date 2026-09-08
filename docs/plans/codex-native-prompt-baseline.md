# Codex native prompt baseline

## Decision and scope

Restore a complete Avibe prompt baseline through native `developerInstructions`
at thread creation, fork, and resume. Keep the existing durable, at-most-once
developer-message injection path for changes on already-loaded threads.
This is an explicitly limited compatibility mode, not lossless hot replacement.
It does not depend on an unmerged Codex change.

The upstream contribution policy at `openai/codex@5a65fd87d842` accepts issues,
not external code PRs. Prepare a minimal reproduction and source analysis for
that supported channel; do not submit an unsolicited code PR.

## Boundary contract

- Render Avibe instructions once at the dispatch boundary, before a new native
  thread needs them. Reuse those exact bytes for native configuration and any
  necessary history injection, including transport recovery.
- Preserve independently configured Codex developer instructions when setting
  the native baseline. Do not change model, reasoning, permissions, user/project
  instructions, or shared production/export/Studio composition.
- On a genuinely new thread, native configuration is enough: record the current
  Avibe fingerprint using the existing durable marker and do not also append it.
- On resume or fork, retain the last model-visible fingerprint. Supplying a new
  native configuration does not prove that restored history already contains it.
  Changed Avibe instructions still use the existing injection path.
- Unchanged instructions do not generate additional injected messages on normal
  turns or restart. Ambiguous injection, persistence failure, legacy collaboration
  markers, and fork ownership retain their existing recovery semantics.
- Do not call resume to refresh prompts on a cached thread, force compaction,
  unload a thread, restart a transport, or terminate background resources merely
  because prompt content changes.

## Known-by-design ledger

1. Injection changes model-visible history, not the loaded native configuration.
   Automatic compaction reconstructs the last native baseline. A newer injected
   overlay can be retained, truncated, or evicted under the provider's policy.
   Its loss can expose the older baseline; this change does not guarantee that
   revoked prompt wording can never reappear.
2. Resume overrides take effect in native configuration only on a cold load.
   Existing history can retain the old baseline until context reconstruction.
   No warm-resume acknowledgement is interpreted as successful prompt refresh.
3. Cold promotion and later compaction can leave both a native copy and retained
   injected copies. Historical cleanup requires upstream lifecycle support; do
   not rewrite rollouts or silently discard other developer messages.
   Loss of the initial durable delivery marker can likewise require an injected
   copy on recovery; native creation and application storage are not atomic.
4. There is no new marker schema or strategy, deployment, Studio source change,
   saved-draft modification, or cross-backend prompt change.

## Acceptance and evidence

- Unit coverage: native creation, resume, fork, one render, preservation of
  configured instructions, durable marker recovery, unchanged-state deduplication,
  and existing legacy/ambiguous-outcome cases.
- Native contract: an isolated Codex home and loopback Responses server, with no
  model credentials. Inspect actual outbound requests before/after changes,
  process restart, ordinary compaction, and automatic compaction under more than
  64K retained-history pressure. Include non-ASCII prompt text.
- The pressure case must show a complete native baseline surviving, and must
  explicitly characterize loss of a newer overlay instead of asserting a false
  latest-version guarantee.
- Run focused Codex tests and changed-file Ruff before push; require exact-head
  Codex review, all lint checks, and zero unresolved review threads before delivery.
  No existing scenario catalog owns this transport contract.

## Verified native behavior

The isolated contract passes on Codex CLI 0.153.2 with actual outbound Responses
requests, including non-ASCII instructions and separately configured native text.
The table counts complete Avibe prompt copies, not RPC acknowledgements:

| Stage | Native A | Native B | Injected B |
| --- | ---: | ---: | ---: |
| New thread and unchanged restart | 1 | 0 | 0 |
| Live update | 1 | 0 | 1 |
| Automatic compaction, ordinary retained history | 1 | 0 | 1 |
| Automatic compaction, over-budget recent user history | 1 | 0 | 0 |
| Cold resume with B, ordinary retained history | 1 | 0 | 1 |
| Subsequent compaction after cold resume | 0 | 1 | 1 |

Fork tests also verify that the first target turn receives B through injection
and later compaction reconstructs its B baseline. Existing native user preferences
remain present. These results verify transport and context composition, not model
obedience or live UI behavior.
