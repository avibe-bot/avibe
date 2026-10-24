# Web search services

Status: **design**, 2026-10-19. Direction and the six product decisions in §0 are owner-approved (2026-10-19 15:38).
- Scope: Vibey's `web_search` through Model Hub. `x_search`, `image_generate`, Claude Code, Codex and OpenCode are
  unchanged.
- Extends [`vibey-hosted-tools.md`](vibey-hosted-tools.md) (design v2). Every rule there holds unless this document
  says otherwise; section references like "hosted tools §6" point into it. Phase-2 page reading is specified in
  [`web-fetch.md`](web-fetch.md), which reuses these conventions and governs `web_fetch` wherever it is more
  specific.
- Replaces the approach of #2854 (not merged): no per-vendor tool names, no environment switch, no requests from the
  agent process.
- The owner reviewed an interactive draft (a Show Page) on 2026-10-19. Formal frames go to `avibe-docs/design.pen`
  through Pencil (§6).
- The built-in, keyless Exa extension is specified in [`web-search-keyless-exa.md`](web-search-keyless-exa.md).
  It keeps this document's service, record, order and `auto_enroll` rules unless that plan names a keyless-specific
  exception; its UI and public-disclosure details are authoritative for `Exa（免费）`.

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
6. **Page reading (`web_fetch`) is phase 2**, as its own tool card over the same services; see
   [`web-fetch.md`](web-fetch.md).

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
4. The tool card reads "via Tavily · 5 sources · ≈ $0.008". Models → Tools says "最近一次由 Tavily 执行 · 正常 · 10-19".
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
- **Search returns snippets, not pages.** Each result carries the vendor's query-relevant excerpt (Tavily's
  `content`, Exa's highlights, DeepSeek's `summary`), never the page's full text. Reading a page is `web_fetch`'s
  job in phase 2, where the model chooses which page to read (owner decision, 2026-10-20).
- **The canonical schema stays `{query}`** (hosted tools §2). Result count is a service setting,
  never model arguments, as Anthropic's `max_uses` and OpenAI's actions are declaration settings today.

## 3. Model Hub

### 3.1 Tool-only Sources

- `model_hub.sources[]` accepts `protocol: "search"` with `kind: "api_key"`, `supply_channel: "hub"`,
  `billing: "metered"`, `models: []`, no `base_url`, and a vendor whose catalog row is a tool-only search vendor.
  Any other combination is refused when it is written. A saved `search` Source whose vendor a later catalog no
  longer lists still loads: it stays in config, is not a search-capable Source, so it is no candidate, has no skip and no `attempts[]` entry (a configuration
  with nothing else gets the no-provider reason, whose remedy, adding a service, is the right one), and its row reads
  "此服务已不再支持" with remove as its only action, so a release that drops a vendor never fails config loading.
- A `search` Source never takes part in model supply: it is excluded from route candidates, Source order for
  backends, model discovery, protocol and credential probes that call a model, engine config rendering, and every
  backend's supply. It appears in the Sources list with the label "搜索服务" and "不提供模型".
- Credential replacement, removal, masked display, `verification_pending`, `credential_generation` and Source health
  are the existing ones. Removing a search Source removes it from `executor_order` as removal does for any Source.
- **Create and replace branch.** `POST /api/models/sources` and `PUT /api/models/sources/<id>/credential` with
  `protocol: "search"` skip protocol observation and inventory discovery; `save_unverified` and
  `accept_unavailable_inventory` do not apply. The server provisions the key and runs the catalog's free check where
  one exists. Its result is one of the five free-check outcomes (§5):
  - `pass`: the Source is saved verified;
  - `balance`: the key is valid but the account cannot pay for a request. The Source is saved verified and in the
    balance `needs_action` state, so the UI asks for a top-up before any query goes to it;
  - `limit`: the key is valid but a plan or spending limit is already reached. The Source is saved verified and in
    the `spend_limit_reached` state;
  - `invalid`: nothing is saved, and the existing invalid-key create error answers (for a replacement, the old key
    stays);
  - `unknown`: a create saves the Source with `verification_pending`, presented as "已保存" like an unverified
    API-key save, and the first successful search clears it. A replacement treats it as a pre-commit failure under
    `api.md`'s strict replacement rule: the old key stays, the replacement material is revoked, and the normal safe
    error answers ("暂时无法验证新密钥，原密钥未改动，请稍后重试");
  - no free check (Exa): create and replacement both save with `verification_pending`, since nothing could verify
    the key before a search; the first successful search clears it.

  A committed replacement clears every `needs_action` state of the Source (invalid key, balance or limit), since
  each described the old key; a free check's answer then sets the new key's state, and without one the next search
  tries the new key and sets a blocker again only if the account still cannot pay.

  A pending marker is cleared by any search outcome that proves the key authenticated: any 200 (a success, an empty
  answer, or a body the mapping cannot read), or a 402 balance or catalog-named limit answer, which clears it while
  setting its own blocker (§4.2), as a free-check `balance` or `limit` result would.

  The response is the existing Source envelope.
- **DeepSeek from the add dialog** uses the ordinary API-key create with the catalog's free check run before commit,
  inside the same create transaction: `invalid` saves nothing and answers the existing invalid-key create error;
  `balance` saves the Source in its balance state, which its models share; `pass` saves it verified; `unknown`
  falls back to the ordinary unverified save.

### 3.2 Settings

`model_hub.hosted_tools.web_search` gains four optional fields (`prefer_native`, `services`, `service_order` and
`executor_order_kinds`):

```json
"web_search": {
  "enabled": true,
  "prefer_native": true,
  "executor_order": ["src_…", "src_…"],
  "executor_order_kinds": {"src_…": "service", "src_…": "model"},
  "services": {"src_…": {"results": 5, "enabled": true}},
  "service_order": ["src_…", "src_…"]
}
```

| Field | Rule |
| --- | --- |
| `prefer_native` | boolean; absent means `true` (decision 3) |
| `service_order` | the automatic order of tool-only `search` Sources: the server appends a Source's id when it is created and drops it when the Source is removed; the user never edits it directly (a reorder saves `executor_order`). Ids that no longer name a Source are skipped. |
| `executor_order_kinds` | written by the server with every `executor_order` save: each named Source id mapped to `service` or `model`, its role at that save. Absent (a released order) means every entry was saved as `model`; an id the order does not name is ignored. An id the order names but that names no Source at the save (a stale client's removed Source) gets no entry and is skipped at read, as `executor_order` already skips it; an entry with no kind is read as `model`. The client never writes it. |
| `services` | per search-capable Source: `results` (1 to the vendor's maximum, default `min(5, the vendor's maximum)`) and `enabled` (default `true`, except `false` for a model Source whose catalog row is not `auto_enroll` (§4.1); `false` takes the Source out of the search order without removing it, which is how a DeepSeek Source keeps its models but stops searching). Absent means defaults. An entry for a Source that no longer exists is ignored. The catalog limits are enforced on write; on load a saved value the current catalog no longer allows is read as the nearest allowed one (`results` clamped to the maximum) and kept as saved until the next write, so a catalog change never fails loading. |

- `executor_order` keeps its rule (hosted tools §3.3) and now lists search-capable Sources beside model executors.
  Each save also writes `executor_order_kinds`, a map from each named Source id to the role it had when the order
  was saved (`service` or `model`); a released order has no map, so its entries were all saved as `model`. The
  map is what lets a read tell a deliberate placement from a stale one (§4.1).
- A damaged server-written `executor_order_kinds` map warns and loads as absent, without discarding the
  tool switch, service opt-outs or other saved choices. S2 interprets absent roles as `model` as above.
- Releases before these settings ignore unknown fields with a warning. An older release's config write drops
  those fields, including per-Source opt-outs; see §12. rc12 retains `service_order` but not the three S1b fields.
- A settings change applies from the next turn. The turn's first resolution freezes `prefer_native`, the effective
  candidate list (Sources with `services.<id>.enabled: false` kept, last, as `disabled` entries) and each candidate's `results` in the turn
  trace's `hosted_tools`, beside the tool set and the executor order (hosted tools §6). Later hop resolutions and every call of the turn read the frozen values.

### 3.3 Credential custody

- The key stays in Model Hub's own credential store, like every API-key Source. It is not copied to Vault, the
  engine config, the turn environment, Vibey, a log or the model.
- The call is made inside the Model Hub adapter, which is already the custody boundary (`adapter.py`: no credential
  material crosses it). A second new operation runs a catalog free check for a `credential_ref` and returns only
  its outcome (`pass`, `balance`, `limit`, `invalid`, `unknown`), under the same transport rules and a 10 s total deadline
  (5 s to connect); expiry is `unknown`; create, replace, the
  DeepSeek add path and "重新启用" call it. A new adapter operation sends one search request for a `credential_ref` and returns a structured
  outcome: status, a bounded header subset, `response_received_at` (the instant the headers arrived, which the retry
  coordinator anchors `Retry-After` to, as `model-hub-retry-experience.md` requires) and the body, the status and
  receipt time surviving a later body-read failure. Like the invocation contract's `on_admitted`, it takes an `on_sent`
  callback (sync or awaitable) that it calls exactly once just before writing the request, after connect and TLS
  succeed; final admission and trial reservation hold a short controller lock, never during connection setup. A failure or
  cancellation before that call is pre-send ($0); one after it and before any status is billing-unknown; and one
  after a status arrived is settled by that status (§4.3). It reads the key from the state store (`read_api_key`, as credential
  retargeting does today) only for the duration of the request.
- Transport rules: HTTPS only, host pinned to the catalog endpoint, redirects refused, environment proxies ignored
  as the engine's own upstream requests ignore them, response body capped at 8 MiB. The credential file read, and the buffered body's JSON decoding, result
  mapping and bounds, run off the controller loop (`asyncio.to_thread`, as the hosted-response parsers already do).
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
- `services`: saved partial overrides for currently search-capable Sources, not effective defaults. Extant
  retargeted or retired Sources are omitted while incapable, but retain their overrides in config for when
  capability returns; removed Sources' overrides are ignored;
- `executor_candidates[]` entries gain `kind: "service" | "model"`. A service entry carries `model_id` = the
  catalog's service id (for example `tavily/search`), `price` in the existing unit form with unit `search` (Exa
  shows "$7 / 千次"; its highlights are included in the search price), its `settings {results, enabled}`, and the Source's health so the row can say "密钥无效 · 已跳过". A
  search-capable Source with `enabled: false` is listed last, reads "未用于搜索" and opens its detail, where the
  switch turns it back on;
- `service_vendors[]`: the catalog's search vendors, for the add dialog: id, name, list price, free tier, facts,
  the maximum `results`, and whether a
  Source of the user already provides it.

The PUT body gains `prefer_native` and `services` as per-field writes, the existing pattern. `services` is a
patch keyed by Source id: a Source absent from it is untouched; a present Source's object merges by field, so
`{results}` alone leaves `enabled` as saved; `null` for a Source, or for one of its fields, removes
the saved value so the default applies again. Two clients editing different Sources, or different fields, do not
overwrite each other.

## 4. The gateway

### 4.1 The chain

Hosted tools §6 holds, with these changes:

- **Candidates** are the model executors of hosted tools §6 plus every search-capable Source: a `search` Source, or a
  model Source with `kind: "api_key"` whose vendor lists a search API and whose endpoint is official. Such a
  Source joins the search order on its own only when its catalog row is marked `auto_enroll` (DeepSeek alone,
  decision 4); for any other vendor that later gains a search API, `services.<id>.enabled` defaults to `false`,
  so its queries go nowhere new until the user turns search on in the Source's detail (a subscription
  or `native_cli` Source has no API key for the search call, so it is never a search-service candidate). A Source holds one role in a chain:
  a model Source with a search API takes the service role while its search is effectively enabled, and is then
  never also a model executor, so it has one row in the order and is called at most once per call; while its
  search is off it keeps whatever model-executor role it had (so a vendor that gains a search API without
  `auto_enroll` loses nothing), and only when it has none is it listed as a `disabled` entry. A tool-only `search`
  Source with `services.<id>.enabled: false` is likewise a `disabled` entry at the end; it is never requested.
- **Automatic order** (no `executor_order`): `search` Sources in `service_order` (§3.2), then model Sources with a search API
  in Vibey's Source order, then model executors as today. A saved `executor_order` is used as saved, and one
  rule, applied whenever the order is read, places what it does not name: unnamed search-capable Sources go
  immediately before the saved order's first model executor (at the end when it names none), in automatic order
  among themselves, and unnamed model executors follow at the end in automatic order. A named entry whose current role
  differs from its `executor_order_kinds` role (§3.2) is read as unnamed, so the same rule places it in its new
  role. An unnamed or role-changed Source therefore lands before every model executor however it became search-capable (added, an existing
  official DeepSeek Source named as a model executor in a released order, a relay retargeted to the official
  endpoint, or a later catalog that gives its vendor a search API), with no migration or insertion write; an entry
  the user placed in its current role keeps its place, even after a model executor (service-first is the default
  placement, not a constraint on a deliberate order), and the order never names a Source twice.
- **Argument check.** Before any candidate, the gateway rejects a query that is empty or only whitespace with the
  canonical tool's argument error, which the model can correct; no Source is tried or recorded.
- **Skips.** A candidate is classified once, by the first reason that applies in the order `unavailable`,
  `disabled`, `blocked`, `cooling`, `query_too_long`, so `query_too_long` names only a Source a shorter query would
  reach. A candidate can be skipped without a request, for one of five reasons: `blocked` (its Source is in
  `needs_action` or in a persistent `error`, the states admission already suppresses), `cooling` (a service cooldown, §4.2, or, for a model Source with a search API, its existing Source-global
  `cooldown` that admission already treats as non-runnable; the skip carries the later of the unexpired deadlines when both apply, since both must pass, `cooldown_until` or the
  Source's `retry_at`), `query_too_long` (the query exceeds what the service
  accepts: the catalog row's query limit, where the vendor documents one: DeepSeek 128 characters; Tavily and Exa document
  none, so they have no query limit) or `unavailable` (the Source was removed, re-identified or refused by final
  admission after the turn froze its candidates, hosted tools §6) or `disabled` (a search-capable Source with
  `services.<id>.enabled: false`; the frozen list carries such Sources last, so they are reached only after every
  other candidate, and never sent a request). A skip costs nothing, writes no state, and is recorded in `attempts[]` (§4.3).
- **One attempt** sends the catalog's request for the canonical query, the turn's frozen `results`, and
  waits at most 30 s, inside the chain's single 180 s deadline. This attempt bound applies only to services;
  model executors retain hosted tools §6's remaining chain budget.

### 4.2 Outcomes

| Outcome | Chain | Source state |
| --- | --- | --- |
| 200 with results | ends with a `completed` record | none |
| 200 with no results | ends with a `completed` record with no sources | none |
| 401, or 403 that the catalog maps to the credential | next candidate | `needs_action`, invalid key (model-hub.md `credential.static_unauthorized`) |
| 402 (balance exhausted) | next candidate | `needs_action`, balance (model-hub.md `credential.account_classified`) |
| a plan or spending limit the catalog names (Tavily 432 plan limit, 433 pay-as-you-go limit) | next candidate | `needs_action`, the existing `models.source.needs_action.spend_limit_reached` detail; the catalog row adds a vendor remedy line naming both repairs (Tavily: "在 Tavily 后台升级套餐或提高该密钥的用量上限（432），或提高按量上限（433）", since Tavily's 432 is either the key's limit or the plan's), so no new state is needed |
| 408, 429, 5xx, timeout, connection failure | next candidate | none; a service cooldown (below) |
| 3xx (the redirect is refused, §3.3) | next candidate, `service_unavailable` | none; a service cooldown |
| a 4xx the catalog row names as query-specific (for example a malformed query) | next candidate, `service_refused` | none |
| any other 4xx (404, 405, 410 and the like: the endpoint or protocol, which a different query cannot fix) | next candidate, `service_unavailable` | none; a service cooldown |
| a body the mapping cannot read (`service_unreadable`) | next candidate | none; a service cooldown, because a 200 may be billed |

A catalog row may refine a status with an exact vendor tag before applying this table. The first batch uses Exa's
`402 API_KEY_BUDGET_EXCEEDED` and `TEAM_BUDGET_EXCEEDED` as `service_limit`, Exa's `403 PROHIBITED_CONTENT`
and `CONTENT_FILTER_ERROR` as `service_refused`, and Tavily's `422` validation response as `service_refused`;
unrecognized tags keep the status-only fallback (including `403 FEATURE_DISABLED` as unavailable).

- **A model Source with a search API (DeepSeek)** shares its key and balance with its models, so a search 401 or 402
  writes the Source's health exactly as a model call would. DeepSeek documents no 403 for Search (its security-policy
  403 is listed for Fetch), so a Search 403 is an undocumented status: `service_unavailable` with a cooldown, and no
  Source state.
- **Service cooldown.** A transient failure puts that (Source, service id) on an in-memory cooldown that only the
  search chain reads. A model route through the same Source (DeepSeek) is unaffected. A 429's or 503's `Retry-After`
  is read by the Model Hub retry coordinator's advice parser (seconds or HTTP date) and honored in full, never
  shortened;
  without advice, and for a 408, 5xx, timeout, connection failure, refused redirect or unreadable body, the
  cooldown is 30 s, doubling on each consecutive failure up to 300 s. A success resets it, and a restart forgets
  it. It writes no Source health. A cooldown longer than 60 s is visible: the Tools read's candidate carries
  `cooldown_until`, the row says "暂停至 HH:MM（服务要求稍后重试）" (with the date, "MM-DD HH:MM", when the deadline is
  not today), and its detail offers "立即重试", which clears the
  cooldown (advancing its generation) so the next search may try the service.
- **Recovery.** Replacing the key clears an invalid-key state, as for every Source. A `search` Source in the balance
  or limit state gets "重新启用" in its detail. When the row's free check can tell that state apart (DeepSeek's balance,
  Tavily's usage limits), it runs the check: `pass` clears the state; `invalid` moves the Source to the existing
  invalid-key state, whose repair is replacing the key; `balance`, `limit` and `unknown` keep or replace the state
  as they say. The check captures the Source's `credential_generation` when it starts; if a key replacement changed it, or the
  Source was removed, before the answer, the answer changes nothing. One re-enable check runs per Source at a
  time: a press while one is in flight joins it and receives its answer instead of starting another, so a repeated
  press (from a second client, say) cannot supersede a current-key `pass`. A state the row's check cannot tell apart (Exa
  has no free check) is not cleared at once. The persisted `needs_action` state stays, and the press arms an in-memory one-shot trial:
  the Source admits exactly one search request, reserved by the first search that is about to send it, after every skip and final-admission check (a search that skips the Source never reserves it), while every other
  search skips it as `blocked`. A trial that completes clears the persisted state, and so does any other 200 (an unreadable body still proves
  the account answered and was billed; it then gets the service cooldown). A trial that fails is settled by the
  outcome table: a classified failure (401, 402, a limit code) replaces the blocker with its own state, so a
  revoked key asks for a new key. Any other failure, whether transient or unreadable, keeps the current blocker. A
  query-specific `service_refused` is unbilled and says nothing about the account, so it releases the reservation
  and leaves the trial armed: the model's corrected query uses it. Every other outcome ends the reservation, so the user can arm a new trial; a cancelled turn (the user
  stops it while the trial request is in flight) releases the reservation and, if no status had arrived, keeps the
  blocker like a transient failure; Stop itself creates no cooldown or failure class: no status settles only billing,
  a received 200 also authenticates and clears the trial blocker without inferring an unreadable body, and an observed
  failure status settles its own outcome row, including health or cooldown. A restart
  drops an unused trial, so the Source stays blocked and the user can press again. This fails closed. While a
  trial is armed or in flight, "重新启用" is disabled (the read reports `trial_pending`), and a repeated request is
  answered with the current trial without arming another or advancing the generation. Nothing retries on its own. A model Source with
  a search API (DeepSeek) recovers through the existing model Source paths.
- **Every state an attempt writes is fenced.** Source health uses the Source-global attempt-start settlement
  generation (`model-hub.md`). The service cooldown uses its own generation per (Source, service id), which only
  search attempts, key replacement, a base-URL retarget (a DeepSeek relay made official or the reverse), Source
  removal, "重新启用" and "立即重试" (`clear_service_cooldown`) advance; a model call on the same Source does not.
  A response whose generation is no longer current writes nothing. Replacing the key, retargeting the base URL or
  removing the Source also clears its cooldowns and retires any armed or reserved re-enable trial, so a trial authorized for the old key is
  never spent on the new one.
- **"重新启用" advances the Source's settlement generation** without interrupting anything, as an attempt start
  does, and clears the Source's service cooldown (advancing its generation), so a check or trial is never skipped
  as `cooling` behind a deadline an earlier concurrent failure installed. A search that was already in flight when the user pressed it cannot put the Source back in `needs_action`;
  only a search started after the action can.
- A search service writes no hosted-tool refusal evidence: its failures are Source health, not tool support.
- A completed attempt writes positive evidence with `mode: "service"`, keyed by (Source, service id, `web_search`),
  through the same in-memory and hourly-file rule (hosted tools §3.2).
- **The terminal reason of a failed call** follows one rule. With no candidate at all, it is today's no-provider
  reason, naming both remedies (add a search service, or an API key from a vendor that offers search). Otherwise it
  is chosen across every candidate, tried or skipped, by what can be acted on first:
  1. a `query_too_long` skip: `search_query_too_long`. The record's `error` text, which the model reads, names the
     largest limit among the skipped candidates ("query exceeds 128 characters"), so a query that short reaches at
     least one of them. The surfaces' localized copy for the code is fixed, "搜索词太长，请缩短后重试", so the code
     needs no stored parameter. The model can fix it at
     once and reach that candidate. Failing that, a tried candidate's `service_refused` (a query-specific 4xx) is
     the reason, with its own code and the vendor's text in `error`: a different query fixes it too, so it ranks
     ahead of any repair the user must make;
  2. a `blocked` skip, or a `disabled` skip when every candidate is disabled: `search_sources_blocked`; the user
     repairs a Source or turns its search back on. A Source the user turned off never decides the reason while an
     enabled candidate exists, so toggling an unused Source does not change it (it still appears in `attempts[]`);
  3. a tried candidate's failure, whether its request was sent or failed before `on_sent`: the last one, as
     today (a pre-send transport failure gives `service_unavailable`);
  4. a `cooling` or `unavailable` skip: `service_unavailable`; wait, or the Source changed and the next turn uses
     the new configuration.

  Every case keeps the per-candidate list in `attempts[]`, so the surfaces can name each Source and its repair. None
  suggests adding a Source while candidates exist.

### 4.3 The record and cost

- The result is the canonical `HostedRecord` (hosted tools §6). `sources[]` follows the service's ranking: `url`,
  `title`, `snippet` and `page_age` from the catalog's mapping, `snippet` taken from the vendor's model-oriented
  field (DeepSeek `summary`).
- **Every field a service mapping writes is bounded before the record is written.** The bounds on the released
  fields (`url`, `title`, `snippet`, `page_age`) belong to the mapping, not to the C-1 read shape, so released
  `HostedSource` rows from native and model-executor calls still load unchanged. `url` is at most 2,048 characters, and a result
  with a longer or non-HTTP(S) URL is dropped. `title` is cut to 300, `snippet` to 1,000 and `page_age` to 64. A
  record keeps at most the configured number of results. An empty results array is a valid answer: a `completed`
  record with no sources (§4.2). `service_unreadable` is a body without a results array, or a non-empty array in
  which no result survives the bounds.
- `execution` is a `ProxyExecution` naming the Source, `executor_billing: "metered"` and the attempt's `cost`. When
  the response reports its own cost as a finite non-negative number at the catalog's cost path (Exa:
  `costDollars.total`), no larger than the catalog's per-request ceiling (100 times the row's list price for the
  request; the bound the xAI tick parser applies for the same reason), the cost is that figure with
  `basis: "reported"`, plus `estimate: true` when the catalog row says the vendor's figure is an estimate (Exa
  documents `costDollars` as an estimated cost), which renders with "≈" like a list price while still counting
  as a reported cost in Usage. Any other value is ignored and the list price applies.
  Otherwise it is the catalog's list price for the request (Exa's always-requested highlights are included in the
  search price), with `basis: "list"`. When the vendor bills only
  non-empty results (DeepSeek), a 200 whose `results` array is non-empty is billed even if every item fails the
  bounds, so it is recorded at the list price. Only a 200 whose `results` array is missing or unreadable leaves the
  charge unknown: the attempt records no cost (unpriced), and the ledger row adds no list-priced quantity but sets
  `lower_bound: true`. A vendor that bills only non-empty results (DeepSeek)
  costs zero for an empty answer. Earlier billable attempts are `failed_attempts[]` as today, and so is an earlier attempt that may have been
  billed (a read timeout after sending): it is listed with no `cost`, which already makes the chain total a lower
  bound ("至少"). A service attempt is
  billable only when it answered 200. Every service attempt the catalog does not bill records an explicit
  `cost: {usd: 0, basis: "list"}`, so the card shows $0 rather than an unknown cost (a zero is exact: it never makes
  a chain total approximate, so the total's basis is chosen from its non-zero figures only, and an all-zero total is `{usd: 0, basis: "list"}`, rendered "$0" like any exact zero): a non-200 response, DeepSeek's
  empty answer, a failure before the request was sent (connect failure), and an outcome the catalog row's billing
  rule declares unbilled (DeepSeek: timeouts, per its Search & Fetch billing rule, 2026-10-20). The adapter's
  structured outcome carries the HTTP status once one arrived, so a body-read failure, timeout or stop after the
  status keeps it: a received non-200 follows its outcome row (health, cooldown, $0), and a received 200 is billed
  by its vendor's rule (Tavily and Exa: the request at list price; DeepSeek: billing unknown, since it depends on a non-empty `results` array). The catalog row's declared unbilled
  outcomes apply before the fallback below (a DeepSeek timeout before any status arrives is $0; once a 200 status arrived, the received-200 rule above
  applies instead). Only another failure
  after sending and before any status (a read timeout, a lost connection, or a stop, which writes no record and
  reaches only the ledger) may still have been billed without a
  known outcome (a stop before sending costs $0), so it is handled like an unreadable 200 whose billing is
  unknown: no cost, `uncertain_requests` in the ledger, and the lower-bound flag.
- A chain with no search-capable candidate (model executors only) writes the released record shape, without
  `attempts[]`, so a model-only search stays readable by older releases.
- `record.attempts[]` (new, on the `HostedRecord`, so it exists when nothing executed) lists, in chain order,
  every candidate the chain reached, including the one `execution` names. Candidates after the one that completed
  are not reached and not listed. Each entry is `{source_id, label, kind, outcome, error_code?, skipped?, needs_action?, cost?}`.
  `attempts[]` is the record's one complete carrier of per-attempt facts, independent of `execution`, so nothing
  is rebuilt from Source or catalog state that may change later:
  - `kind` is `service` or `model`, the role the candidate had in this chain, written when the record is;
  - `cost` is set on every tried attempt: for a service, its §4.3 cost, an explicit $0 included (a pre-send
    failure, a non-200, an empty DeepSeek answer); for a model executor, the pricing module's value of that attempt
    when it was billable (the figure `execution` or `failed_attempts[]` also carries) and an explicit $0 when it was
    not. It is absent only when a charge may exist but cannot be valued;
  - `outcome` is `completed`, `failed` (the candidate was tried and failed, whether its request was sent or
    failed before `on_sent`, as with DNS, connect or TLS errors; a pre-send failure is `service_unavailable`, costs
    $0 and is not metered) or `skipped` (not tried);
  - `error_code` is set on a failed entry when the failure has a code. A failed search-service request always has
    one of `service_key_invalid`, `service_balance`, `service_limit`, `service_unavailable`, `service_refused`,
    `service_unreadable`, or the keyless-Exa-specific `service_rate_limited`, each with en and zh copy. The
    rate-limited code is a cooldown/failover reason, never credential health. A model executor has one only when
    its failure already carries a code (`FAILURE_CODES`); otherwise the note says "失败" for that Source;
  - `skipped` is `blocked`, `disabled`, `cooling`, `query_too_long` or `unavailable`; a `cooling` entry also
    carries `retry_at`, the deadline captured when the skip was decided, since a service cooldown is in memory
    and a later read cannot rebuild it;
  - a `blocked` entry carries `needs_action`: the Source's existing health detail key at that moment, a
    `needs_action` or `error` detail (for example `models.source.needs_action.credential_revoked`, `oauth_expired`,
    `spend_limit_reached` or `models.source.error.unclassified`). The renderer shows that detail's existing remedy,
    so a model executor or a DeepSeek Source gets its own repair (re-authorize, replace the key, top up, raise the
    limit), and only a tool-only `search` Source in a balance or limit state adds "重新启用". A `disabled` entry's
    remedy is turning "用于网页搜索" back on;
  - a skipped entry's remedy is checked against the Source's current state when the record is projected: a Source
    since repaired, re-enabled or removed shows no remedy (the entry still says it was skipped);
  - `superseded: true` marks a failed entry whose actual health or cooldown write its owning fence dropped (a newer attempt, a key replacement,
    a removal or "重新启用"). The projection also treats an entry as superseded when the Source's current health no
    longer matches it, for example a key replaced after the failure settled but before the call ended. The surfaces
    say "这次请求失败，状态已由之后的操作更新" for it and show no repair, since the Source's current state is not
    the one this failure set;
  - `label` is the Source's display name when the chain reached it, bounded to 64 characters. The projection shows
    the Source's current name and falls back to `label` for a Source removed since.

  The switch note names the `failed` entries and the `blocked` and `disabled` skips before the completed one ("Tavily
  密钥无效，已改用 DeepSeek"; with two or more, "Tavily、Exa 失败，已改用 DeepSeek"). A failed call's surfaces name
  every failed and skipped Source with its remedy, whichever one supplied the terminal code. `failed_attempts[]`
  keeps its cost role.
- `core/hosted_activity` projects `attempts[]`, with the Sources' current names, skips and remedies, into the
  shared activity payload (`HostedActivity.to_payload`). Its localized line renderer writes both the switch note
  and, for `search_sources_blocked`, the per-Source repair ("Tavily 需要更换密钥，Exa 需要在 模型 › 工具 中重新启用").
  The Web card (`HostedToolRow`) and the IM line read that one projection; the fixed copy of a known code never
  replaces it.
- The projection also carries the executor kind, `service` or `model`: `service` exactly when
  `executor_origin.api` is `"search_api"` (C-1's `ServiceExecutorOrigin`, §8). With `attempts[]`, the projection
  reads that list alone: the kind and label of the last tried entry (when no entry was tried, every candidate
  having been skipped, the projection has no executor, label or cost, and the card shows the terminal reason with
  the per-Source list, as the released no-candidate failure shows no executor), and the chain total as the sum of the tried
  entries' `cost`. With no known figure at all (every tried entry unpriced, such as a lone DeepSeek unreadable
  200), the total is absent, as an unpriced chain is today; otherwise it is a lower bound when any tried entry has
  no cost
  (a skipped entry sent nothing and has no cost by design, so it never makes the total incomplete). So a chain whose billable model
  attempt was followed by a service that failed before sending keeps both facts, whether or not `execution` is
  present; `execution` and `failed_attempts[]` keep their released meaning for older readers. Without
  `attempts[]` (a model-only chain) the released projection is unchanged. For a
  service it leaves `model` empty, so the Web card (`HostedToolRow`) and the IM line (`_ran`) render "通过 Tavily"
  without the catalog's service id. Model executors keep "via Anthropic API key · claude-haiku-5-5".
- Every candidate that sent a request is metered on its Source; a skip is never metered. The Usage tab therefore
  shows real search requests per service. A service request reaches no model, so the usage ledger gains a service
  row: the Source, the catalog's `service_id` in place of a model, `requests` (sent), `billable_requests` (those
  the catalog's billing rule charges: DeepSeek only a 200 with results, the others any 200), and the list-priced
  quantities: every billable request is valued at the request price (its count is `billable_requests`, so no
  separate request counter exists), and, in parallel, `reported_usd` and `reported_requests` for the requests the vendor priced itself. Usage values the
  quantities at the current price table, as it values model tokens, so `api_cost_usd` is one list valuation with
  one `price_table_date`. Reported costs are the vendor's own figures, exposed beside it as `service_reported_usd`
  and never added to it. A request whose billing is unknown (DeepSeek's unreadable 200) adds to
  `requests` and to a separate `uncertain_requests`, never to `billable_requests`, so the counts stay exact and
  the uncertainty is visible. Released ledger rows without these fields load as zero. The counters keep their
  relations on write and on read, as the model counters do (a corrupt or hand-edited row is repaired, not
  published): `billable_requests + uncertain_requests ≤ requests`, `reported_requests ≤ billable_requests`,
  and `reported_usd` a finite, non-negative amount no larger than `reported_requests` times $1 (a fixed ceiling,
  above every catalog version's per-request ceiling) and zero without a reported request. A row that breaks any
  relation keeps
  `requests`, has its counts reduced to the bounds, drops the quantities that depended on a reduced count
  (`reported_usd`, `reported_requests`), and is marked `lower_bound`; `lower_bound` is also forced
  true whenever `uncertain_requests` is non-zero.
- Pricing presence is unchanged: `pricing` and every cost field appear exactly when the model price table can be
  read. Service quantities are then valued at the catalog prices shipped with the release, whose verification date
  is the new `pricing.service_price_date`; without a model table the summary carries counts only, for services as
  for models.
- `usage-summary` keeps every model shape unchanged and gives service usage its own closed branch, so a service is
  never dressed as a model and model `requests` never count service requests:
  - `UsageBucketRow` becomes `oneOf`: the released model row (no `kind`), or a service row with `kind: "service"`,
    `source_id`, `service_id`, `requests`, `billable_requests`, `uncertain_requests`,
    `reported_requests`, and the cost fields `reported_usd`, `api_cost_usd` and
    `api_cost_lower_bound`, which, like `service_reported_usd`, are present exactly when `pricing` is (the ledger
    keeps them regardless; only the summary follows the presence rule).
  - A new top-level `services[]` (`ServiceUsage`) has one entry per (Source, `service_id`) with those counters,
    `label` (the Source's; null once the Source is removed, folded into the same "Removed providers" identity as
    model usage), `service_label` (the catalog's; null when a later release no longer has that catalog row, and the UI then
    shows the stored `service_id`) and `last_metered_at`, busiest first. A row whose service the current catalog no
    longer prices is left out of `api_cost_usd`, like an unpriced model's tokens: the row is `priced: false`, `api_cost_lower_bound` is set, requests whose request price is gone are counted in the new
    `excluded_service_requests`, and the reported cost still counts. A tool-only Source
    appears only there, never in `sources[]`, whose `models` stay non-empty; a DeepSeek Source keeps its model usage
    in `sources[]` and its search usage in `services[]`.
  - `totals` and each `days[]` entry keep counting model calls. Their `api_cost_usd` adds the services' list
    valuation, the new `service_reported_usd` holds the reported costs, and `api_cost_lower_bound` is set when any
    service row it includes is a lower bound for either reason: its stored `lower_bound: true` (uncertain requests,
    above) or a quantity the current catalog cannot price.
  - The Usage tab shows a "搜索服务" table from `services[]` (requests, billable requests, cost) below the Source
    table; the total cost line is `api_cost_usd`, with the reported figure shown beside it. The label comes from
  the catalog, not from model inventory (§8).

## 5. Catalog: first batch

The catalog row is the single source for each vendor's wire shape and price. The implementation lane verifies every
field against the vendor's current documentation and records the date and link beside the price.

A row's free check maps the vendor's answer to one of five outcomes: `pass` (the key works and the account can pay),
`balance` (the key works but the account cannot pay), `limit` (the key works but a plan or spending limit is
reached), `invalid` (401, or a 403 the row maps to the credential) and
`unknown` (anything else: transport failures, 5xx, unreadable bodies).

A vendor enters the catalog only when its terms allow what every search does: keeping the returned titles, URLs,
excerpts in the Session transcript and showing them to the user, with no screening duty Avibe does not
perform. Prices are in USD. A vendor whose price grows with the result count is capped at the count its base price
includes, so the recorded list price stays exact.

| Vendor | Request | Auth | Results | Excerpt | List price (2026-10) | Free check |
| --- | --- | --- | --- | --- | --- | --- |
| DeepSeek | `POST https://api.deepseek.com/v1/tools/search` `{query, count}` | Bearer | 1–50 | `summary` | $0.75 / 1k successful requests; empty results, failures and timeouts not billed | `GET /user/balance`: 200 with `is_available: true` is `pass`, with `false` is `balance`; 401 is `invalid` |
| Tavily | `POST https://api.tavily.com/search` | Bearer | 1–20 | `content` (a query-relevant snippet; `include_raw_content` is never sent) | $8 / 1k basic searches | `GET https://api.tavily.com/usage`: a 200 needs at least one numeric usage/limit pair (`key.usage`/`key.limit`, `account.plan_usage`/`account.plan_limit`, `account.paygo_usage`/`account.paygo_limit`), and each recognized pair must be wholly absent or have both members as valid numbers, otherwise the result is `unknown`; a `key.limit` of null is Tavily's unlimited key ([OpenAPI](https://docs.tavily.com/documentation/api-reference/openapi.json), 2026-10-20) and counts as a key pair with headroom. A present key pair at its limit is `limit`. Otherwise, with no account pair (Tavily's schema makes `account` optional) the answer still authenticates the key and is `pass`, payment being confirmed by the first search as for a vendor without a free check; with one, it is `pass` when the plan pair or the pay-as-you-go pair has headroom (pay-as-you-go continues after the plan allowance) and `limit` when none does; 401 is `invalid` |
| Exa | `POST https://api.exa.ai/search` | `x-api-key` | 1–10 (the base price includes 10) | always `contents.highlights` (the `snippet` source, since a base result has no excerpt), included in the search price since Exa 3.0 ([announcement](https://exa.ai/blog/exa-3), 2026-10-19); `contents.text` is never requested | $7 / 1k (Exa Auto); the response's `costDollars` is used when present | none known; status/tag mappings use the [error-codes page](https://exa.ai/docs/admin/error-codes) (2026-10-21) |

- DeepSeek facts that shape the UI (DeepSeek's Search & Fetch guide, 2026-10-19): the query is 1–128 characters and
  supports operators (`site:`, `lang:`, `after:`, quoted phrases). The catalog records the list price only. Any
  free period or billing start date stays with DeepSeek's documentation, which has stated a date for its own
  server-side tools. The UI never claims a call is free or charged by date. Rate limits are account-level and have changed since
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
  下一个重试；……按量付费的供应商和搜索服务按各自的规则计费。"

### 6.2 Add a search service

- A grid of `service_vendors[]`: name, one line, list price, free tier and facts. A vendor the user already has is
  shown as "已添加"; DeepSeek, when an official-endpoint DeepSeek Source qualifies, as "已在搜索顺序中" while its effective
  `services.<id>.enabled` is on, and as "已添加 · 未用于搜索" with a link to that Source's detail (where the switch is)
  while it is off. A user whose
only DeepSeek Source is a relay gets the create flow, which adds an official DeepSeek Source.
- Choosing a tool-only vendor asks for the key. Choosing DeepSeek without a DeepSeek Source asks for a key and creates
  an ordinary DeepSeek Source (models and search), and says so.
- The dialog states where queries go, that the service joins the search order before the model executors (in a
  saved order too, §4.1), and the custody line. Saving
  runs the free check where the catalog has one; otherwise it saves without a call.

### 6.3 Service detail

The search-specific parts of a Source's detail (the "用于网页搜索" switch, results, this month's requests)
appear only once the read lists that Source as a service candidate, the same S2 activation signal as the Tools
rows; before it, a `search` Source's detail shows only its key, health and remove, so nothing presents a service
as active.

Status (latest success, or the Source's current health detail with its existing remedy copy; "重新启用" only for a
tool-only `search` Source in a balance or limit state, §4.2, and the existing Source repair for DeepSeek), key (masked; "更换密钥",
or for DeepSeek "使用供应商「DeepSeek」的 Key", managed under 供应商与路由), settings (results), this month's requests and the
persisted cost (list-priced quantities at the current table, labelled "按 API 标价估算", with the service-reported
figure beside it when there is one) with a link to Usage, and remove. Every search-capable Source's detail has the "用于网页搜索" switch
(`services.<id>.enabled`), so a disabled one can always be turned back on. A model Source with a search API
(DeepSeek) has no remove here, and its models stay.

### 6.4 Sources

"添加供应商" gains "搜索服务（Tavily、Exa…）", which opens the same dialog. Search Sources are rows with the "搜索服务"
pill, "不提供模型", and their web search chip or `needs_action` chip.

### 6.5 Chat and IM

The tool card and the IM line are hosted tools §8.3 with the service as the executor: "通过 Tavily · 5 个来源 · ≈ $0.008";
`🔎 网页搜索 "…" · Tavily · 5 个来源 · ≈ $0.008` (a list-price figure and a reported one with `estimate: true`, such as Exa's, keep "≈"; an exact reported figure
and an exact $0 do not; a total keeps "≈" when any non-zero figure in it does). A switch caused by a failing service shows on both surfaces:
"Tavily 密钥无效，已改用 DeepSeek". English copy follows the same keys; every string goes through `vibe/i18n` and
`ui/src/i18n`.

## 7. Defaults, cost and privacy

- The current automatic order is a fact of this release: the user's own search services (Tavily, keyed Exa and
  official DeepSeek) → the built-in keyless Exa service → model executors. Keyless
  Exa is on by default, needs no key or setup, remains a fallback even when
  another service exists, and each search-service row can be reordered or
  turned off.
  The complete keyless-Exa representation, MCP execution, rate-limit handling, UI and public-disclosure facts are in
  [`web-search-keyless-exa.md`](web-search-keyless-exa.md).
- This design makes no general promise that nothing else will turn on automatically. Public docs state the current
  default and where a query goes as facts, and are updated when a default changes.
- A search sends the query to the service that runs it. DeepSeek processes queries in the People's Republic of
  China; keyless Exa sends the query directly from the Avibe runtime host to Exa's hosted MCP endpoint, where Exa
  can see the connection's IP and the honest Avibe integration identity. The keyless request has no API key. The
  Exa Terms and current upstream processing facts are recorded in the keyless-Exa plan.
- Prices are list prices with date and source. Keyless Exa's price and recorded cost are exactly $0 and its UI label
  is "免费"; keyed Exa's monthly free credits, if documented by Exa, apply only to the keyed REST path. Actual
  bills and provider limits may differ from list-price or anonymous-rate-limit facts.

## 8. Contract changes

One coordinated Model Hub version event under the freeze protocol (`model-hub-contracts/README.md`).

- `source.schema.json` and `source-create.schema.json`: `protocol: "search"` and its combination rule (§3.1).
- The verification invariant in `model-hub-contracts/README.md` and `source.schema.json`'s `verification_pending`
  description: for every search-capable Source (a tool-only `search` Source, which never makes a model call, and a
  model Source with a search API, such as a DeepSeek Source saved unverified), the marker clears on an
  authenticated search outcome with the matching credential (any 200, a 402 or a catalog-named limit, §3.1) or a
  free-check `pass`, `balance` or `limit`; a model Source also keeps the matching-credential model-call rule. The structure guards check it.
- The protocol vocabulary's authorities: `model-hub-contracts/README.md` (the "exactly" list) and
  `adapter-interface.py`, which splits it in two. `MODEL_PROTOCOLS` keeps the four invocable wire protocols that
  observation, probes, discovery, the hop schemas (`hop-resolution`, `served-hop`, `hop-origin`) and
  `provider-overlay.schema.json`'s `hosted_tools.propertyNames` (which today references the Source protocol enum
  and moves to the four-value model vocabulary) use, unchanged.
  `SOURCE_PROTOCOLS` is `MODEL_PROTOCOLS` plus `search`, used only where a Source is stored or created. The adapter's
  search and free-check operations are also added to `adapter-interface.py`. The UI contract mirror splits the
  same way: `ui/src/components/settings/models/types.ts` gains a four-value `ModelProtocol` for recorded and
  terminal origins and for observation and probe payloads, and `SourceProtocol` (`ModelProtocol` plus `search`)
  stays for stored and created Source shapes only. The
  structure guards that check them are updated in the same event.
- `api.md`'s Source create and credential replace sections: the search branch (§3.1), with its outcomes in
  `api-response.schema.json`.
- `hosted-tools-status.schema.json`: `prefer_native`, `services`, `executor_candidates[].kind` and settings,
  `service_vendors[]`, and a candidate's `cooldown_until`
  and `trial_pending` (§4.2), with their UI types; the PUT body.
- `capability-evidence.schema.json`, and `status.latest_ok.mode` and `status.verified[].mode` in
  `hosted-tools-status.schema.json` with the UI's types: `mode: "service"`.
- No new Source health detail: plan and spending limits reuse `models.source.needs_action.spend_limit_reached`.
- `model-hub.md`'s settlement rule (the generation is minted at attempt start "and nowhere else"): "重新启用" also
  advances the Source generation, the rule's one stated exception, implemented with the search admission (S2); key
  replacement needs no exception, since it changes `credential_generation` and so the Source identity an attempt
  settles against; and the service cooldown has its own generation (§4.2).
- The vendor catalog (`vibe/data/api_key_vendors.json` and its schema): search vendors and DeepSeek's search API row.
- Usage (`usage-summary.schema.json`, `api.md`'s Usage section, and the on-disk usage ledger): a service row keyed
  by Source and `service_id`, counting requests rather than model calls, with the additive `billable_requests`,
  `reported_usd`, `reported_requests`, `uncertain_requests` and `lower_bound` fields; the
  summary's `kind: "service"` bucket-row branch, `services[]` (`ServiceUsage`, with `priced`), `service_reported_usd`, `excluded_service_requests` and `pricing.service_price_date` (§4.3); and load fixtures for released rows that lack them (§4.3).
- `HOSTED_ERROR_CODES`: `search_query_too_long` and `search_sources_blocked`, with copy on every surface.
- The recovery actions run in the controller, which owns the trial gate and the cooldowns. The UI process forwards
  them over the Model Hub RPC (`ModelHubRemoteService`) as `reenable_search_source(source_id)` (S1a) and
  `clear_service_cooldown(source_id, service_id)` (S2), the path other hosted-tool writes already take. Endpoints:
  `POST /api/models/sources/<id>/reenable` and `POST /api/models/hosted-tools/web_search/services/<source_id>/retry`
  (the path names the Source, and the controller derives the `service_id` from its catalog row, one search service
  per Source). Both routes join the Model Hub management allow-list in `vibe/authorization.py` (unknown routes
  default to Owner-only) with role-policy tests, so a Member who can edit hosted-tool settings can also use them:
  re-enable in S1a, retry in S2.
- `api.md`: the read and PUT changes, the "立即重试" cooldown clear (§4.2), and the "重新启用" action for a `search` Source in `needs_action` (§4.2), with
  its `api-response.schema.json` branch.
- The turn trace's `hosted_tools`: the frozen `prefer_native`, candidate list and per-candidate `results` (§3.2).
- `core/hosted_activity`'s payload: the projected `attempts` with Source names, codes, skips and remedies, and the
  executor kind (§4.3).
- C-1 (`agent-core-contracts/message.schema.json`): `HostedSource` is unchanged (no page content);
  `HostedRecord.attempts[]` with `source_id`, `label`, `kind`, `outcome`, `error_code`, `skipped`, `retry_at`, `needs_action`, `cost` and
  `superseded`; the `core/hosted_activity` projection's "至少" for a total with an unpriced attempt; `HostedCost.estimate` ("≈" on a reported figure), and a cost of exactly 0 rendered as
  "$0" without "≈" by that projection, `HostedToolRow` and the IM line, since a zero is never an estimate; and
  their reader and writer types, and the six existing `service_*` codes plus keyless Exa's
  `service_rate_limited` in `HOSTED_ERROR_CODES`, with copy on every surface; and a
  service executor origin. The rate-limited code is operationally a service
  cooldown/failover reason, never credential health. `Protocol` and the assistant-message `Origin` stay unchanged (four model protocols); a
  new `ServiceExecutorOrigin` definition `{provider, api, model}` has `api` fixed to the const `"search_api"`,
  `provider` the vendor and `model` the catalog service id, and `ProxyExecution.executor_origin` (today a `$ref` to `Origin`) becomes `oneOf` the
  released `Origin` and `ServiceExecutorOrigin`. C-1 owns the definition; the Python reader and writer types in
  the agent core gain the matching variant, and the gateway writes it.
- C-6 (`agent-core-contracts/model-hub-consumer.md`, the search-call items in §2, and its validation fixtures):
  a search chain's candidate is a search-service Source (called through the adapter search operation) or a model
  Source with an executor model, in the §4.1 order with its skips; a service attempt's credential, balance or catalog-named limit failure (401/403 mapped to the credential, 402,
  432/433 and the like) changes that Source's health (§4.2), while its transient, query-specific, redirect and
  unreadable failures write none (failover and the service cooldown only) and a model executor attempt still never
  does; a service attempt the catalog does
  not bill records an explicit zero cost, an earlier possibly billed one is listed in `failed_attempts[]` with no `cost` (a terminal one stays the attempt
  `execution` names, with no cost, and its `attempts[]` entry has none either), and
  the projection marks a partial total (§4.3); a service response is `completed` when it answered 200
  with a `results` array that is empty or keeps at least one row after the §4.3 bounds, and a non-empty array whose
  every row the bounds reject is `service_unreadable` and fails over; and the terminal reasons `search_query_too_long` and `search_sources_blocked`
  (§4.2). The C-6 text is replaced in the same version event, so there is one gateway specification.
- V2 config: `model_hub.hosted_tools.web_search` as §3.2.

## 9. Implementation split

| PR | Lane | Work | Depends on |
| --- | --- | --- | --- |
| **S1a** Catalog and Sources | foundation | search vendors in the catalog; the Source UI guards for a `search` Source (no model refresh, test or add-model controls; its row says "搜索服务"); `protocol: "search"` Sources and their exclusions (the credential store accepts the protocol, the engine config renderer skips it); the protocol vocabulary split; the create and replace branch with free checks; "重新启用" (`reenable_search_source`, its free check and the armed-trial state); `service_order` maintenance on create and remove; the adapter's search call and free-check operation with custody and transport rules; opening the v17 version event | — |
| **S1b** Settings and Tools read | foundation | settings and the service-capable Tools read shape (`prefer_native`, `services`, `executor_order_kinds`, `service_order`, candidate discriminants and `service_vendors`) used by S2b; activation remained gated until the producer and disclosure | S1a |
| **S2a** Chain readers and usage | readers | C-1 `attempts[]`, service executor origin, estimated cost and service error-code readers/writers; attempts-only hosted-activity projection and Web/IM rendering; service ledger and usage-summary readers plus the service-row write API; `mode: "service"` readers; released-shape fixtures and tests. The producer and activation remain with S2b. | S1a, S1b, S1c, S4 |
| **S2b** Chain producer and activation | gateway | Candidate chain and order placement; adapter search calls and mapping; outcomes into health/cooldowns, trial gate, positive evidence and metering writes; hop facts and turn freeze; retry route; flips `SEARCH_SERVICES_ACTIVE`. | S2a, S6's disclosure |
| ~~S3~~ | — | cancelled 2026-10-20 with page content; the card's executor label ("通过 Tavily") moves to S2 with the projection | — |
| **S1c** Content removal | foundation | removes what S1a and S1b landed for page content: the catalog's content request mapping, `content_max_chars` and `page` price extras; S1b's `services.<id>.content` (a saved key is ignored on load), the read's `settings.content` and the vendor page-text flags, with `hosted-tools-status.schema.json`, `api.md`, the UI types and the v18 entries (v19 if a release has carried v18) | S1b |
| **S4** Tools, dialog, Sources | UI | §6 with en and zh copy, built against the read shape with fixtures; it shows service rows and the add dialog's services only when the read lists them | S1b's read shape as S1c leaves it; S5's frames |
| **S5** Frames | design | §6 frames in `design.pen` through Pencil (done: avibe-docs#73) | — |
| **S6** Public docs | docs | Models → Tools: services, order, defaults, privacy and cost, in English and Chinese. The disclosure part (an official DeepSeek Source joins the search order automatically, which data goes to each service, what it costs, and how to turn it off) publishes no later than the release that ships S2; the UI walkthrough follows S4 | the disclosure lands before or with S2 (S2 depends on it); the walkthrough follows S4 |

The version event follows the v15 precedent in `model-hub-contracts/README.md`: S1a opened v17, S1b/S1c completed v18, and RC tag `gh-v3.2.2rc13` carries v18. S2a and S2b-1 opened v19 for the readers and gated producer; `gh-v3.2.3rc1` and `gh-v3.2.3rc2` carry v19, so S2b-2 opens v20. S2b-2 activates the producer, Tools read and snippet-only search chain together with S6's disclosure; the freeze rule means a shipped version never changes meaning. S1c's compatibility rule for removed page-content fields remains in force.

## 10. Acceptance properties

1. The model sees one `web_search` with `{query}`, whatever services exist.
2. No search service key reaches Vibey, the turn environment, a model, a log or Vault; every request is HTTPS to the
   catalog host with redirects refused.
3. Nothing calls a service except a model's tool call in a turn the user started, or a free check when a key is
   added or replaced or when the user presses "重新启用".
4. Automatic order is search services, then model executors; a saved order is honored; with `prefer_native` off,
   every hop declares the function.
5. Invalid-key, balance and limit failures put the Source in `needs_action` and skip it until the key is replaced
   or a re-enable check or trial succeeds; transient failures change no state; an empty answer ends the chain.
6. Each call that finishes yields one canonical record; a call the user stops writes none (the turn abort rule
   stands, as for every hosted call), and only the usage ledger keeps its sent requests, by the §4.3 rules (a stop
   before any status is uncertain; one after a received status keeps that status's accounting). The card and the IM line name the service and, from `attempts[]`, say
   which services failed or were blocked before it, and how to repair a blocked one.
7. A `search` Source never appears as a model route candidate, in discovery, in a model probe or in engine config.
8. A DeepSeek API-key Source on the official endpoint is a candidate with no extra setup; on a relay it is not.
9. A settings change never alters a running turn: `prefer_native` and `results` are frozen at the turn's
   first resolution.

## 11. Test plan

Hermetic, against stub vendor servers; no live upstream and no real keys.

| Layer | Cases |
| --- | --- |
| Create | the search branch skips observation and discovery; free-check pass saves verified, a DeepSeek balance with `is_available: false` saves verified in the balance state, 401 saves nothing, other failures and no-check vendors save pending and the first success clears it; replacement keeps the old key on 401 and on `unknown`, and a committed replacement clears the old balance or limit blocker |
| Config | `protocol: "search"` accepted only in its combination; `prefer_native`, `services` and `executor_order_kinds` round-trip and default, and a mixed service/model order keeps its placement across a restart; a release-before loader ignores the new `hosted_tools` fields |
| Custody | the adapter call attaches the key, refuses redirects and non-catalog hosts, caps the body, and returns no key material; nothing outside the adapter reads the key |
| Chain | automatic and saved order, and the read-time placement of unnamed search-capable Sources before the first model executor (including one qualified by a catalog upgrade, with no config write) and a named entry whose role changed (a catalog upgrade, a retarget, a released order naming an official DeepSeek Source) re-placed through `executor_order_kinds`; service cooldown from `Retry-After` (a longer deadline is not shortened) and doubling backoff, scoped to (Source, service), with a DeepSeek model route unaffected, and not written by an attempt that started before a key replacement; the terminal reason for every mix of skips (a disabled Source decides it only when every candidate is disabled), with `record.attempts[]` present when nothing executed and a blocked model executor showing its own remedy; `services.<id>.enabled: false` takes a DeepSeek Source out of the order and keeps its models; query-length skip; each outcome row of §4.2; one deadline; metering, `failed_attempts` and `attempts`; DeepSeek official versus relay; settings changed mid-turn do not apply until the next turn; re-enable with and without a free check, a second press joining the in-flight check, only one request admitted while a re-enable trial is pending, and a restart before the trial leaves the Source blocked; a long cooldown is shown with `cooldown_until` and cleared by "立即重试"; a reported cost above the ceiling falls back to the list price; a Tavily `key.limit: null` is unlimited |
| Usage | service rows add known costs to the totals without the lower-bound flag; only sent requests are metered; DeepSeek's empty results, non-200 responses, timeouts before any status and failures before sending cost nothing; after sending, the pre-header and post-header cases differ: before any status, a lost connection, a stop, or a timeout for a vendor that does not declare it unbilled records no cost, adds to `uncertain_requests` and sets the lower bound, while after a received status a non-200 costs $0, a Tavily or Exa 200 is list-priced and a DeepSeek 200 (unreadable, timed out or stopped while reading) records no cost, adds to `uncertain_requests` and sets the lower bound; a default Exa call is listed at the search price alone (highlights included); a tool-only Source appears only in `services[]` and validates against the closed schema; a non-200 attempt records $0, and so does a received non-200 whose body read then failed; a pre-send failure is a `failed` attempt with $0 and no metering; a superseded failure, from any fence or from a repair after it settled, shows no repair; the skip classification order; a service no longer in the catalog stays in `services[]` unpriced, with the lower-bound flag, and its saved Source still loads and is no candidate; `service_order` survives a reload and decides the automatic order; a reported cost survives a restart through the ledger; a price change revalues list-priced quantities under the new table date; a released ledger row without the new fields loads; an impossible counter set is repaired, orphaned reported dollars included; a pre-send failure on the only service projects as that service with $0, and after a billable model attempt the total keeps that attempt's cost; a saved `results` above a lowered maximum loads clamped |
| Mapping | one recorded response per vendor maps to the canonical record with bounds; an empty results array is `completed` and ends the chain; unreadable bodies; a released native `HostedSource` row longer than the new bounds still loads |
| Hop facts | `prefer_native` off declares the function on a native-capable hop, and a native result in history replays in the synthetic form |
| UI | the card states, add dialog, service detail, Sources rows, copy in both locales |

## 12. Known by design

1. **Rolling back past this feature is not supported once it has been used.** Four things cross the boundary:
   - adding a `search` Source, because a release before this feature rejects `protocol: "search"` and cannot load
     Model Hub's Sources;
   - any search chain with a search-capable candidate, including one through an auto-enrolled DeepSeek Source,
     whether or not that candidate was reached, because the persisted result
     carries C-1 values (`executor_origin` for a search API, `record.attempts`) that an
     older release's strict reader rejects, so that Session no longer loads there;
   - any sent service request, which writes a service-shaped usage ledger row even when the call was stopped and
     left no record. An older reader requires `model_id` and drops that row, so after a downgrade the Usage tab
     loses that service usage; model rows remain, but the lost service rows mark the surviving usage history as
     degraded or lower-bound, and hourly history is incomplete until the affected evidence ages out;
   - a completed service attempt's capability evidence (`mode: "service"`), which an older reader skips and its next
     evidence write prunes, so after a downgrade the Tools status loses its latest successful service.

   Removing Sources does not repair a transcript. This matches the existing boundary for downgrades after an
   authorized write (`model-hub-upgrade-config-compatibility.md`); release notes say it.

   Settings alone also have a lossy boundary: rc12 loads S1b's unknown settings with warnings, and its next config
   write drops `prefer_native`, `services` and `executor_order_kinds`. The whole-tool switch and both orders
   survive, but a per-Source DeepSeek opt-out does not. Re-upgrading restores service defaults, so that opt-out
   must be reapplied before service execution is enabled. S2/S6's activation and opt-out guidance must state this.
2. **No custom endpoints in phase 1.** Search services use the catalog endpoint only, so relays and self-hosted
   SearXNG wait for a custom-endpoint rule.
3. **Settings are per service, not per call.** The model cannot ask for more results.
4. **No paid test search.** A key without a free check is confirmed by its first real search.
5. **No page content in phase 1.** Search results carry the vendor's excerpt only; full page text comes from
   `web_fetch` in phase 2, one page at a time and chosen by the model, so a search never sends every result's
   page to the model. The content setting and catalog fields S1a and S1b landed are removed by S1c (§9) before S4 and S2 build on the
   read shape; no release ever activated them.
6. **Result quality differs between services.** The order is the user's control; the gateway does not judge results.
7. **`web_fetch` is phase 2**, and other backends are not covered.
8. **DeepSeek's own server-side tools** (its Anthropic- and Responses-compatible APIs) are native search for DeepSeek
   models and are not part of this design.
