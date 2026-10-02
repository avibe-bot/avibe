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
no native protocol pin, no native CLI, and no Direct mode. Its default catalog and
source order are empty. Old configurations acquire only that empty Hub row;
existing backend modes, model metadata, routes, source order, and runtime intent
remain unchanged. An empty Avibe catalog does not prevent an explicit runtime Stop.

The primary currently selected hop owns `protocol`, `provider` (the Source's vendor
identity), and `source_id`. `base_url` is the loopback gateway's `/avibe/v1` API root;
the consumer appends `messages`, `chat/completions`, or `responses` according to
`protocol`. `runtime_model` is the exact prepared request model; the route credential
retains the caller's catalog alias through remapping and failover. The token is
in-memory only and excluded from the launch repr. `request_headers` carries any
gateway correlation metadata. The caller supplies a Session-specific process scope
and the current Avibe turn id for durable turn provenance, then retires the scope
through the existing router lifecycle when the consumer shuts down.

Planning capabilities retain the requested catalog row's authority, including for
aliases. Unknown `context_window`, `max_output_tokens`, `supports_tools`, and
`supports_reasoning` remain null. A nonempty input modality list yields
`supports_images = ("image" in input_modalities)`; an empty list yields null.
There is currently no separate stored input cap, so `input_limit` is null.
`reasoning_efforts` remains the configured list, or empty when reasoning is
explicitly disabled. No upstream limit or capability is guessed from a model name,
a different hop, or a native CLI default.

The consumer resolves once per run and again after a retryable failure. Failover,
recovery, Source health, and credentials remain owned by Model Hub.

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
stores it as `TurnProvenance.served.origin`. Historical records may omit origin.
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

Buffered terminal upstream failures retain the admitted origin header as well.
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
- Real loopback gateway requests through the existing service, with hermetic engine
  fixtures: primary protocol resolution, cross-protocol failover, non-ASCII origin,
  slow Anthropic resolution, buffered/streamed bodies, and concurrent requests.
- The same attempt's response header and persisted served origin agree, even if
  Source metadata changes while the response is in flight.
- Contract authority/version closure, focused Python tests, and the existing UI
  type/build checks run before push.

Known by design: this first PR supports the three existing protocols. Google
requires a second PR covering the runtime/engine frontend as well as vocabulary;
adding only an enum would claim a transport that cannot serve requests. There are
no new UI elements in this lane. C-8 owns enabling the Avibe backend card and its
Hub-only presentation; `cli_present` truthfully remains false here.
