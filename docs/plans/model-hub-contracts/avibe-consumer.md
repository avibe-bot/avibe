# Avibe Agent consumer

Change contract for C-6, 2026-10-02. This is the Model Hub producer contract for
the consumer requirements in docs PR #2333. Backend registration and the Agent
adapter remain C-8 work.

## Resolution

`ModelHubRuntimeRouter.resolve_hop(requested_model, *, process_scope, turn_id=None)`
returns the serialized `HopResolution` in `hop-resolution.schema.json`. It calls
the existing `resolve("avibe", ...)`; `ModelHubLaunch.to_hop_resolution()` projects
that launch. There is no second planner, credential store, or upstream client.

The backend id is `avibe`. It has a fixed catalog (ordinary editable model rows),
no native protocol pin, no native CLI, and no Direct mode. Until its starting
supply is seeded, the Avibe row is pending: a fresh configuration, or one written
before the Avibe Agent existed, reads an empty Hub row that writes leave absent.
The seed runs once, when a Source it can place exists: in the background after
the service reports ready for Sources that already exist (so a first models.dev
fetch never delays startup), and otherwise inside the mutation that creates the
first eligible Source, API key, OAuth, or native takeover alike, once that
mutation's Sources are final, so no restart is needed. Its source order becomes every existing eligible Source, placed exactly as
a newly created Source would be, and its catalog becomes the models the built-in
Claude, Codex, and OpenCode Agents run, in that order, that one of those Sources
lists, deduplicated and added as the picker adds a provider model (models.dev
metadata included). None qualifying leaves the catalog empty; no model is picked
on the user's behalf. An Avibe Agent without a model cannot run a turn, so the
built-in one takes its catalog's first model while it has none: one controller
rule, decided under the Agent store's write lock so a chosen model stands, run
after each seed and at every start, so a lost hand-off heals on the next start.
Where a user Agent already holds the name and no built-in exists, only the
supply is seeded.
The persisted row then belongs to the user and is never seeded again. Existing
backend modes, model metadata, routes, source order, and runtime intent remain
unchanged. An empty Avibe catalog does not prevent an explicit runtime Stop.

The primary currently selected hop owns `protocol`, `provider` (the Source's vendor
identity), and `source_id`. `base_url` is the loopback gateway's `/avibe/v1` API root;
the consumer appends `messages`, `chat/completions`, or `responses` according to
`protocol`. `runtime_model` is the exact prepared request model; the route credential
retains the caller's catalog alias through remapping and failover. The token is
in-memory only and excluded from the launch repr. `request_headers` is a required
map (empty when unused) carrying any gateway correlation metadata; it is never
omitted. The caller supplies a Session-specific process scope
and the current Avibe turn id for durable turn provenance, then retires the scope
through the existing router lifecycle when the consumer shuts down.

Planning capabilities retain the requested catalog row's authority, including for
aliases. Unknown `context_window`, `max_output_tokens`, `supports_tools`, and
`supports_reasoning` remain null. A nonempty input modality list yields
`supports_images = ("image" in input_modalities)`; an empty list yields null.
All capability fields are required in the response, even when their value is
unknown. There is currently no separate stored input cap, so `input_limit` is
present with value null, never omitted.
`reasoning_efforts` remains the configured list, or empty when reasoning is
explicitly disabled. No upstream limit or capability is guessed from a model name,
a different hop, or a native CLI default.

The consumer resolves once per run and again after a retryable failure. Failover,
recovery, Source health, and credentials remain owned by Model Hub.

Candidate admission also preserves this capability authority: Source metadata
first, then exact catalog metadata. Avibe never receives the native-backend
protocol/model-family default reasoning ladder. An undeclared ladder is empty;
undeclared boolean/numeric capabilities remain null.

## Request origin and sequential retry

The prepared route credential retains the immutable primary `(provider, api,
model)` from launch, including untracked launches without a turn id. This origin
travels as in-memory request metadata, never as upstream JSON or a caller-supplied
header. Each revalidated Avibe attempt compares its admitted origin against that
original primary, including fallback, credential refresh, and recovery walks.

If any origin component differs, or the primary origin cannot be verified, the
gateway strips opaque history **before engine invocation and protocol conversion**:

- Block `signature`, `thoughtSignature`, `thought_signature`, and encrypted
  reasoning payloads are removed, including protocol-owned tool-call extensions.
- Anthropic `redacted_thinking` blocks and opaque-only reasoning items are dropped.
- Visible thinking and Responses reasoning summaries become ordinary assistant
  text; their text survives, but reasoning item identities do not.
- Tool ids, arguments, results, and unrelated request options remain unchanged.
  A tool argument named `signature` is user data, not protocol metadata.

Same-origin fallback, even on another Source, retains the original history. Each
attempt gets its own cleaned history; cleaning never mutates the original request
that a later same-origin attempt might reuse. Native callers retain their existing
history behavior.

After a request completes, the consumer may resolve the **same catalog alias and
turn** again to obtain a different primary hop. The existing turn keeps its attempt
history and final successful served origin while replacing only its prepared
launch snapshot. A prior response's remaining owned teardown is drained before
replacement; an overlapping live request or a changed alias is refused with
`409 mapping_target_unavailable` before a model call, without poisoning the first
request's attribution. Retired route credentials may still route late continuations
but cannot claim or invalidate the replacement's identity, even for malformed JSON.
Native route-conflict and ambiguity rules are unchanged.

## Hub-only boundary audit

`avibe-boundary-matrix.json` is the executable audit table. It lists every schema
that declares a backend discriminator, its runtime owner, and the refusal cases.
Tests discover discriminator sites and run the listed schema/runtime refusals;
adding a new shape without a policy fails the audit.

| Boundary | Avibe invariant |
| --- | --- |
| Supply/config/catalog | Hub mode; no CLI presence, Direct mode, or native protocol pin |
| Source order/manual route | Native Sources rejected by the shared eligibility owner |
| Chain/probe | Only Hub channels; no native runnable candidate or native probe result |
| Attempts/served/canceled | Only Hub channels at admission and provenance recording |
| Terminal/local failures | Hub producer or null local attribution, never native CLI |
| Recovery | Reuses the same Hub admission and attempt slots; live annotation adds no channel |
| Guard/adoption/event references | Reference or diagnostic identity, not a separate transport grant |
| Native migration scan | Avibe absent; released native shapes remain unchanged |

## Response origin

Every Avibe model response that the gateway serves carries the response header
`x-avibe-served-hop`. Its value is compact ASCII-escaped JSON:

```json
{"provider":"openai","api":"openai_responses","model":"upstream-model"}
```

The exact object is `hop-origin.schema.json`. The provider and protocol are
captured from the Source snapshot used at attempt admission, and the model is the
configured upstream target of that attempt. The same immutable `HopOrigin` travels
with `ResolvedInvocation` and the request's `AttemptIdentity`; successful settlement
stores it as the required `TurnProvenance.served.origin` for Avibe. Historical and
native records may omit origin; Avibe has no pre-v11 historical record.
Failed/canceled attempts can also retain that snapshot, without implying success.
For a turn making several model requests, each response header names its own
request's hop; the turn-level `served` retains the last successful attempt under
the existing settlement rules.

Headers are committed after resolution reaches the engine's first-output barrier
and before any response bytes. Avibe never takes the early Anthropic keepalive
path, whose headers could otherwise leave before a fallback hop is known.
Native CLI keepalive and HTTP-status behavior remain unchanged. Buffered and
streaming Avibe responses use the same origin header, including tool-only answers.
The header identifies the producer; a later stream error is still terminal and
does not certify a successful response.
Local errors before a serving hop exists have no origin header; they cannot
claim an upstream producer.

The exact header value is bounded to **4096 ASCII bytes**, including JSON syntax
and escapes (excluding the header name and HTTP framing). Every Avibe hop is
checked against this bound at invocation admission, including fallback and
credential-refresh attempts. An oversized value is refused with HTTP `422` and
machine error `served_hop_too_large`, before invoking that hop: no attempt, Source
health penalty, or served-hop header is fabricated. Earlier failed attempts remain
recorded. The gateway uses its existing localized generic error copy. Identifiers
are never truncated; persisted config/history loading and native CLI/gateway
consumers retain their existing identifier rules. Resolving a launch alone does
not invoke a model and does not enforce this transport-only bound.

Buffered and bodyless terminal upstream failures retain the admitted origin
header as well. A bodyless upstream terminal response leaves resolution through
`ModelHubError`, which carries the same immutable admitted origin. Both outcome
carriers use one response-origin header policy. Local engine failures,
pre-admission refusals, and local exhaustion responses carry no origin even when
an earlier hop was attempted; the gateway never substitutes the last attempted
hop or looks up current Source metadata to invent a producer.
A local delivery failure after admission can report that known producer in the
header without changing the existing engine-down provenance rule (which leaves
Source attribution null rather than blaming the upstream for a local failure).

Consumers parse this header before committing a response. The wire protocol stays
the requested frontend protocol; `origin.api != requested protocol` explicitly
identifies cross-protocol conversion. Consumers record the reported upstream
origin, never the original primary hop. Missing or invalid origin follows C-2's
unverified-origin rule (drop opaque signatures/redacted thinking).

No management HTTP endpoint, persistent request lookup, or transcript copy is added.
The header needs no upstream credentials and is never forwarded upstream.

## Validation and delivery boundaries

- Released-shape loading: old modes/routes/capabilities survive without mutation;
  the new row is empty Hub, and native-cli Sources remain ineligible.
- Starting supply (MH-AVIBE-007): an unrelated write keeps the unseeded row absent;
  the seed places existing Sources for Avibe alone and keeps only routable Agent
  models; a second start leaves the persisted row, including removed Sources, alone.
- Real loopback gateway requests through the existing service, with hermetic engine
  fixtures: primary protocol resolution, cross-protocol failover, non-ASCII origin,
  slow Anthropic resolution, buffered/streamed bodies, and concurrent requests.
- The same attempt's response header and persisted served origin agree, even if
  Source metadata changes while the response is in flight.
- One explicit mandatory-field table removes every promised resolution field,
  capability field, and served identity/origin field from real produced values.
  Native records from each supported persisted generation still load without an
  origin. The terminal response table crosses result carrier, body availability,
  upstream/local cause, primary/fallback hop, and native/Avibe backend.
- Cross-origin histories are checked at engine admission across all three existing
  frontends, same/different protocol fallback, recovery, and launch-time config
  changes. Same-origin and native histories retain their payloads.
- Candidate-to-catalog-to-launch tests protect unknown/explicit capabilities.
  Real gateway retries, overlap refusal, and valid/malformed late credentials
  protect sequential route replacement.
- Contract authority/version closure, focused Python tests, and the existing UI
  type/build checks run before push.

The v11 consumer introduced the three existing protocols. The v12 extension in
[`google-protocol.md`](google-protocol.md) adds the Google runtime/engine frontend
together with the vocabulary, while preserving the origin delivery rules. There are
no new UI elements in this lane. C-8 owns enabling the Avibe backend card and its
Hub-only presentation; `cli_present` truthfully remains false here.
