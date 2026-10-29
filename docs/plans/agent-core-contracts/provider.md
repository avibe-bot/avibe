# C-2 Provider adapter interface

One adapter per protocol in `core/agent_core/ai/`. Adapters speak HTTP to the endpoint they are given and know nothing
about Model Hub routing.

```python
class ProviderAdapter(Protocol):
    protocol: Literal["anthropic", "openai_chat", "openai_responses"]

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ProviderEvent]: ...


@dataclass(frozen=True)
class ModelEndpoint:            # built by the adapter layer from C-6 HopResolution
    protocol: str
    base_url: str
    token: str                  # field(repr=False): never logged or rendered, never written to the transcript
    model_id: str               # the runtime model id to send
    request_headers: Mapping[str, str]
    provider: str               # vendor identity of the hop (HopResolution.provider); with protocol and model it
                                # forms the target origin for the cross-provider rules
    source_id: str | None       # HopResolution.source_id: the pin of a request with native hosted content
    source_identity: str | None # HopResolution.source_identity: the account its opaque payloads are bound to
    portable: bool              # never pinned, and no native hosted content (vibey-hosted-tools.md §3.5)
    opaque_refused: frozenset[tuple[str | None, str]]   # (Source identity, payload kind) pairs whose provider
                                # refused that kind in this Session; its messages go without them (cross-provider.md)
    hosted_tools: Mapping[str, HostedToolRoute]   # HopResolution.hosted_tools, in Model Hub's order: per enabled
                                # search tool its mode and replay, and for native its declaration and Responses
                                # include values (vibey-hosted-tools.md §3.4)
    image_models: ImageModels | None   # the models image_generate may call; None when the tool is off


@dataclass(frozen=True)
class ModelCapabilities:      # from C-6 HopResolution; None means Model Hub does not know
    context_window: int | None
    input_limit: int | None
    max_output_tokens: int | None
    supports_tools: bool | None
    supports_images: bool | None
    supports_reasoning: bool | None
    reasoning_efforts: tuple[str, ...]


class MediaLoader(Protocol):   # supplied by the adapter; reads media_objects
    async def load(self, media_token: str) -> tuple[bytes, str]: ...   # bytes, mime type


class MediaSink(Protocol):     # supplied by the adapter; keeps a generated image as a local file
    async def save_image(self, encoded: str) -> HostedImage: ...       # base64 in; path, sniffed mime, size out


@dataclass(frozen=True)
class ModelRequest:
    endpoint: ModelEndpoint
    system: str
    messages: Sequence[Message]          # canonical (C-1); the adapter applies cross-provider.md
    tools: Sequence[ToolSpec]            # C-7; may differ on every call
    max_tokens: int
    reasoning_effort: str | None         # one of the hop's declared efforts, or None
    cache: Literal["default", "none"]    # "default": the protocol's cache breakpoints and their lifetimes
                                         # (conversion-tables.md); "none" for the compaction summarizer
    supports_images: bool                # resolved by the loop; unknown capability counts as False
```

`tools` is the one owner of what a request declares. A hosted tool is its canonical `ToolSpec`
(`core/agent_core/tools/hosted.py`, `hosted_tool_spec`). On a hop whose route for it is native, the adapter sends the
route's `declaration` verbatim in the spec's place, merges a Responses route's `include` values into `include`, and
pins the request with `x-avibe-pin-source: <source_id>`; on any other hop the spec is an ordinary function. Vibey
lists its hosted tools first, so Anthropic's tools breakpoint, which goes on the last function tool, never moves.

History follows the same Source rule. A response's hosted calls go back verbatim only to the hop the request goes to,
when that hop's Source served them on the same origin (`conversion-tables.md`, Hosted tools): within their turn as a
continuation, later with the hop's current `replay`, and pinned to that Source either way.

**Refusals and changed-request recovery** (`vibey-hosted-tools.md` §3.5). Except for the corroborated Anthropic
search recovery below, refusals require no output. The gateway answers a pinned request it cannot serve
from that Source with `409 pin_unavailable`, matched on its `type` and `code`, and names the narrow hosted rule that
fired in `error.reason` when one did. A provider refuses an opaque payload replayed under an account that cannot read
it (a key change, or a relay serving a later turn from another pooled account, which no Source identity can see) with
a 400 naming it: xAI's plain-text `Could not decrypt the provided encrypted_content`, which does not say which kind;
Anthropic's `Invalid \`encrypted_content\`` (or `encrypted_index`) for a search result (kind `hosted`) and
`Invalid \`signature\` in \`thinking\` block` (kind `signature`), on any request. The adapter ends either send with a
`ProviderError` that has no output, is not `retryable`, and carries a `refusal`: `pin_unavailable` with the hop's
Source and the rule's tool and aspect, or `opaque_refused` with the hop's Source identity and the kinds refused (both
for xAI's). The loop answers it by rendering the request again, never by the generic retry:

- by a fresh hop, rendered normally, at most once per (Source, tool, aspect) a narrow rule names and at most twice
  per run, whatever the reason;
- in portable form on the hop it has, past either bound, or at once when a hand-back's fresh hop is the refused one
  again (the same Source, identity, model and hosted-tool facts). A portable request is never pinned, so the gateway
  never hands it back;
- after `opaque_refused`, with each refused (identity, kind) in `opaque_refused`, so none of that identity's payloads
  of that kind is sent again: its earlier search calls take their portable form (the query and sources, readable;
  no encrypted data), and its thinking is left out, as a different origin's (`cross-provider.md`). The loop keeps
  each new pair as a `replay_refusal` row before resending, so the Session's later turns, and its forks, start
  without them (`transcript.md`). Each pair is answered once; refused again, the error ends the run.

A `pause_turn` continuation's `pin_unavailable` comes after output, so it is no refusal (`conversion-tables.md`).

**Anthropic search replay compatibility** (`search-history-recovery.md`). An `invalid_request` from HTTP 400 or
SSE may carry the run-local `search_history` refusal when its precise search ciphertext/citation error corresponds
to native call/result pairs actually sent. A resolvable `messages.N.content.M` path narrows the match; a path that
does not name such a pair is only a hint, since intermediary indices may differ. The exact
assistant-prefill rejection is weaker evidence: it permits a trial only when canonical context ends in a real client
result, the wire ends in a tool-result-only user message answering all preceding client calls beside a pending native
search, and the compiler can demote completed search history. It is not a search diagnosis by itself.

The loop tries once per Source identity per run, on the same hop with only web-search `replay=False`. The existing
compiler preserves in-flight native content, declarations, saved client results, and signatures, while rendering
completed history portably. The decision is not persisted or learned as permanent capability evidence. If an explicit
search error instead names ciphertext remaining in the current exchange, the existing `opaque_refused(hosted)`
fallback remains available once, including after a history trial, and persists only the released `hosted` payload kind.
Absent ciphertext and already-demoted history do not authorize an unchanged retry.

Both search paths may replace an uncommitted partial: audit the failed attempt's usage and recompile before committing
any response. Failed client calls never execute, completed client results are not replayed, and Vibey publishes no
failed narration or duplicate search card. `retryable` and `continuable` stay false: this is a bounded changed-request
repair, not generic replay or an outer transcript continuation. Other refusal kinds retain their no-output boundary.

The loop's `ModelRouter` returns the `ModelEndpoint` together with its `ModelCapabilities`. Every capability has a
defined effect, including when it is unknown:

| Capability | `true` / value | `false` | unknown (`null`) |
| --- | --- | --- | --- |
| `context_window` | W for C-9 | — | the configured default window (128,000) |
| `max_output_tokens` | upper bound for `max_tokens` | — | the configured default (8,192) |
| `supports_tools` | tools sent | the route is refused before the run with a clear error: the agent needs tools | tools sent; a provider rejection surfaces as an error |
| `supports_images` | images sent | placeholders | placeholders |
| `supports_reasoning` | `reasoning_effort` sent if it is one of `reasoning_efforts` | `reasoning_effort = None` | `reasoning_effort = None` | Adapters are constructed with a
`MediaLoader` and resolve each `ImageBlock`'s bytes at request time; the canonical message never carries bytes. The
token names an immutable snapshot (`message.schema.json`), so the bytes never change for a committed block.

`stream` yields `ProviderEvent`s (`provider-event.schema.json`) and always ends with exactly one `done` or one
`error`. `done.message` is the complete canonical `AssistantMessage`; its `origin` comes from the served-hop report
(C-6), which the gateway sends as the `x-avibe-served-hop` response header: compact JSON with exactly `provider`,
`api`, `model`, `source` (Model Hub contract v12), before the first model byte. The `origin` is `provider`, `api`,
`model`; `source` is the serving Source id, which is not part of a message's origin. A report of any other shape
counts as no report. On the direct channel (no gateway) the endpoint is the served hop, and its `source_id`, when it
names one, is the serving Source. When a gateway response carries no
served-hop report, the origin is unverified: the adapter commits the message with every opaque provider payload
removed (the `signature` of every block of any kind, and redacted thinking), keeping the endpoint as `origin`, so no
opaque payload of an unknown vendor can ever be replayed. A block without a signature is always sent without one
(`cross-provider.md`).

Error classification is part of the contract because the loop branches on it:

`retryable` permits replay of the same request. `continuable` (default false) separately permits a new run
from the committed transcript after the failed run closes. Classified transient errors grant continuation even
when streamed output forbids replay. A `model_hub_recovery_exhausted` ending remains non-retryable: for an
explicit pre-output `server` or `network` cause, it may grant Vibey one of its existing bounded transcript-only
continuation runs, which cannot replay the original input or committed tools. Rate-limit/quota exhaustion, missing or
future causes, explicit pin/opaque refusals, permanent/protocol errors, unclassified failures, and local preparation
errors grant no continuation. Neither the terminal sanitizer nor a downstream consumer may infer permission from
`kind` alone. A recovery-exhausted error after thinking remains non-continuable, because the partial response is not
safe to replay or continue implicitly. A failure of any kind that arrives before any output, at least the silence bound
after its request was sent, is neither retryable nor continuable: sent again, the request would most likely wait as
long again (`conversion-tables.md`, network timeouts).

The loop carries both permissions and `retry_after_s` into the terminal `AgentError` unchanged. These fields do
not grant permission to replay committed tools or manufacture user input; Vibey uses `Agent.resume` under its
own bounded continuation policy (`loop-control.md`).

| `kind` | Meaning | Loop action |
| --- | --- | --- |
| `overflow` | the request exceeded the model's context (Pi's overflow patterns and Anthropic's refusal of input plus `max_tokens` over the window, HTTP 413, `context_length_exceeded`) | overflow recovery (plan §5.2) |
| `rate_limit`, `overloaded`, `network`, `stalled`, `server`, `interrupted` | transient (`network`: the provider could not be reached; `stalled`: a connected provider sent nothing for the silence bound; `interrupted`: the provider's stream ended after output without a terminal, as Model Hub reports with its own `modelHub.errors.stream_interrupted` frame) | retry with backoff, honoring `retry_after_s`, only when nothing was streamed (no deltas, no `partial`), at most 3 retries within 120 s; then the run ends with that error. After streamed text or a tool call the error is terminal for the request, matching Model Hub's first-byte rule; after thinking alone it is retried once (below) |
| `auth`, `invalid_request` | not retryable | end the run with the error, except for the corroborated search changed-request repair above and strict final tool-argument recovery below. Neither permits generic replay or execution of malformed calls. |
| `aborted` | cancelled by the caller | end the run as aborted |
| `unknown` | anything else | end the run with the error; after thinking alone, retry once (below) |

**A stream that dropped after thinking alone** (owner decision, 2026-10-11). When a response fails after it
streamed thinking and nothing else (no text, no tool call), with a `network`, `stalled`, `server`, `overloaded`,
`interrupted`, or `unknown` error, the `ProviderError` has `retryable` and `after_thinking` set. The loop retries such an error at
most once per request, inside the same count and time budget; a second drop ends the run with that error, because
some prompts drop at the same point every time. The dropped attempt's thinking is never context: it stays in the
attempt ledger and its audit (C-9 `context.md` §10, invariant 4), and the retry resends the original request.
A refusal or safety stop is a response, not an error, and `auth`, `invalid_request`, `overflow`, and `rate_limit`
after thinking stay terminal. The unknown-wire exception grants one request replay only, not transcript
continuation; an unknown local exception does not acquire the thinking-drop exception.

A `ProviderError` whose `partial` is committed (a response that failed after output) carries the error's text as that
partial's `error_message`, redacted like the error itself and bounded to 1,000 characters, so the transcript row says
why the response ended. A Stop's partial carries none.

A strict final tool-argument parse failure is the only `invalid_request` that grants transcript-continuation
permission (distinct from the search changed-request repair above). Its `partial`
keeps text, thinking, and all canonical tool calls, replacing every malformed argument payload with `{}`. The
`malformed_tool_calls` identities tell the loop which calls are unsafe: it commits the assistant row, records one
model-visible error `tool_result` for each such call, executes valid sibling calls once, and then returns a bounded
transcript-only continuation permission. The malformed provider bytes are never logged or persisted. If the continuation
fails, ordinary provider-error handling reports the actionable failure; unrelated malformed metadata remains terminal
`invalid_request` with the normal partial policy.

The `ai` lane adds a conversion table per protocol (request fields, stream events, stop reasons, usage fields) to this
directory with its first PR; its tests enumerate the tables.
