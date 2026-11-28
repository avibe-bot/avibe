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

## Successful request uploads

`request_uploads.successful_wires` is a JSON array with one entry per **completed Vibey adapter HTTP POST**. `body_bytes` is the length of the serialized request body at the adapter's send boundary. It excludes headers and is not a token count, an estimate of an unchanged prefix, or a measurement of what a gateway or engine subsequently sends upstream. No exact cheap unchanged-prefix byte count is available, so none is recorded.

Each receipt has `source_id` and `source_kind` (`official_api_key`, `relay_key`, `subscription`, or `unknown`). Use these fields, not the request's initially selected route. Hub attribution comes from the serving hop at launch, not today's Source configuration. Unverified or unavailable identity is null; missing classification is `unknown`. Receipts contain no body, URL or credential. A completed pause and its completed follow-up are separate entries. Refused requests, partials and attempts stopped before send contribute no receipt. No upload is inferred from a missing or malformed receipt. The view returns NULL for an invalid container or an array with no valid entries, and keeps valid siblings in a mixed array. An explicitly empty array stays `[]`. As with other request diagnostics, this is not a crash-proof billing ledger.

Sessionless Bot handle proposals are `purpose = 'bot_handle'`, with a null `session_id`; a completed request counts even when its answer is not a valid handle. Their receipts use the existing audit event store, not a synthetic conversation. The `request_uploads` view includes them and receipt-only cleanup audits; `cache_requests` excludes both before comparing request times, so neither affects `vibe data cache`.

For upload totals, expand **only** `successful_wires`: `thread_attempts` describes some of the same POSTs plus failed attempts, and `cache_state` is a recovery snapshot, not additional traffic.

```sql
SELECT json_extract(w.value, '$.source_kind') AS kind,
       json_extract(w.value, '$.source_id') AS source,
       count(*) AS completed_posts, sum(json_extract(w.value, '$.body_bytes')) AS uploaded_bytes
FROM request_uploads AS r, json_each(r.successful_wires) AS w
GROUP BY kind, source;
```

For the install-anchored **72-hour per-Source total**, supply the known installation instant as UTC microseconds since the Unix epoch. The example uses `first_day = 0` and `days = 3`; no daily split is needed. To inspect one day, use its zero-based index and `days = 1`. Each successful POST carries its own `sent_at_us`, sampled at dispatch. Neither a later response, transcript commit nor delayed audit changes its day. The interval is `[start, end)`; these are elapsed 24-hour windows, not local calendar days. Missing or invalid receipt times are NULL and remain in all-time totals only. Do not substitute `request_uploads.at` or infer an installation time from the first receipt.

```sql
WITH anchor(install_us, first_day, days) AS (VALUES (1790861400123456, 0, 3)),
window(start_us, end_us) AS (
  SELECT install_us + first_day * 86400000000, install_us + (first_day + days) * 86400000000 FROM anchor
)
SELECT json_extract(w.value, '$.source_kind') AS kind,
       json_extract(w.value, '$.source_id') AS source,
       count(*) AS completed_posts, sum(json_extract(w.value, '$.body_bytes')) AS uploaded_bytes
FROM request_uploads AS r, json_each(r.successful_wires) AS w, window
WHERE json_extract(w.value, '$.sent_at_us') >= start_us
  AND json_extract(w.value, '$.sent_at_us') < end_us
GROUP BY kind, source;
```

Tests at the accounting boundary cover warm/cold request starts (including a long response), route switches, retries, restart/fork, checkpoint and result-clearing causes, and TTL decompositions without double counting. Store contract tests cover optional cache round trips and indexed session-scoped audit recovery. Adapter tests cover prompt events/no-op previews. Isolated database/CLI tests cover every classification, timestamp-derived legacy rows, unknown rates, what-if arithmetic and freshness summaries. Existing C-9, wire conversion, snapshot, fork and shared-prompt tests guard intentional non-changes.
