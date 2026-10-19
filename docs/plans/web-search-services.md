# Web search services

Status: **design**, 2026-10-19. Direction and the six product decisions in §0 are owner-approved (2026-10-19 15:38).
- Scope: Vibey's `web_search` through Model Hub. `x_search`, `image_generate`, Claude Code, Codex and OpenCode are
  unchanged.
- Extends [`vibey-hosted-tools.md`](vibey-hosted-tools.md) (design v2). Every rule there holds unless this document
  says otherwise; section references like "hosted tools §6" point into it.
- Replaces the approach of #2854 (not merged): no per-vendor tool names, no environment switch, no requests from the
  agent process.
- The owner reviewed an interactive draft (a Show Page) on 2026-10-19. Formal frames go to `avibe-docs/design.pen`
  through Pencil (§6).

## 0. Owner decisions

1. **A search service is a Source.** It supplies a tool and no models. It shares the Source lifecycle: add, credential
   custody, health, usage and removal. It can be added from the web search card or from "add Source".
2. **Automatic order: search services first, then model executors.** Services the user added for search come before
   a model Source that also offers search. The model's own search still comes first when the next decision allows
   it.
3. **"Prefer the model's own search" is one switch, on by default.** Off means every hop runs `web_search` through
   the search order, including hops whose model could search natively.
4. **A DeepSeek API-key Source on DeepSeek's official endpoint is a search candidate automatically.** It needs no
   second key, by the same rule that makes an OpenAI, Anthropic or xAI key an executor.
5. **Adding a key checks it only where the check is free.** Otherwise the first real search confirms it. No paid test
   search (hosted tools §0, no active detection).
6. **Page reading (`web_fetch`) is phase 2**, as its own tool card over the same services.

Carried over from the #2854 review: the model sees exactly one `web_search`, and a vendor is an execution choice,
never a tool name.

## 1. Example

The user has an Anthropic API key and a DeepSeek API key, adds Tavily, and changes nothing else.

1. Vibey runs on Claude Opus. Anthropic offers web search natively, so `web_search` is declared natively (hosted
   tools §1, unchanged).
2. The user switches the Session to a DeepSeek model. DeepSeek has no native search on the route Vibey uses, so
   `web_search` is declared as the canonical function `{query}`.
3. The model calls `web_search({"query": "DeepSeek search API pricing"})`. The gateway walks the search order:
   Tavily (search service), DeepSeek (search service through the DeepSeek Source's key), then Anthropic (model
   executor). Tavily answers 200 with five results. The chain ends.
4. The tool card reads "via Tavily · 5 sources · $0.008". Models → Tools says "最近一次由 Tavily 执行 · 正常 · 10-19".
5. Later Tavily answers 401. The gateway marks the Tavily Source `needs_action` (invalid key), moves on to DeepSeek
   in the same call, and the card says "Tavily 密钥无效，已改用 DeepSeek". Later searches skip Tavily until its key is
   replaced.

## 2. Concepts

- **Three ways a web search runs:**
  - **native**: the hop's provider runs it inside the model call (hosted tools §2);
  - **model executor**: another Source's lightweight model runs one native search (hosted tools §6);
  - **search service** (new): one HTTPS request to a search API, through a Source's key.
- **Search service vendors are catalog rows.** Each names its endpoint, auth scheme, request mapping, result mapping,
  per-request limits, list price and an optional free credential check (§5). A vendor is one of:
  - **tool-only** (Tavily, Exa): its Source has `protocol: "search"` and no models;
  - **model vendor with a search API** (DeepSeek): an ordinary model Source whose catalog row also lists a search
    API. The Source qualifies only on the vendor's official endpoint; a relay base URL does not serve the search API.
- **Page content is a vendor capability.** A row says whether the vendor can return page text. A summary or extra
  snippets are not page text, so a vendor without page text has no content setting.
- **The canonical schema stays `{query}`** (hosted tools §2). Result count and page content are service settings,
  never model arguments, as Anthropic's `max_uses` and OpenAI's actions are declaration settings today.

## 3. Model Hub

### 3.1 Tool-only Sources

- `model_hub.sources[]` accepts `protocol: "search"` with `kind: "api_key"`, `supply_channel: "hub"`,
  `billing: "metered"`, `models: []`, no `base_url`, and a vendor whose catalog row is a tool-only search vendor.
  Any other combination is refused.
- A `search` Source never takes part in model supply: it is excluded from route candidates, Source order for
  backends, model discovery, protocol and credential probes that call a model, engine config rendering, and every
  backend's supply. It appears in the Sources list with the label "搜索服务" and "不提供模型".
- Credential replacement, removal, masked display, `verification_pending`, `credential_generation` and Source health
  are the existing ones. Removing a search Source removes it from `executor_order` as removal does for any Source.

### 3.2 Settings

`model_hub.hosted_tools.web_search` gains two optional fields:

```json
"web_search": {
  "enabled": true,
  "prefer_native": true,
  "executor_order": ["src_…", "src_…"],
  "services": {"src_…": {"results": 5, "content": false}}
}
```

| Field | Rule |
| --- | --- |
| `prefer_native` | boolean; absent means `true` (decision 3) |
| `services` | per search-capable Source: `results` (1 to the vendor's maximum, default 5) and `content` (page text, default `false`, only where the vendor returns page text). Absent means defaults. An entry for a Source that no longer exists is ignored. |

- `executor_order` keeps its rule (hosted tools §3.3) and now lists search-capable Sources beside model executors.
- Releases before this feature ignore both fields with a warning: the existing loader rule for unknown fields.
- A settings change applies from the next turn. The turn's first resolution freezes `prefer_native` and each
  candidate's `results` and `content` in the turn trace's `hosted_tools`, beside the tool set and the executor order
  (hosted tools §6). Later hop resolutions and every call of the turn read the frozen values.

### 3.3 Credential custody

- The key stays in Model Hub's own credential store, like every API-key Source. It is not copied to Vault, the
  engine config, the turn environment, Vibey, a log or the model.
- The call is made inside the Model Hub adapter, which is already the custody boundary (`adapter.py`: no credential
  material crosses it). A new adapter operation sends one search request for a `credential_ref` and returns status,
  a bounded header subset and the body; it reads the key from the state store (`read_api_key`, as credential
  retargeting does today) only for the duration of the request.
- Transport rules: HTTPS only, host pinned to the catalog endpoint, redirects refused, environment proxies ignored
  as the engine's own upstream requests ignore them, response body capped at 8 MiB. The credential file read happens
  off the controller loop.
- The engine is not involved: it only forwards model requests, and rendering a tool-only key into engine config
  would advertise a model provider that does not exist.

### 3.4 Hop facts

- The turn's frozen `prefer_native: false` makes `web_search` `native: "unsupported"` and `replay: "unsupported"` on every hop. It
  writes no evidence. Native results already in history then replay in the synthetic text form (hosted tools §5), as
  on any hop that does not declare the tool natively, so no request carries a native result without its
  declaration.
- With `prefer_native: true`, facts are hosted tools §3.4 unchanged.

### 3.5 The Tools read

`GET /api/models/hosted-tools` (hosted tools §3.3), for `web_search`:

- `prefer_native` (saved, `true` when absent);
- `executor_candidates[]` entries gain `kind: "service" | "model"`. A service entry carries `model_id` = the
  catalog's service id (for example `tavily/search`), `price` in the existing unit form with unit `search`, its
  `settings {results, content}`, and the Source's health so the row can say "密钥无效 · 已跳过";
- `service_vendors[]`: the catalog's search vendors, for the add dialog: id, name, list price, free tier, facts,
  the maximum `results`, whether the vendor returns page text and whether that changes the price, and whether a
  Source of the user already provides it.

The PUT body gains `prefer_native` and `services` as per-field writes, the existing pattern.

## 4. The gateway

### 4.1 The chain

Hosted tools §6 holds, with these changes:

- **Candidates** are the model executors of hosted tools §6 plus every search-capable Source: a `search` Source, or a
  model Source whose vendor lists a search API and whose endpoint is official. A search-capable Source in
  `needs_action` is skipped.
- **Automatic order** (no `executor_order`): `search` Sources in creation order, then model Sources with a search API
  in Vibey's Source order, then model executors as today. A saved `executor_order` is used as saved; candidates it
  does not name follow in automatic order. Adding a search-capable Source while an order is saved inserts it into
  the saved order after its last search-capable entry, so it runs before the model executors and the user's other
  entries keep their order.
- **Per-service limits are checked before the call.** A query longer than the service accepts (DeepSeek: 128
  characters) skips that service for this call with no state and no cost.
- **One attempt** sends the catalog's request for the canonical query, the turn's frozen `results` and `content`, and
  waits at most 30 s, inside the chain's single 180 s deadline.

### 4.2 Outcomes

| Outcome | Chain | Source state |
| --- | --- | --- |
| 200 with results | ends with a `completed` record | none |
| 200 with no results | ends with a `completed` record with no sources | none |
| 401, or 403 that the catalog maps to the credential | next candidate | `needs_action`, invalid key (model-hub.md `credential.static_unauthorized`) |
| 402 (balance exhausted) | next candidate | `needs_action`, balance (model-hub.md `credential.account_classified`) |
| a plan or spending limit the catalog names (Tavily 432 plan limit, 433 pay-as-you-go limit) | next candidate | `needs_action`, a new detail: limit reached, remedy "raise the limit or upgrade in the vendor's dashboard" |
| 408, 429, 5xx, timeout, connection failure | next candidate | none |
| 3xx (the redirect is refused, §3.3) | next candidate, `service_unavailable` | none |
| other 4xx (a query the service refuses) | next candidate | none |
| a body the mapping cannot read | next candidate | none |

- **Recovery.** Replacing the key clears an invalid-key state, as for every Source. A `search` Source in the balance
  or limit state gets "重新启用" in its detail: it runs the catalog's free check where one exists and clears the
  state when the check passes; without a free check it clears the state, and the next real search tries the Source
  once, putting it back in `needs_action` if the same failure returns. Nothing retries on its own. A model Source with
  a search API (DeepSeek) recovers through the existing model Source paths.
- **Health writes are fenced** like every other Source attempt (`model-hub.md`, attempt-start settlement
  generation). A service attempt reserves the generation when it starts. A response that settles after the key was
  replaced or the Source was removed writes no health, so a superseded key cannot block a repaired Source.
- A search service writes no hosted-tool refusal evidence: its failures are Source health, not tool support.
- A completed attempt writes positive evidence with `mode: "service"`, keyed by (Source, service id, `web_search`),
  through the same in-memory and hourly-file rule (hosted tools §3.2).
- With no candidate, the `failed` record names both remedies: add a search service, or an API key from a vendor
  that offers search. When candidates exist but every one is skipped for `needs_action`, the record is `failed` with
  `error_code: "search_sources_blocked"`. It names those Sources and their repairs ("更换密钥" or "重新启用", in
  模型 › 工具), never suggesting a new Source.
- When the query's length skipped every candidate that was not tried, and no attempt failed, the record is `failed`
  with `error_code: "search_query_too_long"`. The text names the shortest limit ("搜索词超过 128 个字符，请缩短后重试"),
  so the model can retry with a shorter query; it never suggests adding a Source. When attempts also failed, the last
  failure is the reason, as today.

### 4.3 The record and cost

- The result is the canonical `HostedRecord` (hosted tools §6). `sources[]` follows the service's ranking: `url`,
  `title`, `snippet` and `page_age` from the catalog's mapping, `snippet` taken from the vendor's model-oriented
  field (DeepSeek `summary`).
- **Every persisted field is bounded before the record is written.** `url` is at most 2,048 characters, and a result
  with a longer or non-HTTP(S) URL is dropped. `title` is cut to 300, `snippet` to 1,000 and `page_age` to 64. A
  record keeps at most the configured number of results. A response that leaves no valid result is
  `service_unreadable`.
- With `content` on, each source gains `content`, bounded to 4,000 characters per source and 20,000 per record.
  Vibey renders it in the model-facing text with the same renderer as today.
- `execution` is a `ProxyExecution` naming the Source, `executor_billing: "metered"` and the attempt's `cost`. When
  the response reports its own cost (Exa's `costDollars`), the cost is that figure with `basis: "reported"`.
  Otherwise it is the catalog's list price for the request plus any per-returned-page part for content (Exa:
  $1 / 1k pages of `contents.text`), with `basis: "list"`. A vendor that bills only non-empty results (DeepSeek)
  costs zero for an empty answer. Earlier billable attempts are `failed_attempts[]` as today; a service attempt is
  billable only when it answered 200.
- `execution.attempts[]` (new) lists every earlier attempt of the call in order, billable or not, as
  `{source_id, label, error_code?}`. `label` is the Source's display name when the attempt started, bounded to 64
  characters. The projection shows the Source's current name and falls back to `label` for a Source removed since. A search service always sets `error_code`: one of `service_key_invalid`,
  `service_balance`, `service_limit`, `service_unavailable`, `service_refused` and `service_unreadable`, each with en
  and zh copy. A model executor sets it only when its failure already carries a code (`FAILURE_CODES`); a provider's
  free-text failure leaves it absent, and the note then says "失败" for that Source. The switch note reads this list
  ("Tavily 密钥无效，已改用 DeepSeek"; with two or more, "Tavily、Exa 失败，已改用 DeepSeek"). `failed_attempts[]`
  keeps its cost role.
- `core/hosted_activity` projects `attempts[]` with the Sources' current names into the shared activity payload
  (`HostedActivity.to_payload`). Its localized line renderer writes the note, so the Web card and the IM line read
  one projection, as they do for cost.
- Every attempt is metered on its Source, so the Usage tab shows search requests per service. A service attempt
  reaches no model, so the usage ledger gains a service row: the Source, the catalog's `service_id` in place of a
  model, requests and cost. The label comes from the catalog, not from model inventory (§8).

## 5. Catalog: first batch

The catalog row is the single source for each vendor's wire shape and price. The implementation lane verifies every
field against the vendor's current documentation and records the date and link beside the price.

A vendor enters the catalog only when its terms allow what every search does: keeping the returned titles, URLs,
excerpts and page text in the Session transcript and showing them to the user, with no screening duty Avibe does not
perform. Prices are in USD. A vendor whose price grows with the result count is capped at the count its base price
includes, so the recorded list price stays exact.

| Vendor | Request | Auth | Results | Content | List price (2026-10) | Free check |
| --- | --- | --- | --- | --- | --- | --- |
| DeepSeek | `POST https://api.deepseek.com/v1/tools/search` `{query, count, need_content}` | Bearer | 1–50 | `need_content` | $0.75 / 1k successful requests; empty results and failures not billed | `GET /user/balance` |
| Tavily | `POST https://api.tavily.com/search` | Bearer | 1–20 | `include_raw_content` | $8 / 1k basic searches | `GET https://api.tavily.com/usage`: 200 passes, 401 is an invalid key |
| Exa | `POST https://api.exa.ai/search` | `x-api-key` | 1–10 (the base price includes 10) | `contents.text`, +$1 / 1k pages | $7 / 1k; the response's `costDollars` is used when present | none known |

- DeepSeek facts that shape the UI (DeepSeek's Search & Fetch guide, 2026-10-19): the query is 1–128 characters and
  supports operators (`site:`, `lang:`, `after:`, quoted phrases); requests made through DeepSeek's own server-side
  tools already hit this API and are billed from 2026-11-01. Rate limits are account-level and have changed since
  launch, so the catalog and the UI do not state them; a 429 is a transient outcome (§4.2).
- **Second batch:** Perplexity Search, Parallel, Zhipu Web Search, Alibaba Cloud web search, and self-hosted SearXNG
  (a URL instead of a key; needs the custom-endpoint rule this phase leaves out).
- **Needs other terms or a compliance path first:**
  - Brave Search: its API help prohibits retaining any data received through the API without a separate
    arrangement, and the transcript keeps every result.
  - Bocha: its terms (§§4.2–4.5) require the API customer to screen end users' queries and the results, which this
    design does not do. Its price is in CNY, so it also needs a currency rule. When it returns, its free check is
    `GET /v1/fund/remaining`.
- **Not supported:** the Bing Search APIs (retired 2025-08-11), Google's Custom Search JSON API (closed to new
  customers, ends 2027-01-01) and Google results-page scrapers.

## 6. User interface

The frames are in `avibe-docs/design.pen` (frame IDs recorded by the design PR). The Show Page draft holds the same
states interactively.

### 6.1 Models → Tools → 网页搜索

- The card keeps its header, switch and status line (hosted tools §8.1).
- Below the status line, always visible while the tool is on:
  - **"优先用模型自带的搜索"** with its switch and one line: on, "Claude、GPT、Grok 等自己能搜时直接用，结果和引用由模型
    厂商给出。"; off, "所有模型都按下面的顺序搜索，包括能自带搜索的模型。";
  - **"搜索顺序"**, moved out of Advanced: one numbered list of search services and model executors, each row with
    its glyph, name, a kind pill ("搜索服务" or "模型代搜"), its status pill ("正常 · 10-19" or "密钥无效 · 已跳过"),
    a second line (the executor model, or the service's settings), the price, and up/down buttons. The caption says
    when the order applies and whether it is automatic; "恢复自动" resets it;
  - **"添加搜索服务"**; with no search service yet, an inline note comparing list prices with the add button as the
    primary action;
  - a one-line custody note: "搜索服务的密钥和其他供应商一样由网关保管，不会交给模型或 Agent。".
- A service row opens its detail; a model row is managed under 供应商与路由.
- Advanced keeps only "各供应商状态" and the clears.
- The tab subtitle names search services as a destination: "搜索会把查询发给执行它的厂商或搜索服务，失败时可能改由顺序中的
  下一个重试；……按量付费的供应商和搜索服务每次调用都会计费。"

### 6.2 Add a search service

- A grid of `service_vendors[]`: name, one line, list price, free tier and facts. A vendor the user already has is
  shown as "已添加"; DeepSeek, when a DeepSeek Source qualifies, as "已在搜索顺序中".
- Choosing a tool-only vendor asks for the key. Choosing DeepSeek without a DeepSeek Source asks for a key and creates
  an ordinary DeepSeek Source (models and search), and says so.
- The dialog states where queries go, that the service joins the search order before the model executors (in a
  saved order too, §4.1), and the custody line. Saving
  runs the free check where the catalog has one; otherwise it saves without a call.

### 6.3 Service detail

Status (latest success, or the failure that put it in `needs_action` with time and HTTP status, with "重新启用" for a
balance or limit state, §4.2), key (masked; "更换密钥",
or for DeepSeek "使用供应商「DeepSeek」的 Key", managed under 供应商与路由), settings (results; page content only where
the vendor returns page text, with its effect on price and on what the model reads), this month's requests and list
cost with a link to Usage, and remove.

### 6.4 Sources

"添加供应商" gains "搜索服务（Tavily、Exa…）", which opens the same dialog. Search Sources are rows with the "搜索服务"
pill, "不提供模型", and their web search chip or `needs_action` chip.

### 6.5 Chat and IM

The tool card and the IM line are hosted tools §8.3 with the service as the executor: "通过 Tavily · 5 个来源 · $0.008";
`🔎 网页搜索 "…" · Tavily · 5 个来源 · $0.008`. A switch caused by a failing service shows on both surfaces:
"Tavily 密钥无效，已改用 DeepSeek". English copy follows the same keys; every string goes through `vibe/i18n` and
`ui/src/i18n`.

## 7. Defaults, cost and privacy

- Nothing new turns on by itself except decision 4: a qualifying DeepSeek Source joins the search order, ahead of
  model executors, because it is a search service. The public docs say so.
- A search sends the query to the service that runs it. DeepSeek processes queries in the People's Republic of
  China; the add dialog states each vendor's facts.
- Prices are list prices with date and source. Free tiers and plans are not modeled; the footnote says that actual
  bills may differ.

## 8. Contract changes

One coordinated Model Hub version event under the freeze protocol (`model-hub-contracts/README.md`).

- `source.schema.json` and `source-create.schema.json`: `protocol: "search"` and its combination rule (§3.1).
- `hosted-tools-status.schema.json`: `prefer_native`, `services`, `executor_candidates[].kind` and settings,
  `service_vendors[]` (§3.5); the PUT body.
- `capability-evidence.schema.json`, and `status.latest_ok.mode` and `status.verified[].mode` in
  `hosted-tools-status.schema.json` with the UI's types: `mode: "service"`.
- The Source health detail for a reached plan or spending limit, with its remedy, in `source.schema.json` and the
  classification registry of `model-hub.md`.
- The vendor catalog (`vibe/data/api_key_vendors.json` and its schema): search vendors and DeepSeek's search API row.
- Usage (`usage-summary.schema.json`, `api.md`'s Usage section): a service row keyed by Source and `service_id`,
  counting requests rather than model calls, with reported or list cost (§4.3).
- `HOSTED_ERROR_CODES`: `search_query_too_long` and `search_sources_blocked`, with copy on every surface.
- `api.md`: the read and PUT changes, and the "重新启用" action for a `search` Source in `needs_action` (§4.2), with
  its `api-response.schema.json` branch.
- The turn trace's `hosted_tools`: the frozen `prefer_native` and per-candidate `results` and `content` (§3.2).
- `core/hosted_activity`'s payload: the projected `attempts` with Source names and codes (§4.3).
- C-1 (`agent-core-contracts/message.schema.json`): `HostedSource.content` and the field bounds (§4.3);
  `ProxyExecution.attempts[]` and the six `service_*` codes in `HOSTED_ERROR_CODES`, with copy on every surface; an
  `executor_origin` that names a search service (`provider` = vendor, `model` = service id, and an `api` value for
  search APIs, which the contract lane names).
- V2 config: `model_hub.hosted_tools.web_search` as §3.2.

## 9. Implementation split

| PR | Lane | Work | Depends on |
| --- | --- | --- | --- |
| **S1** Catalog, Sources, settings | foundation | search vendors in the catalog; `protocol: "search"` Sources and their exclusions; `prefer_native` and `services`; the adapter's search call with custody and transport rules; free checks on add and replace; the Tools read and PUT; the version event | — |
| **S2** Chain and records | gateway | candidates, automatic order and insertion into a saved order, limits, per-vendor request and result mapping with field bounds, outcome classes into fenced Source health, positive evidence, record with `attempts[]`, cost and metering, `prefer_native` in hop facts, the no-candidate reason, the `hosted_activity` projection | S1 |
| **S3** Model text | Vibey | `content` in the model-facing text and its bounds; the card's executor label | S2's record |
| **S4** Tools, dialog, Sources | UI | §6 with en and zh copy | S1's read; S2 for live states |
| **S5** Frames | design | §6 frames in `design.pen` through Pencil | — |
| **S6** Public docs | docs | Models → Tools: services, order, defaults, privacy and cost, in English and Chinese | S4 |

## 10. Acceptance properties

1. The model sees one `web_search` with `{query}`, whatever services exist.
2. No search service key reaches Vibey, the turn environment, a model, a log or Vault; every request is HTTPS to the
   catalog host with redirects refused.
3. Nothing calls a service except a model's tool call in a turn the user started, or a free check when a key is
   added or replaced or when the user presses "重新启用".
4. Automatic order is search services, then model executors; a saved order is honored; with `prefer_native` off,
   every hop declares the function.
5. Invalid-key, balance and limit failures put the Source in `needs_action` and skip it until the key is replaced
   or the user re-enables it; transient failures change no state; an empty answer ends the chain.
6. Each call yields one canonical record; the card and the IM line name the service and, from `attempts[]`, say
   which services failed before it.
7. A `search` Source never appears as a model route candidate, in discovery, in a model probe or in engine config.
8. A DeepSeek API-key Source on the official endpoint is a candidate with no extra setup; on a relay it is not.
9. A settings change never alters a running turn: `prefer_native`, `results` and `content` are frozen at the turn's
   first resolution.

## 11. Test plan

Hermetic, against stub vendor servers; no live upstream and no real keys.

| Layer | Cases |
| --- | --- |
| Config | `protocol: "search"` accepted only in its combination; `prefer_native` and `services` round-trip and default; a release-before loader ignores the new `hosted_tools` fields |
| Custody | the adapter call attaches the key, refuses redirects and non-catalog hosts, caps the body, and returns no key material; nothing outside the adapter reads the key |
| Chain | automatic and saved order; query-length skip; each outcome row of §4.2; one deadline; metering, `failed_attempts` and `attempts`; DeepSeek official versus relay; settings changed mid-turn do not apply until the next turn; re-enable with and without a free check |
| Mapping | one recorded response per vendor maps to the canonical record with bounds; empty results; unreadable bodies |
| Hop facts | `prefer_native` off declares the function on a native-capable hop, and a native result in history replays in the synthetic form |
| UI | the card states, add dialog, service detail, Sources rows, copy in both locales |

## 12. Known by design

1. **Rolling back past this feature is not supported once it has been used.** Two things cross the boundary:
   - adding a `search` Source, because a release before this feature rejects `protocol: "search"` and cannot load
     Model Hub's Sources;
   - any search-service call, including one through an auto-enrolled DeepSeek Source, because the persisted result
     carries C-1 values (`executor_origin` for a search API, `HostedSource.content`, `execution.attempts`) that an
     older release's strict reader rejects, so that Session no longer loads there.

   Removing Sources does not repair a transcript. This matches the existing boundary for downgrades after an
   authorized write (`model-hub-upgrade-config-compatibility.md`); release notes say it.
2. **No custom endpoints in phase 1.** Search services use the catalog endpoint only, so relays and self-hosted
   SearXNG wait for a custom-endpoint rule.
3. **Settings are per service, not per call.** The model cannot ask for more results or for page content.
   Vendors without page text have no content setting.
4. **No paid test search.** A key without a free check is confirmed by its first real search.
5. **Result quality differs between services.** The order is the user's control; the gateway does not judge results.
6. **`web_fetch` is phase 2**, and other backends are not covered.
7. **DeepSeek's own server-side tools** (its Anthropic- and Responses-compatible APIs) are native search for DeepSeek
   models and are not part of this design.
