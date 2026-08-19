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

### Round 10 (head `2520031c4`)

Three findings, and class C a seventh time. The breaker is tripped, so this round starts
with the diagnosis rather than an edit.

**The class, restated over all seven heads.** Every member has the same shape: *a
metering fact is recorded later than the moment it became true*, so any ending between
those two moments loses the row permanently. What moved each round was only which gap
was left. Heads 1 and 2 and round 3 moved *what* is metered (which populations exist);
rounds 7 and 8 and 9 moved the *lifetime* of the write (who owns it once it is queued).
Round 10 is the last remaining gap and a different axis from both: *when the fact is
published to the object that will be asked for it*.

Concretely, `_TurnExecution.reached_model` derived its answer from two later
observations — a wire tracker, or a buffered verdict. The gateway adopts the upstream
body at `turn_gateway.py:619`, then prepares the downstream response, then starts
reading. A client that disconnects inside `response.prepare()` leaves neither
observation behind, so cleanup asked a question that had a true answer and got nothing.

**Why a third derivation would not close it.** Rounds 8 and 9 each added a mechanism to
carry the answer further; round 10's reviewer found the one ending that arrives before
any mechanism exists. Adding a fourth publication point only moves the gap earlier
again. The terminal form removes the derivation: *adoption is itself the proof*. The
gateway holds a body iff the call reached the model, the field is assigned before
anything downstream can fail, and nothing after it can unset it.

**Verified contract-preserving before shipping.** The pairing looked like a double-count
risk, because `ResolvedInvocation` (`service.py:279`) carries both a `handle` and an
`outcome`. `_invoke` (`service.py:4904`) settles it: `if handle.stream is not None:
return handle, None` — the resolver meters exactly the calls it completed itself, and
hands over exactly the ones it did not. The two populations are already partitioned by
`handle.stream is not None`, which is also the condition under which the gateway adopts.
The new branch is therefore purely additive: it fires only where the row used to be
dropped.

**Closing the unflagged sibling in the same edit.** The review reached the streaming
path. The buffered path has the same gap — cancelled mid-`outcome()`, before any verdict
exists — and `test_a_turn_cancelled_before_it_read_the_body_is_still_a_call_that_happened`
pins it. Its assertion is the honest one: `requests == 1` with `token_reports == 0`, which
is how the ledger says the vendor billed a call nobody got to read.

The streaming test is written as an invariant, not a list: it derives its failure points
from `FakeStreamResponse`'s own `*_error` parameters, so a downstream step that becomes
failable later is covered when it can fail rather than when someone remembers this test.

**P2 — ledger writes off the shared pool.** `record()` submitted one `asyncio.to_thread`
job per completed call into the loop's *default* executor, where `BoundedUsageLedger`'s
lock immediately serialized them across an `fsync`. A burst therefore occupied shared
workers that unrelated controller work needs, all but one of them waiting, and nothing
bounded the backlog. The fix owns the serialization instead of suffering it:
`UsageWriter` is now a queue whose flush takes everything that accumulated during the
previous write and hands it to `BoundedUsageLedger.record_many` as one transaction, on a
dedicated single-worker executor. The backlog is bounded by what arrives during one
fsync however hard the hub is driven, no row is ever dropped, and a batch of ten is
arithmetically the ten writes it replaces rather than a summary of them.

**P3 — the version-number class, closed rather than patched.** The reviewer flagged
`api.md`'s declaration that "every versioned nested contract uses terminal version 5"
while the same file's envelopes advertise 6 — the second contract-version finding on
this PR. The class is nameable, so the remaining members were enumerated instead of
waited for: the status header (line 3) and the `RuntimeDependency` note (line 55) made
the same stale claim and the review had not reached either.

The root cause is that `api.md` is listed under the README's version closure while the
closure test globbed only `*.schema.json`, so eighteen machine-checkable literals were
guarded and every prose declaration was not. The fix is one rule plus one guard: a claim
about the current value is written as `contract_version <n>` — the token the guard
matches — so a bare `vN` is a generation name by construction, and the closure test now
reads every non-schema file in the directory. The four legitimately historical `v5`
sentences the README already blessed stay untouched, and are now distinguishable
mechanically rather than by judgment.

### Round 11 (head `d9e19d977`)

One finding, and class C an eighth time — the direct sibling of what round 10 fixed.
`reached_model` gained the adoption floor; `reported_usage`, which that docstring names
as its sibling, kept its two branches. Same window (a client leaving inside
`response.prepare()`), same call, and the row now records `requests == 1` with the token
counts lost.

**Why round 10's answer does not extend to this fact.** Adoption proves *that* the call
happened; nothing about adoption reveals *how many tokens* it reported. So the class was
never about publishing gateway-built facts earlier — it was about where those facts come
from. Every fact `_TurnExecution` held was built from bytes the gateway had already
pulled, and pulling starts after the downstream response is prepared. Each round found
one more ending inside that window, and each fix taught one more fact to survive it. A
ninth fact would have cost a ninth round.

**The fact already exists on the other side of the hand-off.** `client.py:401` builds a
`ProtocolSSEState` and `_read_stream_prelude` feeds it: the engine only knows there *is*
a stream because it read as far as the first model output, which for Anthropic is past
the `message_start` frame carrying the billed input tokens. `_response_stream` keeps
observing into that same tracker as it yields, without re-observing the replayed prelude
— so it is never behind the gateway's copy and never double-counts. `EngineInvokeHandle`
then handed over `stream`, `outcome`, and `stream_closer`, and dropped it.

`ended()` (`client.py:282`) is the proof this is a property with a missing owner rather
than a missing line: the *non-adopted* population has owned exactly this since round 7 —
one exit that attaches the wire's report to the outcome, with a comment naming the same
Anthropic case. The adopted population is the same property with no owner at all.

**The fix.** `InvokeHandle` gains `observed`, the adapter's own observation of the body
it hands over, mirrored byte-identically into the contract copy. Both `_TurnExecution`
facts ask it first and fall back to what the gateway builds itself. `reached_model` also
moves off a gateway-local `ProtocolSSEState` subclass and onto the shared tracker, which
deletes `_SSEWireState` entirely: both sides of the hand-off keep a tracker over the same
body, so a reading available on only one of them is a reading half the turn cannot use.

**Closed as a class, both directions.** `test_every_body_fact_the_turn_reads_can_come_from_the_engine_that_read_it`
states the property rather than the pair: any `_TurnExecution` property that reads the
gateway's own tracker is by definition a fact about the forwarded body, so it must also
read `upstream_observation` — a ninth fact fails a test instead of costing a round. Its
second half guards the other direction, that every member the contract declares is
implemented by the one real handle, since a fact that stops at the hand-off leaves the
reader asking only fakes for it. The behavioural test reuses round 10's enumeration-free
form, driving the same `FakeStreamResponse` failure points and asserting the prelude's
1028 input tokens land in the ledger at every one of them.

### Round 12 (head `69c51cbaf`)

Three findings. One is class C a ninth time, but through the half every earlier round
was not.

**Reader half versus producer half.** Rounds 7 through 11 were all readers: a fact had
been observed, and some ending could not reach it. Each fix pointed one more ending at
the observation. This finding is upstream of all of them — bytes the socket delivered
that nobody extracted. `_read_stream_prelude` asked the storage question before the
reading question, so a `prelude.write` that refused the chunk which overflowed the
16 MiB budget returned before anything parsed it. A vendor that reports usage in the
frame that happens to land past the budget was billed and never metered, and no
reader-side fallback can recover it: unobserved bytes leave nothing behind to fall back
to.

**Why `_received` is the terminal form.** Two questions get asked about the same bytes
and only the second can fail — what they say, and whether there is room to keep a replay
of them. Whether we can hold a copy is not a question about what happened upstream, so
it cannot come first. Both arrival sites now go through one owner that observes before it
buffers, and that owner is the sole caller of `prelude.write`, so an arrival site added
later cannot reorder the two questions by forgetting which comes first. The overflow path
then reaches `ended()`, which has attached the wire's report to the outcome since round 7.
`test_wire_bytes_reach_the_observer_before_the_buffer_that_can_refuse_them` states the
ordering; `test_tokens_reported_by_the_chunk_that_overflows_the_prelude_are_still_metered`
drives it, with the overflowing chunk carrying a complete `message_start`.

**Class D: five copies of one durability property.** The review flagged
`usage.py:_write` for leaving its temporary file behind when the replacement raised. The
members were enumerable immediately: `revocations.py`, `events.py`, `usage.py`,
`provenance.py`, and `oauth.py` each carried the same seven lines — mkdir, compact
credential-free `json.dumps`, `NamedTemporaryFile`, fsync, `chmod 0o600`, `os.replace` —
so one orphan was really five latent orphans in one directory. Nothing downstream can
notice: the recorders swallow `OSError` deliberately, so a full disk cannot break
metering. A ledger bounded to `max_rows` whose state directory grows by one file per
failed write is not bounded. `state_file.write_state_document` now owns the property and
the five call sites shrank to one to three lines each. The guard is enumeration-free by
construction: no module in the package other than `state_file.py` may name `tempfile`,
`os.replace`, or `os.rename`, so a sixth collection cannot spell its own replacement.

**Scope decision on the sixth site.** `config/v2_config._atomic_write_text` is the same
property in another subsystem and is already correct, including directory fsync. Promoting
both onto one owner would mean editing a widely used config module inside a metering PR
for no behavioural gain, so it stays where it is; the shared home is worth revisiting when
something else needs it.

**Class E: the catalog rows this stage never wrote.** Five `MH-USAGE-*` scenarios now name
the properties the stage actually claims — a reported token count surviving the turn's
ending, per-hop attribution on failover, a failed write costing only that write, the
read-modify-write staying off the event loop, and a report surviving a full prelude buffer.
`tests/test_model_hub_l3.py` and `tests/test_model_hub_usage.py` join `canonical_tests` and
the project index, and each scenario ID is greppable from its test's docstring.

### Round 13 (head `915c3a73b`)

One finding, and it is class C a tenth time — so the circuit breaker applies and the
scope decision is recorded here before the edit rather than after it.

**Why this one is not another instance of the same fix.** Rounds 7 through 12 were all
about *when* a fact could be read: an observation existed and some ending, hand-off, or
buffer stood between it and the ledger. This one is not about reachability at all. The
call arrives with every fact intact and the ledger throws it away, because it asks
`canonical_model_id` — the *admission* question — about an identifier admission never
governed. Two policies, no owner of the question "what may key a usage row".

**The contradiction was written down, not merely implied.** Two tests were shipped side
by side and both passed: `test_a_persisted_model_id_past_the_bound_still_loads` asserts a
model ID longer than `MODEL_ID_MAX_LENGTH` stays loadable and keeps its length, because
per the persisted-shape rule a file an older release wrote must load; and
`test_an_unusable_identifier_is_never_persisted[oversized-model]` asserted that the same
shape's usage is dropped. Three comments stated the invariant the pair violates — the
`MODEL_ID_MAX_LENGTH` header ("a model config accepts is always a model usage can
meter"), `_text`'s docstring, and `record_many`'s "unreachable for anything the hub
admits". A false invariant asserted in prose next to a test that proves it false is how
this survived nine reviews.

**Scope decision: one owner, and it folds instead of refusing.** `usage_ledger_key` joins
the two existing spellings in `identifiers.py`. Admission may answer no — a request
naming a 4KB model is refused and nothing is lost — but metering cannot, because the call
already happened and was already billed. So the ledger's bound became a fold: a value
that fits is its own key, a longer one is keyed by its readable head plus a digest of the
whole identity. Both key fields go through it on both the read and the write path, which
is safe because the fold is idempotent by construction (a folded key is longer than any
verbatim one, so it is returned unchanged) and injective by digest rather than by a prefix
padding could collide with. `source_id` gets the same treatment for the same reason: its
config pattern is `src_[a-z0-9]{8,}`, unbounded above, so it belongs to the same
population.

**One deliberate behaviour change beyond the finding.** The old rule also dropped an
identifier that is nothing but padding. That is the same class one probe further on — a
padding-only ID is loadable too — so the refusal set is now exactly what no config can
hold: not text, or empty. `usage_ledger_key` returning `None` and
`ModelHubModelConfig.from_payload` raising are now the same predicate, and
`MH-USAGE-006` asserts that equivalence against the config constructor itself rather than
against a list of rejected shapes, so the two halves cannot drift apart again without
failing. The structure guard keeps each module to its own question: the service admits and
may not borrow the ledger's, the ledger meters and may not borrow the service's, and
neither may spell the bound.

### Round 14 (head `500fb55af`)

One finding, and it is a defect in round 13's own fix rather than a new instance of
class C: no call is dropped now, but two calls can be attributed to one identity. The
breaker still applies — same property, eleventh head — so the decision is recorded
before the edit.

**The claim that was false.** The sentence "a folded key is longer than any verbatim
one, so it is returned unchanged" appears twice in round 13's prose and once as the
reason one function could serve both directions. The code did not implement it: the
verbatim branch admitted anything up to `USAGE_LEDGER_KEY_MAX_LENGTH`, which is
*exactly* the length every folded key has. The two populations overlapped at that
length, and the overlap needs no preimage attack — whoever writes the config picks any
over-long ID `X`, computes `X[:200] + "~" + sha256(X)` themselves, and stores that
literal string as a second model's ID. Both models load, both route, and the ledger
merges them onto one row. The reviewer's phrasing names the real defect: a "string
namespace that legacy IDs can already occupy".

**Why the obvious fix is not enough on its own.** Folding everything past
`MODEL_ID_MAX_LENGTH` makes verbatim (≤ 200) and folded (= 265) disjoint by length, and
then no admissible identifier can occupy the folded form — reaching that length means
being folded. But it also breaks the idempotence round 13 relied on: a stored folded key
fed back through the same function folds a second time and orphans its own row, which is
a mutation the existing `MH-USAGE-006` assertion already catches. Recognizing the folded
*shape* instead would restore idempotence and reopen the hole, because a legacy
identifier can carry any shape marker too. No marker closes this; only a length no
admissible identifier can reach.

**Scope decision: split the direction, not the shape.** Deriving a key for a call that
already happened and accepting a key a row already carries are different questions, and
only the second may answer no — a row is not a call, it is what an earlier write claims
about calls, so a key no write could have produced is a corrupt row and refusing it
loses a claim rather than a served call. `usage_ledger_key` therefore folds past the
admission bound and never refuses an identity; `persisted_ledger_key` recognizes what a
write derived and bounds the row. `record_many` derives once, where a live call becomes
a row, so `_normalize_row` only ever sees keys — the double duty that function was doing
is what made a single self-idempotent function look necessary in the first place.

**Evidence.** `MH-USAGE-006` now asserts the disjointness (`len(key)` is within the
admission bound or exactly a folded key's length, never between) and the pairing between
the two directions in place of self-idempotence. `MH-USAGE-007` states the property the
finding violated as injectivity over a closure — every seed plus the key it folds to,
each one asserted loadable through `ModelHubModelConfig.from_payload` — so the crafted
pair fails it without being named, and a read-back test fixes which keys a row may
carry. Four mutations, four distinct failures: the late fold threshold, a re-deriving
read path, a truncation-only fold, and an unbounded read.

### Round 15 (head `5507c5708`)

CI went fully green and the review returned four P2 findings: one more instance of the
identity-keying property — its third consecutive head, at a new call site — and three
first instances in code this PR introduced. The diagnosis is recorded before the edits.

**F1: the label join was keyed by the identity, not by the row.** `usage_summary` built
`{source.id: source.display_name}` and looked it up with `row["source_id"]`, which is a
*key*. A source whose ID is past the admission bound therefore reported `label: null`
while still existing, and a rename never appeared. The reviewer's one-line fix — key the
map with `usage_ledger_key` — is correct and insufficient: this is the third head on
which a call site had to remember the keying rule, so the property-ownership rule
applies and ownership has to move rather than the site being patched. The ledger is the
only component that knows its rows are keyed, so `summary` now takes the labels as
*identities* and keys them itself, and `usage_summary` no longer touches a row's key
fields at all. The model half ships with it instead of waiting for the reviewer to reach
it: a folded model row publishes a key that is not the identity the user typed, so the
tab has to join that identity back the same way a source label is joined.

**F2: the queue was bounded by arrival rate, not by anything we measure.** The writer's
own docstring claimed the backlog "is bounded by what arrives during a single fsync
however hard the hub is driven". That is only a bound while an fsync is fast — under a
hung disk it is a restatement of "unbounded", which is the same failure mode as rounds
13 and 14: prose asserting a bound the code does not enforce. The three ways out are not
equal. A capacity that drops loses exactly the billed usage this module exists to keep;
backpressure stalls a served turn on a hung disk; folding loses nothing, because the
ledger already folds calls onto `(day, source, model)` rows when it writes them. So the
queue now folds on arrival: calls that share a day, a source, a model, and whether they
reported tokens become one queued row carrying a `requests` count, and the pending set
is bounded by the identities config holds rather than by traffic. Grouping on *whether*
tokens were reported is what keeps `token_reports` derivable from `requests`, so no
second additive field is needed and `token_reports <= requests` holds by construction.

**F3: a dropped batch was invisible.** The `OSError`/`ValueError` branch logged at
`debug`, so metering that stops because the state directory is read-only looks exactly
like metering that has nothing to record. It now warns on the transition into that state
and on recovery out of it, which bounds the volume by the number of state changes rather
than by the number of failed flushes — a counter would need arithmetic to say the same
thing less precisely.

**F4: the new route expanded the compat surface.** `GET /api/models/usage` was added as
`@app.route`, which the repo's Web UI Server rule reserves for the migration scaffold,
and it reached the ledger through the sync 300s RPC from the compat threadpool. It is
now a native FastAPI route awaiting an async client method. The guard that keeps it that
way is the mirror image of the one already covering the controller: the ledger read
blocks on the same lock writers hold across fsync, so *every* path into it must be off
the thread that would otherwise be serving requests, in the UI process as much as in the
controller.

**Evidence.** `MH-USAGE-008` states F1's property as a join that survives folding: a
source whose ID is past the admission bound gets its label, and a rename shows up
immediately. `MH-USAGE-009` states F2's bound as one that traffic cannot move — 512
calls over four identities leave four queued rows and still meter 512 requests — and a
drop test fixes F3's transition warning. Three structure guards close the two classes
against the next call site: `usage_ledger_key` has exactly one importer and
`usage_summary` may not subscript a row's key fields; every UI function that reaches the
read must be async, awaited, and served through the native dispatch, and the client
method behind it may not be sync; and the list of ledger reads both loops scan for is
asserted to be the whole of what the service exposes, so a second reader fails a test
rather than escaping both name lists. Eight mutations, eight distinct failures.

Making the read async also exposed a latent defect in the response-conformance driver,
which enumerates every route in `api-response.schema.json` and drives it against a stub.
The stub was the controller service, so the driver had been asserting that the routes
work against a shape no deployment has — the routes call the *client*, whose
`usage_summary` is async precisely because the service's is sync. `GET /api/models/usage`
returned HTTP 500 there while working in the browser. The stub now takes its sync/async
shape from `ModelHubRemoteService` itself rather than from a list of method names, so the
next async-only read is carried without an edit. With that fixed, the driver validates a
real summary against `usage-summary.schema.json`, which is what makes `label` a contract
rather than a field the ledger happens to emit.

### Round 16 (head `a85a88569`)

Four P2 findings, and three of them are one class: **metering was positioned relative to
our bookkeeping instead of relative to the upstream call.** That is the class name, and
naming it is what makes the remaining members predictable — every ending that settles a
turn is a member, and so is every property that follows from *when* the row is taken.
The fourth finding is unrelated and closed on its own terms. Per the property-ownership
rule the fix moves ownership rather than patching the sites.

**F1: a settlement that raised took the billed row with it.** Metering was each ending's
own step, written after that ending's bookkeeping, so an exception on the way through
settlement skipped the recorder — the vendor had already billed the call, and our ledger
never heard about it. The sites were the members; the owner was missing. `_settle_metered_turn`
is now the single ending owner for every shape a turn can end in, and `await self._record_usage(execution)`
is its first executable statement, ahead of every branch that can fail. Doing it exactly
once needed a second piece: the boundary that catches the raise re-enters the same ending,
so "is a row still owed" had to stop being each caller's inference. `_TurnExecution.owes_metering`
is now that one answer, read by the recorder and by the abandonment boundary alike.

**F2: the row was dated by when bookkeeping got around to it.** The recorder read the
clock itself, so a call that ended at 23:59:58 and settled at 00:00:01 landed in the next
local day — the wrong day's usage for a call the vendor billed on the previous one. The
completion instant is now captured where the upstream body ends, which is the one place
that has a body to end, and carried into `record(at=...)`; the writer keeps
`metered_at = min(call.at, persisted_at)` so a row is dated by its call and clamped
forward only by the persist instant. What is worth recording is *why this one is not a
behavior test*: with the recorder as the first statement of the ending, nothing suspends
between the capture and the queue, so no clock can advance in between and no test can make
one. The unreachability is the fix, so the guard asserts where the instant comes from
rather than driving a gap that no longer exists.

**F3: the durability wait was spelled at each call site.** Metering waits out its own
write so a client that opens the usage tab right after its call already sees that call.
Two call sites each wrote their own version of that wait, and the gateway's was
unbounded — hold the single thread ledger writes run on and a served response waits
behind a disk that has stopped answering. `UsageWriter.wait_recorded` now owns both
halves once: shielded, so a timed-out write is queued rather than cancelled, and bounded,
so the convenience cannot become the turn's critical path. `service._meter_call` was
migrated onto it instead of keeping its own copy.

**F4: canonical duplicates were repaired on live writes instead of refused.** Collapsing
a duplicate hop is right for a persisted tree we must be able to load, and wrong for a
request we can still answer: the caller that named one model in two spellings gets half
of what it sent, silently. The two doors are now distinct — `from_payload(..., repairing=True)`
collapses, the default raises — and there is exactly one repairing door,
`V2Config.from_payload`. `create_source` already refused, because it round-trips through
`to_payload()`; only the route-hop path reproduced. Client config saves cannot reach the
repairing door either way, since `vibe/api.py` strips `model_hub` from them.

**Evidence.** `MH-USAGE-010` states F1's property in both shapes — buffered and streamed
reach the ending from different frames — with `requests == 1` rather than `>= 1`, because
exactly-once is the half that the re-entering boundary threatens. `MH-USAGE-011` states
F3's property as its two halves at once: the turn is served while the ledger hangs, and
the row is still queued rather than dropped. `MH-USAGE-012` states F4's property as one
payload through both doors with opposite answers, which is why it is one test rather than
two. Two structure guards carry what behavior cannot reach: the recorder must be the
ending's first statement and the ending must be the only one there is, and the completion
instant must be captured in `_resolved_response` and carried rather than re-read at the
write. Six mutations, six distinct failures.

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
