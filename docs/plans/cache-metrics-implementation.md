# Vibey cache metrics — implementation contract (Delivery plan PR 3b)

Owner: `system-prompt-cache-tiers.md`, Metrics. Base: merged PR #3117. Only Vibey's loop and its diagnostics; no prompt/behavior change for native backends, no migration, deployment, or cache-policy retuning.

## Invariants and boundaries

- Capture request admission time, assumed route lifetime, unchanged-prefix expectation and causal facts in the one model-attempt pipeline. Persist `cache` beside the response payload's request/message and on non-context attempt records; never duplicate billed usage. Record TTL writes as a decomposition of existing total writes, not extra tokens.
- Cache clocks are keyed by served provider/model and start time, distinct from the existing C-9 response-end clearing clock. A restart restores committed response facts and audited attempts. No metrics read or write blocks the controller loop.
- Persist only hashes and token estimates for prefix comparison, never a second copy of prompts, credentials, headers, or tool content. Whole unchanged requests use their previous reported input count; partial matches use the existing C-9 estimator and are explicitly marked estimates. Unknown usage/prices/TTL breakdowns remain unknown, never zero-priced facts. A provider TTL detail without its aggregate write total is retained as an incomplete diagnostic, not promoted to a complete billable total.
- `prompt.pending` records newly detected/revised components; `prompt.applied` records reason and each component's wait. Preserve the decision's exact tiers, revisions and deadline; previews neither emit events nor write state.
- A read-only `cache_requests` TEMP view exposes facts/classification, with a labeled timestamp inference for pre-metrics rows. Full-prefix misses can be classified as provider misses only when their basis is `reported_input`; partial-prefix misses remain `estimated_miss`. A CLI `vibe data cache` report groups by route/period: idle bands, class/cause/TTL writes, lifetime what-if costs, and tier freshness. Reads reuse guarded query access and local price data; no provider/network calls and no mutation of historical rows.
- New fields are optional at persisted boundaries; old rows load unchanged. Invalid optional diagnostics degrade to unknown without breaking a Turn. Query/report timestamps and price basis remain visible so estimates cannot be mistaken for historical bills.

## Acceptance

Tests at the accounting boundary cover warm/cold request starts (including a long response), route switches, retries, restart/fork, checkpoint and result-clearing causes, and TTL decompositions without double counting. Store contract tests cover optional cache round trips and indexed session-scoped audit recovery. Adapter tests cover prompt events/no-op previews. Isolated database/CLI tests cover every classification, timestamp-derived legacy rows, unknown rates, what-if arithmetic and freshness summaries. Existing C-9, wire conversion, snapshot, fork and shared-prompt tests guard intentional non-changes.
