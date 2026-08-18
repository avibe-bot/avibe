# Model Hub usage metering core

Status: **in delivery** — stage 1 of two.

## Background

The Model Hub settings page ships two tabs. 来源与网关 is fully specified by 13
`design.pen` frames and fully implemented. 用量与额度 renders a placeholder that
promises "usage appears after a gateway source serves a request" — a promise
nothing in the product can keep:

- `ModelHubSourceUsageConfig` (`config/v2_config.py`) is validated and persisted
  but has **zero writers**; all three construction sites emit empty defaults.
- No `/api/models/*` route returns usage. `api.md` mentions `usage` once, only to
  forbid it in create requests.
- Token counts exist nowhere durable. `agent_runs`, `background_runs`,
  `run_definitions`, and `runtime_records` have no token columns; backend adapters
  surface per-turn counts transiently into a progress bubble and drop them.

So the tab cannot be drawn truthfully before a metering core exists. This plan is
that core: produce real data first, draw the tab second.

## Goal

Persist a bounded, credential-free, per-source token aggregate measured from the
turns the hub actually proxies, and expose it through one read route.

Non-goals, explicitly:

- **No spend.** Converting tokens to money needs a per-model price table Avibe
  does not ship and cannot keep current. `month_spend_cents` stays unwritten.
- **No quota percentage.** No vendor API exposes a consumer subscription's cycle
  usage. `cycle_used_pct` and `projected_exhaust_at` stay unwritten.
- **No enforcement.** These numbers never gate admission, routing, or cooldown.
  They are a report, not a control input. A hostile upstream must not be able to
  change resolution behavior by lying about usage.

## Where usage is observable

`core/handlers/model_hub/turn_gateway.py` is the only place in the product that
sees a complete upstream model response. `stream_wire.py` already tokenizes every
SSE frame and parses every buffered body through one declarative per-protocol
table (`PROTOCOL_STREAM_TAXONOMY`) to decide terminal outcome and first-output
boundary. Usage extraction belongs in that same table — no second parser.

Per protocol, the usage container and its leaves:

| Protocol | Containers | input | cached input | output |
| --- | --- | --- | --- | --- |
| `anthropic` | `message.usage` (stream `message_start`), `usage` (stream `message_delta`, buffered body) | `input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens` | `cache_read_input_tokens` | `output_tokens` |
| `openai_responses` | `response.usage` (`response.completed` / `response.incomplete`), `usage` (buffered body) | `input_tokens` | `input_tokens_details.cached_tokens` | `output_tokens` |
| `openai_chat` | `usage` (final chunk under `stream_options.include_usage`, buffered body) | `prompt_tokens` | `prompt_tokens_details.cached_tokens` | `completion_tokens` |

Two composition rules make this order-insensitive and duplicate-frame-safe:

- **Sum within one container.** Anthropic's `input_tokens` *excludes* cache
  tokens, so the input total is the sum of three leaves — the same composition
  `modules/agents/claude_agent.py` already applies. OpenAI's input counts
  *include* cached tokens, so `cached_input_tokens` there is an informational
  subset, never an addend.
- **Take the max across containers and frames.** Anthropic reports cumulative
  `output_tokens` on every `message_delta`; a max ignores intermediate values and
  is immune to a frame being observed twice.

## Self-measured versus vendor-reported

`requests` is measured by our own code and always available. Token counts are
reported by the upstream vendor and may be absent (`openai_chat` only emits usage
when the client asked for it). The ledger therefore keeps a separate
`token_reports` counter so the tab can say "12 requests, 9 with reported tokens"
instead of implying that a missing report is zero usage.

Every vendor-reported integer is validated as a non-negative, non-bool `int` under
a fixed structural ceiling before it is accumulated, so one buggy or hostile
response cannot permanently poison the aggregate. The ceiling is a constant in our
code, never a value the upstream declares.

## Persisted shape

New state file `~/.avibe/state/model_hub_usage.json`, written through the same
atomic pattern as `BoundedEventLog` / `BoundedProvenanceStore`: lock, read,
mutate, `NamedTemporaryFile` + `flush` + `fsync` + `chmod 0o600` + `os.replace`.
A read error or non-list payload degrades to `[]` — per the persisted-shape rule,
a broken optional-feature file disables the feature and never fails startup.

One row per `(day, source_id, model_id)` — exactly the dimensions the read surface
reports. A `backend` dimension is deliberately absent: nothing in the summary
groups by it, and a persisted column no reader consumes is a concept without an
owner. It can be added when a reader needs it.

```json
{
  "day": "2026-08-18",
  "source_id": "src_abc",
  "model_id": "claude-opus-4-6",
  "requests": 12,
  "token_reports": 9,
  "input_tokens": 148230,
  "cached_input_tokens": 96010,
  "output_tokens": 4120,
  "last_served_at": "2026-08-18T03:14:00+00:00"
}
```

Days are **local-calendar** days: Avibe is a local-first product and the UI
already presents local days (`ui/src/components/settings/models/localCalendar.ts`).

No source label is persisted. Labels are user-supplied text that resolution events
have to redact; the read path joins `source_id` against current config instead, so
the ledger stays credential-free and immune to label drift.

Bounds: rows older than 62 local days are dropped on write, then the newest 400
rows are retained (oldest day evicted first).

## Recording policy

Record one row increment at each of the two settlement points in
`ModelHubTurnGateway._resolved_response`, and only when the turn actually reached
the model — buffered: the settlement decision is `return`; streaming: the wire
terminal was `served`. A turn that upstream reported usage for is also recorded
even if its stream ended in a terminal error, because the vendor billed it.

Failures already have their own surface (resolution events plus source health);
counting them here would duplicate that concept.

## Read surface

`GET /api/models/usage?days=<n>` → `{usage: UsageSummary}` with `days` bounded to
`[1, 62]`, defaulting to 30. The summary is derived entirely from the rows:

- `totals` — window-wide counters
- `sources[]` — per source, with a `models[]` breakdown and `last_served_at`
- `days[]` — one entry per local day, ascending, for a trend series

Contract work: an `api.md` route-table row, an `x-model-hub-routes` entry plus a
`UsageResponse` definition in `api-response.schema.json`, and a new
`usage-summary.schema.json`. The existing conformance guard then exercises the
route against a real server response.

## Todo

- [x] Usage taxonomy and extraction in `stream_wire.py`
- [x] `BoundedUsageLedger` in `core/handlers/model_hub/usage.py`
- [x] Gateway recording at both settlement points
- [x] `ModelHubService.usage_summary` + RPC + `GET /api/models/usage`
- [x] Contract row, response registry entry, and usage schema
- [x] Unit and contract coverage
- [ ] Stage 2: draw the 用量与额度 tab against this data

## Stage 2 note

The tab is currently named 用量与额度 / "Usage and quota". After this stage the
用量 half is real and the 额度 half is still unobtainable, so the label needs a
design decision before stage 2 ships — renaming the tab to 用量 is the honest
option unless a quota source appears.
