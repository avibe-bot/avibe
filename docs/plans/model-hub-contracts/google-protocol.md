# Google protocol consumer extension

Normative for the v12 Model Hub contract. This extends the existing gateway,
EngineAdapter and pinned CPA transport; it does not add a provider implementation
to agent-core, change backend registration, or upgrade the engine.

## Source admission and observation

The complete protocol vocabulary includes `google`. New Google API-key Sources
are admitted with `vendor: "custom"`, explicit `protocol: "google"`, an explicit
Base URL and the usual transient `key` through the existing observe/create APIs.
Existing vendor pins, subscriptions, OAuth and native CLI configuration do not
change. The UI protocol picker still contains only its three approved options.
Existing Source details can label Google; its tier suggestions are empty.

The Base URL is the upstream origin or a proxy root, optionally ending in
`/v1beta`; that suffix is removed only for CPA configuration because its Gemini
executor appends it. Query-bearing roots are refused before Source registration,
because this executor appends paths rather than preserving a Base URL query.
Discovery calls `<root>/v1beta/models`, supplies the held key
as `x-goog-api-key`, and follows `nextPageToken` on that same endpoint. Redirects,
malformed pages and repeated continuation tokens fail the whole observation;
partial inventory must never replace a saved list. Model names lose only their
`models/` API resource prefix. Inventory does not invent limits, vision support
or reasoning tiers.

Google is not added to custom Auto detection: its inference endpoints require a
model in the path. Add-time observation performs no model call. For an explicitly
declared protocol, the existing credential witness rule applies: a listing that
accepts the supplied key and rejects the same uncredentialed request establishes
authentication; public lists do not. Explicit unverified save remains available.
A user-requested saved-model probe uses a Gemini `contents` request.

## Consumer and transport

`ModelHubLaunch.to_hop_resolution()` and the runtime router return `protocol: "google"`
and the gateway frontend Base URL ending in `/avibe/v1beta`. Required
`request_headers`, gateway token, runtime model, Source identity, provider identity
and nullable capabilities have exactly the same owners as in `avibe-consumer.md`.

The gateway accepts:

- `POST /<backend>/v1beta/models/<runtime_model>:generateContent`
- `POST /<backend>/v1beta/models/<runtime_model>:streamGenerateContent?alt=sse`

It also accepts omitted `alt` or `$alt=sse` on the streaming path, always using SSE.
Other `alt` values are refused before admission. Model and streaming mode come
from the path, not JSON `model` or `stream` fields; those fields are refused.
`countTokens`, embedding/batch APIs and query-string credential authentication are
not consumer endpoints. A caller uses the returned gateway token through Bearer
authentication or `x-goog-api-key`; upstream keys never cross this boundary.

The engine request routes `Source.prefix/model_id` in the URL and uses the Google
frontend regardless of the selected Source's protocol. CPA owns native or
cross-protocol request/response conversion. Avibe performs no duplicate translator.
The engine remains v7.3.16 at upstream `c404af96` with the existing Avibe patch.

### Pinned-engine limitation and admission skip

For **Avibe streaming only**, a Google frontend request cannot admit an
`openai_responses` Source hop: the pinned CPA Responses/Codex-to-Gemini translator
does not emit `finishReason` for `response.completed`. Before transport/recovery
admission, skip that hop with stable reason
`google_stream_responses_unsupported` and continue the request's effective chain.
The reason is recorded on the Model Hub service diagnostic log record's `reason`
field with the exact Source/model identity. A skip is not an upstream attempt:
no health transition, cooldown, metering, failed-attempt entry, or served origin
is fabricated. If all selectable hops were skipped without an upstream attempt,
return local HTTP 422 with that same machine reason and no served-hop header.
If actual upstream attempts failed too, ordinary exhaustion still owns the turn.

Native CLI behavior is unchanged. Google buffered conversion, Google/Anthropic/
Chat streaming, and other frontends served by a Google Source are not skipped.
No usage frame or clean EOF is interpreted as a missing model terminal.
Fixing the engine and publishing a replacement managed-runtime asset is a
separate owner-authorized release operation, not part of this PR.

## Response facts and provenance

Google candidate content parts are model output; metadata alone is not. Native
`finishReason` or prompt blocking marks terminal completion; there is no `[DONE]`.
Native `error` envelopes (unnamed or CPA's `event: error`) are failures. The
existing no-fallback-after-output rule remains in force.

Token reports project `usageMetadata.promptTokenCount` (including its cached
subset), `cachedContentTokenCount`, and the sum of `candidatesTokenCount` and
`thoughtsTokenCount`. No report is guessed when the upstream provides none.
Both buffered and streaming responses share these facts with metering.

Local JSON failures use Google's numeric `error.code`, canonical `error.status`,
localized `error.message`, and `details[].reason` for the Model Hub machine code.
Late local streaming endings use the Google error envelope. All carriers retain
the v11 response-origin policy: `x-avibe-served-hop` comes only from the immutable
admitted invocation that actually supplied the response. Pre-admission/local
failures have no origin header.

Avibe fallback compares each admitted hop with the launch's primary origin.
For a different or unverifiable origin, Google `contents[].parts` lose opaque
signatures and visible thought text becomes ordinary text; tool arguments and
results are user data and remain intact. Same-origin requests retain signatures.
Cross-protocol fallback follows this same policy before CPA conversion.

## Validation and compatibility

The shared Source enum guard includes create, observation and Source-probe
contracts, runtime storage, config, adapter and UI type consumers. The new writer
generation is 12; persisted provenance generations 5 through 11 still load, and
released configuration fixtures retain their meaning. Only version literals
change in existing UI browser fixtures.

Owning tests cover config/runtime/YAML round trips, Google URL/body admission,
finite stream facts and metering, paginated credential witness/refusals,
same/different-origin gateway fallback and served provenance. Real pinned-engine
tests isolate all state and allow only loopback upstreams. UI validation uses the
existing hermetic Model Hub browser suite and production build.
