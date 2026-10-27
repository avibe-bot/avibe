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

- `vibe/data/api_key_vendors.json`'s `gemini` row pins `google` on `https://generativelanguage.googleapis.com`.
- An existing official-endpoint `gemini` Source on `openai_chat` moves to `google`: protocol and base URL change,
  its models are listed again natively, and every hop that names a `models/<id>` model of it names `<id>`. The
  Source keeps its id, credential and routes; its identity changes, so stored opaque payloads stop replaying once
  (the documented safe degradation, `model-hub-consumer.md` item 3).
- Gemini image models stay on the engine's images endpoint: the Source's engine registration adds an internal
  OpenAI-compatible entry for its image models only. The user still sees one Source.
- The Web UI's protocol picker gains `google` (design frame first, `../avibe-docs/design.pen`).

## 4. Google Search as Gemini's native `web_search` (PR 3)

- The overlay's `gemini` provider row declares `web_search` on `google` as `{"googleSearch": {}}` with the per-query
  price; the adapter adds `toolConfig.includeServerSideToolInvocations` when it declares one.
- A response's `toolCall`/`toolResponse` pair becomes one `HostedToolBlock` (`action: search` with the queries;
  the record's sources from `groundingChunks`; the answer's citations from `groundingSupports`), with the parts
  verbatim as its native payload for same-Source replay; any other target gets the synthetic call and result
  (`vibey-hosted-tools.md` §5). The raw `search_suggestions` markup is not kept in the record.
- Evidence (`evidence_rules.py`): a served response with a `GOOGLE_SEARCH_WEB` `toolCall` is positive; an error
  naming the tool before output is the narrow negative rule. Cost: queries × list price, from
  `groundingMetadata.webSearchQueries`.
- Public docs (`avibe-docs`, en and zh): Gemini searches with Google, queries go to Google, and the price.

## 5. Delivery

| PR | Scope | Visible result |
| --- | --- | --- |
| 1 | §2, this plan, the conversion table | A `google` Source (API-configured today) runs Vibey natively; no change for Chat Sources |
| 2 | §3 | Gemini keys run natively; images unchanged |
| 3 | §4 | Gemini models search with Google |

Each PR is verified against the real Gemini API on the local regression environment. PR 2 and PR 3 are independent
of each other once PR 1 is merged.

## 6. Known limits

- A Gemini subscription (Antigravity) is served by the engine over Chat only; it keeps Exa/Tavily search.
- Source links in Google's grounding are redirect URLs; the card shows the domain as title.
- The OpenAI-compatible Chat path keeps no dummy for another origin's current-turn call: after PR 2 no official
  Gemini Source uses it, and a Chat relay that serves Gemini is the relay's to translate.
