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
(discovery and custom-model mapping) store what it returns, so config and resolution
cannot disagree about what "the same model" means. Metering preserves that identity
through `usage_ledger_key`: keys up to the shipped 200-character verbatim threshold
remain unchanged and longer identities fold to the same bounded key across upgrades.
The ledger never re-runs admission because a persisted legacy model must remain
meterable even when a new request could no longer add that identifier.

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
buffers, and that owner is the sole stream-observation caller of `prelude.write`, so an arrival site added
later cannot reorder the two questions by forgetting which comes first. The overflow path
then reaches `ended()`, which has attached the wire's report to the outcome since round 7.
`test_wire_bytes_reach_the_observer_before_the_buffer_that_can_refuse_them` states the
ordering; `test_a_prelude_that_dies_after_reporting_tokens_carries_them_to_the_resolver`
drives it, with a complete `message_start` preceding the failed read.

**Later correction (2026-08-30).** The 16 MiB refusal was itself an invalid protocol
rule: official response schemas do not impose that byte ceiling, and image-generation
SSE events can legitimately carry much larger Base64 strings. The prelude now spills to
a temporary file without a total response ceiling, while the wire observer keeps only a
bounded metadata projection and never changes the forwarded bytes. Its total pre-output
wait remains bounded by the transport deadline, so an upstream cannot renew disk growth
with keepalives forever. Observation-before-copy remains the owner ordering, but copy can
no longer refuse a valid response for its size.

The same rule applies to buffered JSON. Its exact bytes spill once and are replayed from
that owner; a single taxonomy-backed selective JSON projection extracts terminal errors,
allowlisted machine fields, and usage for both small and large bodies. The projection
lexes only those finite protocol paths and skips unrelated subtrees without materializing
their values. Body size therefore changes storage only, never classification or metering,
and HTTP error diagnostics keep
only a bounded private prefix after the machine fields have been projected from the full
body.

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
read-modify-write staying off the event loop, and a pre-output report surviving a later
stream failure.
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

### Round 17 (head `27aae667e`)

Three P2 findings, and **all three repeat a class already seen**. The circuit breaker is
tripped, so the round opens with the diagnosis rather than an edit. Inventory across the
five findings-bearing heads: class **K** — *identity keying done by whoever happened to
need it* — is on its 4th head (R13, R14, R15, R17); class **B** — *a bound stated at the
wrong layer* — is on its 3rd (R15, R16, R17).

**Scope decision, recorded: continue, no owner escalation.** The architecture is not what
is wrong. Ledger-owns-keying survives R17 — no caller keyed anything this round — and
one-serialized-writer survives too. What was never stated as an *owned property* is
narrower than either: **identity arity** and **worker abandonability**. Each finding has
exactly one terminal rule, each is reversible and contract-preserving, and two of the
three *delete* a concept rather than add one, which is what makes the smallest complete
action clear enough to take without asking.

**F1: a model's label was keyed by its ID alone (class K, 4th head).** A common model ID
exists precisely so several Sources can offer it, so a flat `{model_id: label}` answered
for whichever Source asked — a model removed from Source A kept its label there for as
long as Source B still listed it, and a retained row read as though its own identity were
still configured. The previous three rounds moved *who* does the keying; none of them
asked what a metered model's identity *is*. It is the pair. Two flat 1-arity maps cannot
express a 2-arity identity, so no amount of relocating the lookup could have closed this,
and the class was never going to end while the argument shape still admitted the mistake.
`summary()` now takes `Sequence[SourceIdentity]` — the shape config actually holds, Sources
each carrying their listed models — and derives both key levels itself. Arity lives in the
type, so there is no keying convention left for a caller to hold. Model labels are derived
on the way through (a model's label is its own identity), so the caller no longer passes
`{model.id: model.id}` at all: one concept deleted, not relocated.

**F2: `datetime` conversion was assumed total (class B, 3rd head).** A value near either
end of the representable range, offset far enough, leaves that range on the way to another
zone and raises `OverflowError` — an `ArithmeticError`, so it passes straight through a
handler written for bad data, out of the flush task, and stops metering for the rest of the
process while the row that caused it stays on disk to do it again. The tempting fix is a
year range, and the project rule rules it out: a bound with a free parameter is not the
terminal rule, it is the next thing to probe. **The conversion is the bound.** `_carried`
performs both conversions this module makes — UTC for the spelling it publishes, local for
the day it buckets — and returns nothing when either refuses, so what escapes it is safe
at both by construction. The two doors differ in what they do with a refusal, and correctly
so: persisted text degrades to absent, while a caller's moment cannot be lost, so it dates
the row by the only instant the module can measure. The `(OSError, ValueError)` handlers
were deliberately *not* widened — that would be a second answer to the same question and
would hide the next real bug; the door makes the class unreachable instead.

**F3: the write was bounded but not abandonable (class B, same round).** Every wait this
module makes a caller do is bounded, which is what keeps a served turn off a disk. Shutdown
is where a bound stops being the question: the write is still running after the last
bounded wait has already returned and told its caller the row was unfinished, so all that
is left is whether the process may walk away. `ThreadPoolExecutor` will not — its workers
are non-daemon and it registers an `atexit` hook that joins them — so a worker wedged in
`fsync` holds interpreter shutdown open forever, and a stop or restart waits on optional
metering. `_AbandonableWriter` is one daemon thread over a `queue.SimpleQueue`, keeping the
`Executor` interface so `run_in_executor` still bridges it to the loop. Same single idle
thread, same call sites; the atexit join that *was* the whole defect is gone.

**Evidence.** `MH-USAGE-013` states F1 as the biconditional over every pair the ledger
holds — a label is published exactly when config lists that model under that Source — with
both directions asserted, because a join that labels everything and one that labels nothing
each satisfy half of it, and over both key populations so the scoping survives the fold.
`MH-USAGE-014` seeds the two spellings that break the naive conversion regardless of the
machine's zone and asserts first that they still raise `ArithmeticError`, which is the
finding itself; then both directions of the door in one test. `MH-USAGE-015` runs a real
subprocess, because `atexit` hooks and non-daemon joins run after the interpreter has
finished with `__main__` and nothing in-process distinguishes a worker that will be
abandoned from one that will be waited on forever. Two structure guards carry what behavior
cannot: `usage_summary` may construct no mapping at all (a map built there is a join key
whose arity the caller chose), and no module in the package may build a pool or start a
non-daemon thread — scanned package-wide rather than asserted about the one class that had
the defect, since the next worker inherits the rule. Six mutations, six distinct failures;
the pool mutation fails by hanging the child for the full timeout, which is the defect
verbatim.

## Todo

- [x] Usage taxonomy and extraction in `stream_wire.py`
- [x] `BoundedUsageLedger` in `core/handlers/model_hub/usage.py`
- [x] Resolver and gateway recording over the two disjoint call populations
- [x] `ModelHubService.usage_summary` + RPC + `GET /api/models/usage`
- [x] Contract row, response registry entry, and usage schema
- [x] Unit and contract coverage
- [x] Stage 2: draw the tab against this data, as 用量

## Stage 2 — what shipped

**The tab is named 用量 / "Usage".** The label was 用量与额度 / "Usage and quota",
and the 额度 half has exactly one production writer:
`core/handlers/model_hub/migration.py` writes `"cycle_used_pct": None`. Every
non-null value in the repo is a mock or a schema example. So this is not a naming
preference — a quota reading would be an invention, and the tab is named for what
it can show. If a quota source ever appears, the label grows back with it.

**Where the data becomes a view.** `usageProjection.ts` owns every derivation the
tab could get wrong — the trailing local-day window, what a row may display, and
the densification of a sparse trend — so each one is asserted as a property rather
than inspected in JSX. `UsageTab.tsx` owns layout and copy and translates nothing
into numbers. Three properties are worth naming because getting them wrong would
be a lie rather than a glitch, and each is a catalog row (MH-USAGE-016..020):

- the caption states the `window_days` the server *served*, never the number the
  control asked for;
- a `token_reports` shortfall reads as reports that never arrived, never as unused
  capacity, which the schema forbids in as many words;
- a model whose label is gone shows no identity at all, because its ledger key is a
  digest; a gone Source keeps its `src_*` id, which is a string the user has seen.

**The read is the tab's own.** It is deliberately not a member of
`FIRST_PAINT_REGION_WHITELIST`: the landing decides routing, and a report nobody
is looking at must not delay it. One effect owns both the open and the window
change, since a window change is the same read over a different span, and
`beginRegionRead` keeps the previous figure on screen while the new one lands.

**Geometry.** `design.pen` has no frame for this tab on the local surface, so the
block adapts `MS/ConfigPanel → cp-usage-body` and otherwise follows the source
table directly above it — same 18px gutter, 36px head, 11px column labels, 12px
radius. Importing a second panel's spacing into the middle of this one would read
as two surfaces stitched together. Two deliberate departures: the day series is a
column chart because the day count is a window parameter rather than a fixed five,
and the panels stack because the table carries nested rows. No light-mode branch
was needed — every ink resolves through a step both light blocks already re-anchor,
and the chart's two colors are a wash and a `color-mix` accent, which flip on their
own.

**Catalog.** `tests/scenarios/model_hub/test_model_hub_catalog.py` previously
resolved a row's evidence with `ast.parse`, so the capability's user-visible half
could not be registered at all. A `.ts`/`.tsx` row now cites its own ID
(`…/UsageTab.test.tsx::MH-USAGE-018`) and resolves to the single executable
`it('MH-USAGE-018: …')` declaration that carries it. A vitest case's name is its
docstring, so that is where the ID belongs — the same per-row greppability the
Python docstring rule gives, and the evidence is runnable by ID
(`vitest -t MH-USAGE-018`) rather than only greppable.

The first version of this checker sliced each case's body instead and asked whether
the ID appeared inside the slice. Two properties are why it was replaced rather than
patched. Deciding where a case *ends* needs balanced delimiters, and a scan that
small cannot tell a regex literal from division — so `/Couldn't refresh/i`, ordinary
in this suite, read as a string opening and the slice swallowed eight sibling cases.
And its guard against exactly that (re-parse the slice, require one declaration) ran
the same scan, so the desync agreed with itself and passed. Reading only names
removes the failure instead of guarding it: a wrong guess can lose a declaration,
which fails the row loudly, but can never hand a row a neighbour's ID. Verified
against vitest's own collection over the whole Model Hub UI suite — 873 collected
cases, every unresolved one a parameterized `it.each` whose name does not exist in
the source, and nothing resolved that vitest does not run.

**Dead 额度 code removed with the rename.** Copy: `settings.models.usage.monthSpend`,
`settings.models.usageTab`, and `settings.models.tabs` (a zero-caller duplicate of
`shell.tab`). Formatters: `formatSpend` and `currencySymbol`, which had zero callers
already on `master` — they render `month_spend_cents`, whose one writer is
`migration.py`'s `None`, so the amount they format is unobtainable in production and
keeping them only invites someone to wire it back up. What remains is
`types.SourceUsage` and the `mockData` 额度 fields: the type mirrors
`docs/plans/model-hub-contracts/source.schema.json`, which still declares the block,
and is retired when the contract retires it.

### Stage 2, round 2 (head `5957182`)

Two P2 findings, **no class repeat** — head 1 (`7082c882e`) carried one finding about
catalog evidence resolved textually, which neither of these touches — so the breaker is
not tripped and the round is two closures rather than a diagnosis.

**F1: the day series read a missing token report as an idle day.** `token_reports <=
requests` is a contract, and the gap means *reports that never arrived* — the tab already
says exactly that for the totals (MH-USAGE-018). The day series then contradicted it: it
scaled and captioned days by tokens alone, so a window whose upstreams all answered
without token counts drew every bar at zero and captioned itself
「没有任何一天有计量数据」 — our own missing evidence reported as the user's idleness.

The finding named the peak/quiet copy. The class — *the day series asks tokens a question
only requests can answer* — has exactly three members inside `ByDayPanel`: that copy, the
column height, and the per-column tooltip. So the fix is one predicate, not one call site:
`UsageDayColumn` carries `requests` alongside `tokens`, `usageDayIsMetered` is the single
place the series asks whether a day ran, and all three members read it. A no-peak window
now splits into the two different windows it always was — one nobody used, and one whose
upstreams never said what it cost (`byDay.quiet` vs `byDay.unreported`).

Ruled *out* of the class on evidence rather than by assumption, by reading
`core/handlers/model_hub/usage.py`: `usageIsEmpty` gates on `sources.length`, and
`summary()` builds source rows from the same request-driven rows, so an unreported window
still has sources and is not empty; the stat cards, `cached.none`, and the chart's
aria-label each state a token figure they really do own.

**F2: deleting the last Source took the ledger with it.** `directEmpty` was a top-level
routing fork, so the Frame 09 landing replaced the whole tab shell — including the Usage
tab. But the ledger outlives the Sources it meters: retention is 62 days and MH-USAGE-017
exists precisely because a vanished Source keeps its `src_*` id in the report. A user who
deletes their last source loses the only route to what it cost, and a user reading the
report when a deletion lands is thrown off the tab mid-read.

One rule replaces the fork: **the Hub always has its two tabs, and `directEmpty` decides
the body of the `sources` tab.** That is fewer concepts than the alternative (a usage
affordance inside `DirectHome` plus a route back out of it), and it fixes the mid-read
eviction for free. MH-USAGE-022 states the property over `Record<ModelsSurfaceKind, …>`,
so a third landing fails to compile rather than shipping without a route.

**Known-by-design: Frame 09 is drawn without the tab strip.** The frame predates this tab
and the Usage tab has no frame at all, so the frame's silence is the absence of the
concept, not a decision about it. What Frame 09 decides is the *body* of the `sources`
tab, and that is still Frame 09 there — asserted, including that no gateway-overview
content leaks in beside it.

**Evidence.** MH-USAGE-021 draws a window where every request came back unreported and
asserts the copy plus each day's readout. The bar geometry itself is not asserted there:
the floor is `max(2px, …)`, an inline CSS function jsdom's parser drops, so `style.height`
reads empty for drawn and undrawn columns alike. The decision behind the floor is the
assertable part and it is asserted where it lives — `usageDayIsMetered` over columns in
`usageProjection.test.ts` — leaving only the pixel to the residual visual check that this
tab already carries.

### Stage 2, round 3 (head `d22cb479`) — breaker tripped

Two P2 findings. **F1 repeats head 1's class on a second reviewed head**, so the breaker
is mechanical: stop patching, diagnose at orchestrator level, record the scope decision.

**The class: a text scan deciding which vitest declarations execute.** Head 1 flagged that
the catalog accepted a non-executable case; the round-2 fix answered with a hand-written
JS lexer in `test_model_hub_catalog.py` — comments, string and template literals, a regex
heuristic. Head 3 then flagged a regex opening after `return`, which the heuristic reads as
division because it only inspects the previous *character*.

Naming the remaining members is what ends this. Measured against the fixture, the lexer
accepted a fake declaration in **five** shapes — a regex after a keyword, JSX text, a
`describe.skip` body, an `if (false)` body, and its own division heuristic — and the
reviewer had reached one. Two of the four it never reached (`describe.skip`, `if (false)`)
are ordinary code, not adversarial constructions. The class has no last member by
construction: telling a regex from division needs the parser's state, and telling a skipped
or unreachable declaration from a live one needs the run. Every future round buys one more
patch and leaves the property unproved.

**Scope decision (orchestrator, no owner escalation needed): delete the lexer and ask the
collector.** `vitest list` answers "does this case run" definitively, and `npm test` — which
is `vitest run` — already executes in CI's `build-linux-artifacts` job on the same commit,
so the real collector costs no new job and no new dependency beyond a declared `js-yaml`.
The action is reversible, contract-preserving (row shape and `path::ID` syntax unchanged),
and smaller than what it replaces: 219 lines of scanner deleted for 126 lines of resolver
plus gate.

Duties split so neither side overclaims:

| Question | Owner | Why there |
| --- | --- | --- |
| Row shape, cited file exists, ID greppable in it, no `expected_fail` on a UI row | `test_model_hub_catalog.py` | `ast`/text answers these exactly |
| Does the cited ID name exactly one case vitest **runs** | `ui/scripts/validate-scenario-catalog.mjs` | only the collector knows |

`scenarioCatalog.mjs` holds the resolution rules with no subprocess in them, so they stay
unit-testable; the gate discovers `tests/scenarios/*/catalog.yaml` rather than listing
them, reads each catalog's own `status_legend` so the two sides cannot drift into
disagreeing policies, and throws if `tests/scenarios` is absent — this gate must never
report "nothing to check" for the one input it exists to read. It collects only the cited
files (9 rows, 3 files, ~0.3 s), not the whole 231-file suite.

MH-CATALOG-002 moves to `ui/scripts/scenarioCatalog.test.mjs`, seeds one declaration of
every shape, and asserts the set that resolves *equals* the set the fixture names as
running — derived from the fixture text, so a shape added later is covered without editing
the assertion. `it.each` is no longer disqualified on principle: the surviving property is
"exactly one collected case named for the row", and a 2-row `it.each` fails it by count.

**F2: every non-peak day's figures existed only in a hover `title`.** Keyboard,
screen-reader, and touch users could reach the endpoints and the peak from the axis and
nothing else — and `role="img"` on the chart hides the columns from assistive tech by
design. One `readout(column)` helper now feeds both the tooltip and an `sr-only` list
rendered as a *sibling* of the image (inside it, it would be hidden with everything else).
No new i18n key: `byDay.column` is the same wording, so the two readings cannot disagree —
which is how MH-USAGE-023 asserts it, tooltips against list items rather than against a
list of days.

### Stage 2, round 4 (head `82c46d5a0`) — breaker tripped again

Two P2 findings, **both class repeats**, so the breaker trips a second time and both
closures are made at the class owner rather than at the flagged site.

| Class | Heads | What recurs |
| --- | --- | --- |
| A | 1, 3, 4 | A row can read `covered` while nothing executable backs it |
| B | 3, 4 | The report states per-row figures through visual position alone |

**Class A — the gate asked one catalog and passed three in silence.** Round 3's gate
selected rows through each catalog's own `status_legend`, and `status_legend` maps a status
to a *description string* in every catalog here but `model_hub`. Reading `test_required`
off a string yields `undefined`, so `harness_command_task`, `memory_list`, and
`message_delivery` contributed zero rows — the gate reported a clean 9/9 while three
catalogs went unasked. That is the same failure the gate exists to prevent, one level up.

The reviewer proposed normalizing the scalar legends. Rejected: inferring `test_required`
from prose (`'deterministic scenario or contract coverage exists'` → true?) is a heuristic
over English, the exact species of thing round 3 deleted from the Python side. **A row that
*cites* a UI file must resolve, whatever its legend looks like** — simpler and strictly
stronger. A citation is the row's own claim, exists in every catalog, and no legend shape
can switch it off. Coverage went 9 rows / 1 catalog → 20 rows / 4 catalogs; collection
still rides `npm test`, 1.7 s.

Three citation shapes now resolve under one rule, because one notion (prefix) is what they
have in common — a rule per catalog would be a policy each new catalog could contradict:

| Shape | Written as | Resolves against |
| --- | --- | --- |
| Catalog ID | `path::MH-USAGE-024` | the case's own name |
| Readable full name | `path::taskCommandPreview quotes an argv part …` | the full name with `' > '` flattened to `' '` |
| File only | `path` | the file being collected at all — *deleted in round 5, below* |

Two shapes that resolve to a running case and still prove nothing are rejected explicitly: a
row citing a **sibling row's ID** (checked against the whole catalog's ID set, including
pytest-evidenced rows), and a file-only citation whose file collects nothing.

The gate immediately caught a real pre-existing defect it had been unable to see:
`harness_command_task` **SCT-016** was `covered` on `taskCommandPreview quotes argv the way
the shell would read it back`, a case name that no longer exists. Re-pointed to the live
case; the row's claim was true, its citation had rotted.

**Class B — one mechanism for every panel of per-row figures.** Round 3 answered the by-day
chart with an `sr-only` sentence per day; head 4 flagged the by-source table, whose figures
were unattributed `<span>`s in a CSS grid. Answering the second panel with a second
mechanism invites a fifth round, so both panels now carry the same structure.

Why roles and not a native `<table>` for the visible panel: the layout is a CSS grid
(`--model-hub-usage-columns`) that collapses to one column below 767px, and a `<table>` can
only stack through a `display` override — overriding `display` is exactly what strips the
native table semantics it was chosen for. Explicit roles are unaffected by `display` and
keep both the grid and the structure at every width. The by-day list became an `sr-only`
native `<table>` (Tailwind's `sr-only` does not touch `display`), so the tab has one answer
for "figures need row and column association" instead of two.

A figure's column is **its position in its row** — which is what makes the header row an
answer rather than decoration, in an ARIA table exactly as in a native one — and the cell
repeats its header as a `md:hidden` label for the width where the header row is `display:none`.
Between them the two cover both widths, so a per-cell `aria-colindex` restates what position
already says; it earns its keep only for a row that skips a *middle* column, and the model
row's empty `lastMetered` cell is the simpler way to not have one. Mutation-checked before
deciding: dropping `aria-colindex` changed no observable association and no assertion, so it
went.

MH-USAGE-023 is reframed against the table (its `<li>`s are gone), still deriving every
expectation from the tooltips so the two readings of a day cannot answer differently.
MH-USAGE-024 is the class property, asked of *whatever* tables the tab renders: every body
row has exactly one row header, states as many figures as there are columns (no row ends
early and shifts the ones before it), and — where the headers can leave the accessibility
tree — labels every figure with the header at its own position. Four mutations were run
against it and all four fail: dropping `role="table"`, removing the model row's placeholder
cell, removing the stacked labels, and reordering `SOURCE_COLUMNS`.

### Stage 2, round 5 (head `28baeb512`) — class A, fifth appearance

One P2 finding, and it is class A again: **a row can read `covered` while nothing executable
is tied to *that row*.** Heads 1, 3, 4, 5. Each round's fix was correct about the level it
was shown and defined the property by what the existing rows happened to say — which is what
produced the next level.

| Head | What the gate accepted | The question it was actually answering |
| --- | --- | --- |
| 1 | a commented-out declaration | does the cited case run |
| 3 | a regex literal read as division | the same, one layer deeper |
| 4 | rows selected through `status_legend` | which rows get asked |
| 5 | a file-only citation | what counts as an answer |

Round 4 accepted the file-only shape **because three legacy rows used it**. The reviewer
showed why that cannot hold: `MEMORY-LIST-006` and `MEMORY-LIST-007` both cited
`MemorySearchPanel.test.tsx` and nothing else, so deleting either row's case leaves the other
row keeping the file collected and *both* rows green. A file runs for reasons that have
nothing to do with the citing row.

**Terminal rule, with no free parameter left for an adversary to probe:** a citation must
name a case, and that name must resolve to **exactly one** case the collector observes in the
cited file — for **every** citation a row makes, not for one key. The shape is deleted rather
than weakened; there is no ID heuristic bolted onto a bare file.

**The member the reviewer had not reached, which is where the rot was.** `memory_repair`
states its UI half as `ui_contract: {test, case, inputs}`, so reading `row.test` alone left
all five of its fully-written citations unasked — the round-4 legend hole again, in a
different key. Four of the five were dead. `git log -S` traces three to `d6ea9ee0f`
(#1401, merged 2026-08-14), which deleted 623 lines from `SettingsMemoryPage.test.tsx` and
368 from `MemoryStatusPanel.test.tsx`; nothing read `ui_contract`, so the false claims sat
there for five days. Those three `ui_contract` blocks are deleted — `status`,
`expected_outputs`, and `related_tests` untouched, since only the case-level claim was
provably false and asserting anything more about another capability's semantics is not mine
to assert.

`related_tests` and `canonical_tests` stay unread, and that is **the same rule rather than an
exception to it**: they name a file and no case, so they cannot evidence a row — and they do
not claim to. "Related" is a pointer.

Two mechanics follow from measurement rather than from taste:

- **Containment, not prefix.** `[MEMORY-LIST-004][MEMORY-LIST-006] browses …` is one case
  answering two scenarios, each citing it by its own ID, so an ID is not always first. The
  looseness is bounded by the pre-existing uniqueness count — a name reaching two cases fails
  for the same reason as one reaching none.
- **Parameterized cases without emulating printf.** `vitest list` *does* expand `it.each`, so
  a citation's terms are the template's literal head plus each `inputs` entry, all contained
  in one collected name. The literal tail is dropped rather than parsed because the count is
  what decides: for `accepts the declared failure %s with result %s` with
  `[memory_repair_failed, timed_out]`, one collected case carries all three terms and the
  other seven carry two.

The three bare-file rows were fixable as citations alone — their cases already carry their
IDs, which §4 of the scenario-testing standard has required all along, so a bare-file row was
the standard not being followed rather than a second legitimate convention. Zero test renames.
Coverage went 20 rows / 1 legend-selected catalog → **22 citations across 22 rows**, and the
standard now states the property so the next catalog writes citations that pass.

### Stage 2, round 6 (head `aa051476e`) — a new class: a composite npm script swallows filters

One P2 finding, on a line round 5 introduced, and it is **not** class A — it is the first
appearance of a different class: **a composite npm script forwards `--` arguments only to its
last command.** No breaker trip; a first appearance is a finding, not a pattern.

```
"test": "vitest run && npm run validate:catalog"
npm test -- UsageTab.test.tsx
  → vitest run && npm run validate:catalog UsageTab.test.tsx
```

npm appends the arguments to the *end* of the whole string, so the filter landed on the
validator — which takes no path and ignored it — while vitest ran all 231 files unfiltered.
Both halves still exited 0, so the failure is silent: the focused run AGENTS.md asks for
first quietly becomes a four-minute full suite, and the argument that was supposed to narrow
it is discarded by a command that never wanted it.

The class has exactly one other member in `ui/package.json`, and checking it is what decided
the fix. `"build": "npm run validate:imports && tsc -b && vite build"` is composite too, but
its argument-taking command is **last**, so `npm run build -- --mode=x` reaches `vite build`
by accident rather than by design. A rule of the form "put the arg-taking command last" is a
rule about command order that nothing enforces and the next composite breaks again.

**So the fix deletes the mechanism instead of wrapping it.** `"test"` returns to
`vitest run`, and the gate reaches CI as a vitest case: `checkCatalogs()` is exported from
`validate-scenario-catalog.mjs` with the CLI behind an `import.meta.url` guard, and
`scenarioCatalog.test.mjs` asserts on it. A test case has no argument to misroute, and
`npm test` already means "everything that must hold", so the gate needs no second entry
point in CI to be reached.

Both alternatives the reviewer offered were declined for the same reason. A wrapper script
that parses `--` and forwards to each command keeps the hazard and adds a layer that hides
it. A separate unfiltered CI command re-splits "what must hold" across two callers, which is
how the gate could be forgotten on the next workflow edit — and `build-linux-artifacts`
already runs `npm test`.

What proved it: `npm test -- UsageTab.test.tsx` now runs 1 file / 14 cases, and the gate case
fails with the resolver's own message when `MEMORY-LIST-006`'s citation is rotted to a
nonexistent ID (reverted after checking). Full suite 231 files / 2943 tests, CLI gate 22
citations across 22 rows, `tsc` clean, `eslint` clean, `npm run build` ✓.

### Stage 2, round 7 (head `f5991f2a6`) — breaker tripped: a token figure states a number without saying it was measured

One P2 finding, and it is a **class repeat on a second reviewed head** (`5957182` →
`f5991f2a6`), so the breaker trips mechanically. As the orchestrator in a user-started
session I recorded the scope decision and continued: the smallest complete action is clear,
reversible, and changes no contract.

The finding is about the copy round 2 introduced. `byDay.zero` claims 「这个区间的 token 用量
是 0」 for a window whose upstreams never reported anything, because `usageDayColumns`
carried `requests` and `tokens` and **dropped `token_reports`** — so no site in the day
series could ask the coverage question even if its copy wanted to.

**Why this is the same class, and why round 2's closure could not have held.** Round 2 named
the class *the day series asks tokens a question only requests can answer* and closed it with
`usageDayIsMetered`. That predicate is about activity, and it is still right about activity.
What round 2 missed is that a bucket carries **three independent facts, not two**:

| Counter | The question it alone answers |
| --- | --- |
| `requests` | did calls happen |
| `token_reports` | did an upstream say what they cost |
| `input_tokens + output_tokens` | how much |

A rendered `0` is therefore three different readings — a cost reported as nothing, a cost
nobody reported, and nothing having run at all — and round 2's projection could distinguish
only the third. Round 2 also explicitly ruled the stat cards *out* of the class, on the
evidence that they 「each state a token figure they really do own」. Ownership was the wrong
test: owning the figure says nothing about whether the figure is a measurement.

**The inventory, which is what makes this a class and not a line.** Every token figure the
tab states, found by grepping every token expression in `UsageTab.tsx` rather than by
recalling the panels — **8 members**, one of which round 2's ruling had cleared:

1. the tokens stat card's value
2. its input/output split note
3. the cached-input card's note — 「No input tokens in this window」, the same defect one card
   over: with nothing reported there were no input tokens *we know of*, and an absence of
   reports is not an absence of usage
4. the by-model row cell
5. the by-source row cell
6. the day bar's tooltip readout
7. the sr-only day table cell
8. the peak / no-peak sentence

**The fix is one door plus an enumerable marker.** `useTokenText(counters, value)` is the only
path from a token count to text, and coverage travels *with* the value rather than being
checked by the caller, so a new token figure cannot be written without naming the counters it
came from. `TokenFigure` wraps it in `<span data-usage-token="">`, which is not styling: it is
what lets a test enumerate every node-shaped token figure on screen and assert they all went
through the door — completeness by construction, which a per-site test cannot claim. Figures
interpolated into translated sentences have no element of their own and call `useTokenText`
directly; their sentence is then the asserted unit.

**Two predicates, deliberately not one.** The first draft was `token_reports > 0` alone, and
it would have shipped the mirror image of the defect it fixes: a day where nothing ran cost
nothing, and that zero is measured by *our own* request counter rather than promised by an
upstream. Blanking it reads an idle day as unknowable. So:

- `usageTokensAreReported` = `token_reports > 0` — an upstream costed something. Used by peak
  selection and by the claim about what the reports said.
- `usageTokensAreKnown` = `usageTokensAreReported(c) || c.requests === 0` — the number is a
  measurement, so printable. Used by the figure door.

Only a day whose tokens were reported can be the busiest one: a day with no report has no
measured cost to compare, so naming it the peak puts a superlative on a number the report
never carried. And the no-peak copy becomes four-way with **`reported` ordered before
`metered`** — a window with any report in it can state what its reports said, and the calls
that came back without one are the requests card's shortfall to name, not this sentence's.

**The backend premise, verified rather than assumed.** A reported zero really does exist on
the wire: `extract_protocol_usage` (`core/handlers/model_hub/stream_wire.py:533`) returns a
non-null report for an explicit all-zero usage block — `_usage_sum` returns `0`, not `None` —
and `usage.py:464` sets `token_reports = call.requests if usage is not None else 0`. So
MH-USAGE-025 draws a window that exists, not a hypothetical.

**What proved it, by breaking it.** Rotting `usageTokensAreKnown` to `return true` fails
MH-USAGE-021 and MH-USAGE-026 and nothing else; swapping the `reported`/`metered` branch order
fails MH-USAGE-025 and only it. Both restored. Full suite 231 files / 2950 tests, catalog gate
24 citations across 24 rows, model_hub catalog pytest ✓, `tsc` clean, `eslint` clean,
`npm run build` ✓.
