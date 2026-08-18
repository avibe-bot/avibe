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

The same rule decides the cached-input subset. `cached_input_tokens` is a part of
`input_tokens`, so after merging every container and frame the cached count is
bounded by the input count *this code normalized itself* — a bound we measured,
never a total the response declared. Cached input reported without a readable input
count is a subset of nothing and clamps to zero. `ProtocolUsageReport.of` is the one
constructor that holds the invariant, and a persisted row that violates it (older
release, hand edit) is repaired on read rather than travelling out through a read
surface that promises the subset.

Persisted instants get the same treatment: the read surface promises `date-time`, so
a value that is not one degrades the field to absent instead of being published, and
`last_metered_at` comparisons order two rows as *points in time* — text order is not
time order once two rows carry different UTC offsets.

Identity is decided once, by `canonical_model_id` in `identifiers.py`: a model ID is
trimmed, non-empty, and within `MODEL_ID_MAX_LENGTH`. Both boundaries that admit one
(discovery and custom-model mapping) store what it returns, and the ledger keys rows
by it, so config, resolution, and metering cannot disagree about what "the same
model" means — two spellings of one ID would otherwise become two rows in the tab,
and a listing naming one model twice is a failed discovery rather than two models.
The ledger's re-check is therefore unreachable for anything the hub admits, so it
logs a warning rather than dropping silently: a future boundary that forgets should
show up as lost metering, not as a quietly incomplete tab. The rule is deliberately
*not* applied when loading persisted config — per the persisted-shape rule, a legacy
value must not make startup fail.

Every cross-field promise the read contract makes is a `_COUNTER_SUBSETS` entry
repaired in one place — cached input inside input, token reports inside requests —
and `record()` builds its row through the same normalization a persisted row gets, so
what this module writes it could also have read. Rejected state is never silent: a
ledger that fails to parse, is not a list, or holds unusable rows warns before the
next write replaces it, which is the last moment that history is recoverable.

Eviction under `USAGE_MAX_ROWS` orders rows by day and then by *when they were last
metered*, not by key. Ordering by key would evict by spelling: an early-sorting model
would be recreated and evicted again on every write while later-sorting stale rows
survived, so its usage could never accumulate.

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
  "last_metered_at": "2026-08-18T03:14:00+00:00"
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

**The metered unit is one upstream call, not one turn.** A turn that failed over
made several calls, and each hop that reported tokens billed the Source it was made
against — attributing all of them to the Source that finally served would both
undercount the failing Source and misprice the serving one.

A call is recorded when it reached the model: the hub forwarded its output
downstream, or upstream reported tokens for it. The second half matters because a
vendor that reported tokens billed us whether or not the response ended well — a
stream that died after its terminal frame, or a buffered error carrying a usage
block. Usage a stream reported before it failed counts too: Anthropic bills input
tokens on `message_start`, so a terminal observation carries everything accumulated
to reach it. A call that never reached the model (rejected credential, engine down)
is not recorded: resolution events and source health already own that surface, and
counting it here would duplicate the concept.

For a stream, `_SSEWireState.reached_model` is the one place that answers "did this
call reach the model", because every ending of a stream has to answer it the same
way. Forwarded model output is sufficient on its own — a connection lost after a text
delta is a request that happened, and `token_reports` staying at zero is exactly how
the ledger records that nobody reported its tokens.

Metering has **two owners over disjoint populations**, split by the one line that
decides who can read a call's body — `handle.stream is not None` in
`ModelHubService._invoke`:

- `ModelHubService._meter_call` records every call the resolver consumed itself.
  These have no body to forward, so the gateway never sees them, and for an error
  the resolver may not even name that Source in what it raises.
- `ModelHubTurnGateway._record_usage` records every call whose body the gateway
  forwards, reading tokens from the buffered body or the live SSE wire state.

Each call therefore reaches exactly one owner. Within the gateway, several endings
can describe one forwarded call — a stream that finished and then failed to flush,
a downstream disconnect that raced the terminal chunk — so the first ending to
report wins and later ones are no-ops. A structure guard pins both owners and the
gateway's idempotence flag.

Both owners write through `asyncio.to_thread`: the ledger's read-modify-write is
file I/O, and per `CLAUDE.md` §6 blocking work must not run on the controller event
loop. Metering is a report, never a control input, so a ledger failure is logged and
the call the caller sees is unchanged.

## Read surface

`GET /api/models/usage?days=<n>` → `{usage: UsageSummary}` with `days` bounded to
`[1, 62]`, defaulting to 30. The summary is derived entirely from the rows:

- `totals` — window-wide counters
- `sources[]` — per source, with a `models[]` breakdown and `last_metered_at`
- `days[]` — one entry per local day, ascending, for a trend series

Contract work: an `api.md` route-table row, an `x-model-hub-routes` entry plus a
`UsageResponse` definition in `api-response.schema.json`, and a new
`usage-summary.schema.json`. The existing conformance guard then exercises the
route against a real server response.

## Review-loop diagnosis (2026-08-18, heads `847d681d` and `ac32d098`)

Two findings-bearing heads produced twelve P2 findings. Classified by root cause
rather than by comment, three classes recurred across both heads, which trips the
`ENGINEERING.md` circuit breaker. The full inventory and the ruling:

- **Class A — no owner for "the canonical form of a model identifier"** (findings 1,
  11). Three notions coexisted: config accepted anything, the admission boundaries
  checked length only, and the ledger trimmed *and* bounded. Consolidated into
  `canonical_model_id`; both boundaries store its result and the ledger keys by it.
- **Class B — persisted-state degradation versus read-contract promises** (findings
  2, 9, 12). Each cross-field guarantee needs one normalization site, and per the
  persisted-shape rule a rejection must warn. Consolidated into `_COUNTER_SUBSETS`,
  one shared `_normalize_row` on both the read and write paths, and warnings in
  `_read`.
- **Class C — metering population coverage** (findings 3, 4, 7, 10). Ownership was
  the fix on head 1 (`_invoke` splits the two populations); on head 2 the remaining
  two were a dropped field and code contradicting its own written policy, now owned
  by `reached_model`.
- **Class E — blocking I/O on the controller loop** (finding 6). Fixed, no recurrence.
- **Class F — bounded-retention eviction policy** (finding 8). No recurrence; evicting
  by key starved an early-sorting active model, so eviction is by recency.

Ruling: the breaker pauses blind patching, it does not automatically escalate to the
owner. Every action above is local, reversible, and makes the code match a contract
that was already written down, with no major trade-off and no irreversible risk — so
the work continued rather than stopping for a decision. The recurring signal was real
and was answered the way the property-ownership rule prescribes: consolidate the
owner, then resume.

### Round 3 (head `1ddff18d`)

Three more P2 findings, two of them class C again — a third findings-bearing head for
that class. Diagnosis over patching, per the same ruling: class C had two *remaining*
seams, both the same shape as the original one. "What upstream reported for this call"
was still being re-derived at each site that needed it, so any site that ended the call
by another route answered the question with nothing.

- **Seam 1, engine client** (`vibe/model_hub_runtime/client.py`). The prelude reader
  owned the wire tracker as a local and returned it alongside the outcome, so an exit
  that *raised* — a read timeout, a frame-limit error, a transport drop — lost whatever
  the wire had already reported. Anthropic reports input tokens on `message_start`,
  which lands while the prelude is still buffering, so this is a call the vendor billed.
  The tracker now belongs to the caller, and one closure (`ended`) is the sole exit for
  the whole population `_meter_call` meters; it attaches the report in one place instead
  of at nine construction sites.
- **Seam 2, turn gateway** (`core/handlers/model_hub/turn_gateway.py`). For a streamed
  turn the report lived in `execution.wire_state`; for a buffered one it lived in a
  request-frame local. `_settle_boundary_termination` runs at the boundary and could
  only see the former, so a downstream disconnect during settlement dropped a billed
  buffered turn. `_TurnExecution.reported_usage` now owns the question for both shapes,
  and the buffered observation is published before anything cancellable runs.
- **Class B again** (finding 3, `_timestamp`). The read surface promises an
  offset-bearing date-time while `_instant` accepts every spelling
  `datetime.fromisoformat` does. Publishing the raw text let a hand-edited
  `2026-08-18T03:14` leave through the API as something the schema does not describe.
  The field now carries what the parser understood, not what the file said.

### Round 4 (head `617aefb80`)

Five P2 findings, and the two recurring classes both turned out to have been fixed one
level too shallow. Round 3's rulings were correct about the sites they named and wrong
about where the property lived.

- **Class A, a third admission path** (`identifiers.py`, `config/v2_config.py`,
  `service.py`). Rounds 1–2 put model-ID canonicalization at "the admission boundaries",
  which made it a duty each boundary had to remember — so `create_source` with inline
  `models`, a boundary added for a different reason, obeyed neither half. The rule is now
  split by what each half can safely do. *Spelling* (`normalized_model_id`) moves into
  `ModelHubModelConfig.from_payload`, the one validator every path goes through,
  including the one that loads files older releases wrote; it only strips padding, so it
  cannot reject anything that used to load. The *length bound* stays at the admission
  surfaces, because that same validator sits under `ModelHubConfig.from_payload`, where
  raising fails config load outright — rejecting a persisted value there would break the
  persisted-shape rule. Two owners because there are two properties, not two call sites.
- **Class B, three findings** (`usage.py`: `_timestamp`, `_normalize_row`, `_read`).
  Applying the repo lesson about anchoring adversarial-input rulings to self-measured
  bounds: round 3's `_timestamp` fix still let the *file* decide the output shape, which
  is the endless narrowing series that lesson names — fix `2026-08-18T03:14` and
  `+00:00:30` is still reachable. Terminal rule: **the file supplies the instant, this
  module supplies the spelling.** `_timestamp` normalizes to UTC so the offset is
  `+00:00` by construction; `_normalize_row` re-emits the day via `date.isoformat()` so a
  row written as `20260818` still lands in the windows that compare `YYYY-MM-DD` text;
  `_read` degrades by failure *category* (`OSError, ValueError, RecursionError`) rather
  than by the shapes one file happens to hold.
- **Class E, the other half of round 2** (`rpc.py`). Round 2 moved the ledger *write* off
  the controller loop and left the *read* on it, where it takes the same lock a writer
  holds across `fsync()` — every turn on the machine waiting on one settings page. The
  read now goes through `asyncio.to_thread`, and a structure guard asserts every RPC
  entry into the ledger is enclosed by one, so the next entry cannot quietly be the
  exception.

Known limitation observed while narrowing
`test_opencode_identity_computation_stays_in_validator_and_resolver`: its import check
inspects absolute imports only, so a module inside `core/handlers/model_hub/` can reach
OpenCode identity helpers by relative import without tripping it. Pre-existing and left
alone here — closing it requires ruling on `service.py`'s existing relative import of
`STANDARD_OPENCODE_VENDOR_IDS`, which is a separate question from this stage.

### Round 5 (head `8e2af4a4b`)

The two remaining P2 threads. Both fixes are one line of behavior; what took the round
was that each one, once written, exposed something the change it belonged to had left
half-done.

- **Route hops are spelled by whatever spells what they name** (`config/v2_config.py`).
  Round 4 moved model-ID canonicalization into `ModelHubModelConfig.from_payload` so a
  persisted inventory normalizes on load. A route hop names a model in that inventory,
  and `inspect_exact_hop` decides membership by an exact comparison — so normalizing one
  side only converts a working chain into `model_unsupported` on upgrade. `from_payload`
  now applies `normalized_model_id` to the hop too. The dedup check that follows had to
  split in two: duplicates *as written* stay a malformed payload and raise, while
  duplicates that appear only once spelling is settled are a legacy file naming one
  upstream model twice — the second hop was already unreachable past the first, so it
  collapses and the chain loads, where raising would fail a config load the
  persisted-shape rule requires to succeed.
- **`contract_version` 5 → 6, and the persisted object that cannot follow it**
  (9 contract files, `service.py`, `provenance.py`). The usage route is a new versioned
  surface, so the contract's single-number closure has to move as a whole. Bumping it
  broke `test_released_v5_permission_denied_records_degrade_at_read_boundary`, which is
  the point: `TurnProvenance` is the only versioned object written to disk, so records a
  released v5 build persisted outlive the bump that republished the shape. It now accepts
  `{5, 6}` — a set ending at the terminal value — while every other versioned object,
  being an envelope built and consumed inside one request, stays pinned to 6 alone.

Two invariant tests hold the properties rather than the cases.
`test_every_versioned_object_ends_at_the_terminal_version_the_code_writes` reads the
accepted values out of whatever schemas the contracts directory holds and compares them
with each other *and* with `CONTRACT_VERSION`; nothing did that before, which is how
round 4 could have shipped a half-applied bump.
`test_config_reload_spells_route_hops_like_the_inventory_they_name` seeds a model and the
hop naming it in every spelling a persisted config can carry, rather than listing the
spellings that are exempt.

Writing them surfaced a third gap of the same shape: the contracts `README.md` index had
been missing `api-response.schema.json` since before this PR and `usage-summary.schema.json`
since this one — a file that ships but is invisible to anyone reading the index.
`test_contracts_readme_indexes_every_file_beside_it` now compares the table with the
directory, so the next file added is caught here instead of by whoever needed it.

### Round 6 (head `44bd53b48`)

Two P2 findings, and the first of them tripped the circuit breaker: "deduplicate model
IDs after normalization" in `ModelHubSourceConfig.from_payload` is the *same class* as
round 5's hop finding on the previous head. Stopping before a third site patch and
diagnosing the inventory instead, per the ruling recorded above.

- **Class G — normalized identity versus the collections that hold it**
  (`config/v2_config.py`). Round 4 made `normalized_model_id` a many-to-one map applied
  inside a leaf validator, while every uniqueness check lives in the parent that holds
  the collection. So each parent compares pre-images while the object it builds carries
  post-images. The consequence is not cosmetic: the loaded config keeps both entries,
  `to_payload` writes one spelling for both, and the next load of what this one wrote
  raises — a released build could write a file this product refuses. The class has
  exactly two members, confirmed by reading every application of the normalizer:
  `ModelHubModelConfig`/`ModelHubSourceConfig` and
  `ModelHubRouteHopConfig`/`ModelHubRouteConfig`. Route dict keys, `menu.checked`, and
  `sources.order` are a different identifier space and are not normalized. Both members
  now go through one owner, `_collapse_settled_duplicates`, which keeps round 5's
  ruling: duplicates *as written* raise, duplicates that appear only once spelling is
  settled collapse into the first — the later one was already unreachable.
- **Recency is only meaningful against a clock this module measures**
  (`core/handlers/model_hub/usage.py`). `_retained` named a window and implemented a
  half-line: a lower edge only. `window` already refuses to report a row dated after
  today, so a future-dated row contributes to nothing a reader can see — while holding
  one of the `max_rows` slots and outranking every real row in `_recency`, which evicts
  the least recently metered. A host clock that jumps forward while many pairs are
  metered and is then corrected therefore stops metering entirely until those dates
  arrive. Retention keeping what reads refuse was the defect; `_retained` now applies
  the same window at both edges, and bounds a within-window row whose `last_metered_at`
  has not happened yet to the measured instant. Same terminal rule as round 4's: the
  file supplies the instant, this module supplies its spelling *and* its ceiling.

Three invariant tests, each proven to fail without its fix.
`test_loading_a_persisted_config_yields_one_this_product_can_load_again` states the
property behind two rounds of findings once instead of per collection: whatever `load`
returns for a file a released build wrote, serializing it produces a file `load`
accepts. `test_every_normalized_identifier_collection_collapses_through_one_owner`
names the class rather than its members — an AST walk asserting the exact set of
classes that normalize and the exact set that collapse, so a third site fails a test
instead of costing a review round.
`test_a_new_call_is_metered_whatever_recency_the_ledger_already_claims` seeds every
shape a persisted row can use to outrank the present (future day, future instant, both)
rather than listing the ones that are handled.

### Round 7 (head `ac907286b`)

Two P2 findings. One of them is class C again — a fourth findings-bearing head for
that class — and the other is a regression introduced by round 6's own fix.

- **Class C — metering population coverage, closed by removing the question**
  (`core/handlers/model_hub/turn_gateway.py`). Round 3 gave "what tokens did upstream
  report" one owner, `_TurnExecution.reported_usage`, because a boundary that ends the
  turn early cannot know which shape the response took. Its sibling question — "did
  this call reach the model" — was left to the endings, and each answered in the
  vocabulary of the shape it happened to see: the buffered ending read the settlement
  decision, the streaming ending read the wire tracker, and the boundary read the wire
  tracker *and a buffered turn never has one*, so `served` there was structurally
  `False`. A complete upstream body carrying no usage block, cancelled downstream
  mid-settlement, was therefore never counted. That is why the class kept recurring at
  a new site each round: the finding is not any one ending, it is that endings answer
  at all.

  So the closure is not a third owner but one fewer question. `reached_model` joins
  `reported_usage` on `_TurnExecution` — reading `wire_state.reached_model` for a
  stream and the buffered observation's own `outcome == "served"` otherwise, which the
  gateway already computed and discarded — and `_record_usage` now takes only the turn.
  An ending is a *when*, never a *what*; an ending added later cannot get this wrong
  because it has nothing to pass in.

- **A ledger-wide bound cannot come from one call's stamp**
  (`core/handlers/model_hub/usage.py`). Round 6 fixed retention by measuring the window
  against a clock instead of the file — and then handed it `at`, the *captured* moment
  the call ended. Metering runs off the event loop, so concurrent calls reach the lock
  in whatever order the executor ran them: a call stamped just before local midnight
  persisting after one stamped just after it dated the newer row into the future and
  dropped it, and a same-day inversion clamped a newer `last_metered_at` backward.

  `at` and the horizon are the same clock read at two different times, and the gap is
  the whole point — so the ledger now takes the clock, not a moment, and reads it under
  the lock where the write happens. `at` decides this call's bucket and stamp and
  nothing else, bounded by that reading because nothing is metered later than the write
  that records it. The hub's callers pass their own clock in, so a fixed service clock
  still decides every day the ledger writes; this is one clock read at the right place,
  not a second clock. Round 6's ceiling property is unchanged and now holds under
  concurrency.

Three invariant tests, each proven to fail without its fix.
`test_a_cancelled_buffered_turn_is_counted_even_when_it_reported_no_tokens` is the
existing cancellation test with the one thing removed that was carrying it, the usage
block; without the fix it fails with `KeyError: 'requests'`, nothing metered at all.
`test_no_ending_of_a_turn_decides_for_itself_what_the_call_did` asserts the closure
rather than today's three endings: every `_record_usage` call passes the turn and
nothing else. `test_a_write_never_rewrites_what_a_later_stamped_write_already_persisted`
seeds one row of every shape a later-stamped concurrent write can leave behind — an
instant this write has not reached, and a day it has not reached — and asserts the
earlier-stamped write contributes its own row and changes nothing else; without the fix
it reproduces both harms at once, one row dropped and one clamped backward.

The ledger's clock also removes a latent test defect: retention was previously measured
against whatever moment a test happened to pass as `at`, so the fixed 2026-07-29 clock
in `test_model_hub_l3.py` would have started failing once the wall clock drifted past
the retention horizon from it. Ledger construction now carries the same fake clock as
its service.

### Round 8 (head `a481bc864`)

One P2 finding, and class C a fifth time: a forwarded call permanently omitted.
`usage_recorded` was set before the cancellable `asyncio.to_thread()` await, so a
downstream disconnect while that write sat queued behind a saturated executor
cancelled the write *and* left the flag saying it was done. Cleanup could not retry,
and the buffered path had already recorded settlement, so the boundary's own
`_settle_boundary_termination` never ran either.

Breaker diagnosis, as orchestrator. The four earlier instances were all "an ending
decides something about metering", and round 7 closed the *what* — no ending is asked
what the call did. This one is a different seam of the same class: the write's
**lifetime** was still bound to the turn. `_record_usage`'s docstring already claimed
metering is a report and never a control input, while the code made the report's
existence depend on the turn's control flow. Same shape as round 6's finding — code
contradicting its own written policy — so the ruling is the same: make the code mean
what it says, locally and reversibly, and continue.

The gateway already owns the pattern. Its cancellation boundary comment reads *"Only
the request is canceled. Owned teardown and settlement are shielded and drained before
this boundary re-raises"* — metering was the one piece of work never given that
treatment. So:

- `_TurnExecution.usage_recorded: bool` becomes `usage_write: asyncio.Task[None] | None`.
  The record that a turn was metered *is* the owner of its write, rather than a flag
  that something whose own death could take the write had started one.
- `_record_usage` creates that task, registers it in a gateway-held set, and only then
  awaits — shielded. Nothing suspends between reading the facts and owning the write,
  so a cancellation lands either before this turn was ever going to be metered or after
  the write is already someone else's to finish. An ending is now a *when* in one more
  sense: it decides when a call is metered, never whether the metering survives.
- Shielded rather than detached, deliberately. The ordinary path should still leave the
  row on disk before the turn ends, so a caller reading the tab right after its own call
  sees it. Cancellation is the one ending that ordering cannot survive, and there it is
  the ledger's read-your-write guarantee that gives way, not the write.
- `close()` drains the outstanding writes, bounded by the transport timeout like every
  other owned drain, and warns on overrun. `core/controller.py` already closes the
  gateway on shutdown, so this is a real drain rather than a decorative one.

`test_a_cancelled_turn_cannot_take_the_ledger_write_it_queued_with_it` reproduces the
window exactly rather than mocking around it: it pins the loop to a one-thread executor
and occupies that thread, so the ledger write is genuinely queued-not-started, asserts
the ledger is still empty (the scenario is only the scenario while the write is
queued), cancels the turn, then releases and drains. Replacing the shield with a plain
`await write` fails it with `KeyError: 'requests'` — the reviewer's permanent omission,
verbatim.

The structure guard `test_usage_metering_has_one_owner_per_call_population` now names
the gateway owner as the pair `_record_usage` + `_persist_usage`, and pays for the
second name with the constraint that makes it one owner: `_persist_usage` has exactly
one call site and it is inside `_record_usage`, the same single-caller assertion
`_meter_call` already carries.

### Round 9 (head `839498456`)

One P2 finding, and class C a sixth time — at the address round 8 should have covered.
`ModelHubService._meter_call` still awaited `asyncio.to_thread()` inline, so a bodyless
attempt that upstream had already billed (a failover error carrying usage) lost its row
to exactly the window round 8 closed at the gateway: cancellation reaching the queued
executor future before `record()` starts.

This is not a new class, it is the sibling the round-8 note could already have named.
The skill's rule is explicit — *the moment you can enumerate the members the reviewer
has not reached yet, close the class now* — and round 8 knew metering has exactly two
owners, because the guard that asserts it was edited in the same commit. Patching the
second site would buy the same finding back at whatever third population arrives.

So the closure is ownership, not a second patch. Write-lifetime is a third property:
neither population decides it, both need it, and it had no owner. `UsageWriter` in
`usage.py` is that owner.

- `UsageWriter.record()` is **synchronous** on purpose. Nothing may suspend between a
  caller deciding to meter a call and the writer owning the result, or a cancellation
  could land in the window where the call is neither metered nor still meterable. It
  starts the task, keeps a strong reference, and hands the task back.
- Both owners keep their existing shape: decide, hand off, `await asyncio.shield(...)`.
  A caller that wants the row on disk before it returns awaits; one that does not can
  drop the task. Read-your-write survives on the ordinary path, and only cancellation
  gives up ordering — never the write.
- `drain(timeout=...)` replaces the gateway's private set. The gateway reuses
  `service.usage_writer` whenever both write the same ledger, so one bounded drain at
  `close()` covers both populations rather than one drain per population.

`test_a_cancelled_resolve_cannot_take_the_ledger_write_it_queued_with_it` mirrors the
round-8 test at the resolver: one-thread executor occupied, row queued-not-started,
ledger asserted empty, caller cancelled, then released and drained. Dropping the shield
fails it with `KeyError: 'requests'` — the same permanent omission, at the other owner.

The structure guard is what makes this a closure rather than a second patch. It now
asserts that **neither** `service.py` nor `turn_gateway.py` touches the ledger at all,
that every ledger write in `usage.py` is inside `UsageWriter._persist`, and that each
owner hands off to the writer exactly once with nothing else in either module recording
anything. A third population cannot be written with its own write lifetime; it inherits
one or it fails a test.

## Todo

- [x] Usage taxonomy and extraction in `stream_wire.py`
- [x] `BoundedUsageLedger` in `core/handlers/model_hub/usage.py`
- [x] Resolver and gateway recording over the two disjoint call populations
- [x] `ModelHubService.usage_summary` + RPC + `GET /api/models/usage`
- [x] Contract row, response registry entry, and usage schema
- [x] Unit and contract coverage
- [ ] Stage 2: draw the 用量与额度 tab against this data

## Stage 2 note

The tab is currently named 用量与额度 / "Usage and quota". After this stage the
用量 half is real and the 额度 half is still unobtainable, so the label needs a
design decision before stage 2 ships — renaming the tab to 用量 is the honest
option unless a quota source appears.
