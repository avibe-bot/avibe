# Vibey on native Gemini

Status: **plan**, 2026-10-27, owner-approved direction ("直接做第 2 步").
Scope: Vibey only. Claude Code, Codex and OpenCode are unchanged.

It implements the Google adapter that `avibe-agent-core.md` §3 item 3 always named ("Native protocols: … Google
Gemini") and the owner deferred from v1 (`agent-core-contracts/conversion-tables.md`, "Recorded Pi stream
differential"), and uses it to give Gemini models their own web search the way Claude and GPT already have theirs
(`vibey-hosted-tools.md`).

## 0. Owner decisions (2026-10-27)

1. **Vibey speaks Gemini natively on a `google` hop.** It calls the gateway's Gemini frontend
   (`/<backend>/v1beta/models/<model>:streamGenerateContent?alt=sse`, #2340) instead of Chat Completions, so Gemini
   features reach Vibey untranslated. Keeping Chat and asking the engine to carry Gemini's server-side tool parts,
   grounding and signatures through private Chat fields was rejected: it is a dialect nobody else speaks, it lives in
   an upstream translator, and the thought-signature failure fixed by #3025 is exactly that class of bug.
2. **Gemini's web search is Google Search grounding, declared natively** on a Gemini hop, as Claude's and GPT's are
   on theirs (`vibey-hosted-tools.md` §4.1 native mode). One behaviour on every surface (Web and IM alike).
3. **Google's Search Suggestions are not displayed.** The Gemini API Additional Terms ask an application to show
   grounded results "with the associated Search Suggestion(s)" and not to modify them. The owner judged that no
   agent product follows this (Google's own Gemini CLI returns only the answer and a source list to its model) and
   accepted the risk, which falls on the account that owns the API key. The public docs state where queries go and
   what they cost.
4. **A Gemini API key speaks the `google` protocol.** New keys use it, and an existing key on Google's
   OpenAI-compatible endpoint moves to it (§3, PR 2).

## 1. Facts this plan rests on

Measured on 2026-10-27 against the real Gemini API (`gemini-4-argon`, the regression environment's key) and the
pinned engine (CLIProxyAPI v8.0.30, `5be2cdab`):

- **Google's OpenAI-compatible endpoint cannot run Google Search.** `tools: [{"google_search": {}}]`,
  `{"type": "google_search"}`, `{"type": "web_search"}` and `extra_body.google.tools` are each `400
  INVALID_ARGUMENT`. Google documents grounding on that layer for the Images endpoint only.
- **The native API runs it next to function calling**, with `toolConfig.includeServerSideToolInvocations: true`;
  without it, `400 "Please enable tool_config.include_server_side_tool_invocations to use Built-in tools with
  Function calling."`. The response carries the search as parts: `toolCall {toolType: "GOOGLE_SEARCH_WEB", args:
  {queries}, id}` and `toolResponse {toolType, response: {search_suggestions}, id}`, each with a `thoughtSignature`,
  then the answer text, and `candidates[0].groundingMetadata` with `webSearchQueries`, `groundingChunks[].web {uri,
  title}` (Google redirect links, the title is the domain) and `groundingSupports` (text segments and the chunks
  they cite).
- **Price** (Gemini 3 and later): per search query the model runs, 5,000 free a month across those models, then
  $14 per 1,000. One request may run several queries (two in the probe).
- **The engine's Chat translation loses Gemini state**: the Gemini→Chat response translator drops every
  `thoughtSignature`, so a Chat client never holds one to send back, and the Chat→Gemini request translator then
  writes `skip_thought_signature_validator` on each call (the Antigravity translator writes it on every call
  unconditionally); neither sets `includeServerSideToolInvocations` or converts server-side tool parts or grounding
  (`internal/translator/{gemini,antigravity}/openai/chat-completions/`). A `gemini-api-key` entry serves the native
  frontend without translation.
- **The engine's images endpoint** serves GPT Image, xAI, and image models registered on an
  `openai-compatibility` entry only (`sdk/api/handlers/openai/openai_images_handlers.go`), not a `gemini-api-key`
  entry. Gemini image generation today relies on the OpenAI-compatible entry (`vibey-hosted-tools.md` §7.4).
- **Thought signatures** (Google, "Thought signatures"): Gemini 3+ validates the signature of the first
  `functionCall` part of every step in the *current turn* (since the last user message with ordinary content); a
  signature belongs to the exact part it came on; parallel calls carry it on the first call only; a response
  without calls may carry it on an empty final text part. A function call Gemini did not produce may carry the
  documented dummy `skip_thought_signature_validator`.
- **Model ids differ by surface.** The OpenAI-compatible listing names `models/gemini-4-argon`; the native listing
  (`_project_google_inventory`) yields `gemini-4-argon`.

## 2. Vibey's Google adapter (`core/agent_core/ai/google.py`)

Ported from Pi (`packages/ai/src/api/google-shared.ts`, `google-generative-ai.ts`, MIT, `7fbbd5f`) and tau
(`tau_ai/google.py`, MIT) onto the shared stream assembler, exactly as the other three adapters are. The
conversion table is in `agent-core-contracts/conversion-tables.md` ("Google Gemini").

- **Request**: `systemInstruction`; `contents` with `user`/`model` roles; `functionDeclarations` with
  `parametersJsonSchema`; `generationConfig.maxOutputTokens`; `thinkingConfig` with `includeThoughts` and the
  effort as `thinkingLevel` (Gemini 3 and later, the `-latest` aliases) or `thinkingBudget` (Gemini 2.5).
- **Signatures**: a same-origin signature goes back on the part it came on, only when it is well-formed base64 (a
  transcript written over Chat may hold Chat reasoning markers). The first function call of a current-turn step
  that has none (another origin's call, or an unverified one) carries the documented dummy, so a mid-turn failover
  onto Gemini is accepted.
- **Tool results**: one `user` content holds a step's `functionResponse` parts in call order (Gemini refuses
  interleaving); a result's images ride in `functionResponse.parts` on Gemini 3 and later, else in a following user
  content.
- **Effort tokens** stay Chat's (`registry.sent_effort`): the menu and the evidence keys Model Hub holds for a
  `google` hop do not move; the adapter maps the token to `thinkingConfig`.
- **Hop resolution** (`modules/agents/vibey/models.py`) stops rewriting a `google` hop to Chat, so every Vibey path is
  untranslated and `hosted_tools.runs_on` no longer excludes `google` Sources; their native hosted payloads are not
  replayed until §4 adds Gemini to `NATIVE_REPLAY_PROTOCOLS`.
- **Verified** against the real Gemini API (`gemini-4-argon`, `gemini-flash-latest`, `gemini-2.5-flash`): sequential
  and parallel calls with their signatures, another origin's current-turn call with the stand-in, and the same body
  through the pinned engine's `gemini-api-key` entry, which forwards it untranslated.

## 3. Gemini API keys on the `google` protocol (PR 2)

Model Hub contract v22 (`model-hub-contracts/README.md`). Every Gemini API key speaks Gemini natively, existing ones
included (owner, 2026-10-27: "统一都走原生").

- `vibe/data/api_key_vendors.json`'s `gemini` row pins `google` on `https://generativelanguage.googleapis.com`, and a
  `google` Source's vendor is `custom` or `gemini`.
- **Names do not move.** A Gemini Source keeps Google's `models/<id>` names (its OpenAI-compatible listing always used
  them), so no hop, route key, menu row or Session changes. Discovery keeps them for the `gemini` vendor only (a
  `custom` Google Source keeps bare ids), and the engine's `gemini-api-key` entry routes each model by that alias while
  calling the bare id upstream (measured on the pinned engine: `prefix/models/gemini-4-argon` is served).
- **Existing keys move on load.** A Gemini API-key Source saved on Google's official OpenAI-compatible endpoint, named
  or by default, changes only its protocol and endpoint, through the shared on-load migration (projected on every
  load, persisted at service start, `model-hub-upgrade-config-compatibility.md`). A relay URL under the vendor keeps
  what it speaks. The Source's identity changes, so stored opaque payloads stop replaying once and its capability
  evidence restarts.
- **Images stay one Source.** The engine's images endpoints serve only image models an OpenAI-compatible entry flags,
  so a Gemini Source also registers its image models on an internal OpenAI-compatible entry of the same key and
  prefix, on Google's `/v1beta/openai` root (measured: both entries register under one prefix). `IMAGE_PROTOCOLS`
  gains `google`. A Chat relay serving Gemini keeps Gemini's image priors (`prior_vendors`).
- **Downgrade:** a release before v22 refuses a `gemini` Source on `google`, so once a v22 build has saved the moved
  Source an older build cannot load Model Hub's Sources; removing the Gemini Source first avoids it.

## 4. Google Search as Gemini's native `web_search` (PR 3)

- The overlay's `gemini` provider rows (prefixes `gemini-3`, `gemini-4`; ids `gemini-flash-latest`,
  `gemini-flash-lite-latest`, `gemini-pro-latest`) declare `web_search` on `google` as `{"type": "google_search"}`
  with the per-query price ($14 per 1,000, as of 2026-10-27). Gemini 2.5 is not declared: it refuses a server-side
  tool next to functions (measured). A Source keeps Google's `models/<id>` names, so the overlay matches the id
  without that prefix.
- The adapter sends that declaration as `{"googleSearch": {}}` after the `functionDeclarations`, with
  `toolConfig.includeServerSideToolInvocations: true`, which Gemini requires to run it next to functions (measured).
- A response's `toolCall`/`toolResponse` pair (`toolType: GOOGLE_SEARCH_WEB`) becomes one `HostedToolBlock`
  (`action: search` with the call's `queries`; the record's sources are the response's `groundingChunks`, each
  once), with both parts verbatim, signatures included, as its native payload. Same-Source replay sends them back
  in place; any other target gets the synthetic call and result (`vibey-hosted-tools.md` §5). The
  `search_suggestions` markup is never kept (§0). A server-side part of a tool the request did not declare stays
  out of the transcript.
- Gateway: `hosted_admission` reads the keyed declaration through `request.declared_tool_types` and Gemini's
  server-side parts as replays (the in-flight response is the last `model` content followed only by function
  responses). Gemini's refusal "Tool call context circulation is not enabled" before output is the narrow negative
  rule for `web_search` `call`, so the hop falls back to the executor chain. Cost: the response's distinct `groundingMetadata.webSearchQueries` × list price, as
  `hosted_summary.counts.web_search_requests`.
- Not in this step: a positive evidence rule (the Tools tab shows no "最近一次 … 正常" for Google Search) and
  answer citations from `groundingSupports` (the record lists the sources; the text carries no links).
- Public docs (`avibe-docs`, en and zh): Gemini searches with Google, queries go to Google, and the price.

## 5. Delivery

| PR | Scope | Visible result |
| --- | --- | --- |
| 1 | §2, this plan, the conversion table | A `google` Source (API-configured today) runs Vibey natively; no change for Chat Sources |
| 2 | §3 | Every Gemini key, existing ones included, runs natively; names, routes and images unchanged |
| 3 | §4 | Gemini models search with Google |

Each PR is verified against the real Gemini API on the local regression environment. PR 2 and PR 3 are independent
of each other once PR 1 is merged.

## 6. Known limits

- A Gemini subscription (Antigravity) is served by the engine over Chat only; it keeps Exa/Tavily search.
- Source links in Google's grounding are redirect URLs; the card shows the domain as title.
- Google Search has no positive evidence yet and Gemini answers carry no inline citations (§4).
- Gemini is not a search executor for other hops: the executor speaks Anthropic and Responses only, so Gemini runs
  Google Search on its own hop and names no `executor_models` (a follow-up would add a `google` executor request
  and result).
- The OpenAI-compatible Chat path keeps no dummy for another origin's current-turn call: after PR 2 no official
  Gemini Source uses it, and a Chat relay that serves Gemini is the relay's to translate.
