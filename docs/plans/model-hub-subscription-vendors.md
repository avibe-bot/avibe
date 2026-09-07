# Model Hub — subscription vendor expansion: Gemini, Kimi, xAI

Status: owner-directed 2026-09-07 (this conversation). Design authority for
the change. `docs/plans/model-hub.md` OAuth sections and
`docs/plans/model-hub-ui-spec.md` §1.4 receive dated amendments referencing
this file.

## Why

The Model Hub engine already implements OAuth login for more coding-plan
subscriptions than Avibe surfaces.

**Corrected 2026-09-07.** This section was drafted against CLIProxyAPI
v7.2.105 (`4a2eb54d`), which is not the pin the product ships:
`vibe/model_hub_runtime/cliproxyapi_manifest.json` has named **v7.2.149,
`2a6b87aca083a5bf498ac1f68a1b636c500d7aaa`** since #1862, and does so at both
this change's merge base and current master. Evidence about a commit users do
not run is not evidence about shipped behavior, so every engine citation in
this document — the route table below, the presentation forms, and the serving
protocols in the amendment — was re-read at `2a6b87ac` and is stated at that
commit. The two versions agree on all of it; the correction is to the
provenance, not to a conclusion. A future pin bump re-reads them again.

Verified at `2a6b87ac`, `internal/api/server_management.go:176-180`:

| Engine endpoint | Vendor | Avibe today |
| --- | --- | --- |
| `/anthropic-auth-url` | Anthropic (Claude plans) | shipped |
| `/codex-auth-url` | OpenAI (ChatGPT plans) | shipped |
| `/antigravity-auth-url` | Google (Gemini / Antigravity plans) | this change |
| `/kimi-auth-url` | Moonshot (Kimi Code plans) | this change |
| `/xai-auth-url` | xAI (Grok Build plans) | this change |

Shared machinery (`/get-auth-status`, `DELETE /oauth-session`) is
vendor-generic and already consumed by the §1.4 flow. No engine pin bump.

## Scope ruling

- New subscription vendors: `gemini`, `kimi`, `xai`. Labels: Gemini, Kimi,
  xAI. Owner ruling: the xAI product is always presented as "xAI", never
  "Grok".
- **Hub custody only.** None of the three has a native CLI backend in Avibe,
  so the native/hub channel choice does not exist for them: the §1.4 dialog
  presents them as hub-held Gateway upstreams with no channel selector and no
  takeover semantics. The `native_cli` singleton rule and Claude/ChatGPT
  custody recommendations are untouched.
- Vendor menu (frame 13) order: Claude 订阅, ChatGPT 订阅, Gemini, Kimi,
  xAI.
- The OAuth flow state machine (§1.4 forms A/B/C, polling, paste-back,
  reconciliation, error classes) is reused verbatim. No new flow states, no
  contract_version change. `OAuthFlow` shape unchanged.
- Vendor marks: reuse `vendorGlyph.tsx` marks (gemini mark ships with the
  api-key preset PR; kimi already exists). Subscription rows carry the vendor
  mark, consistent with the api-key picker. **Corrected 2026-09-07:** `xai`
  has no mark in `vendorMarks.ts` and gets the sanctioned monogram fallback;
  authoring one is a design deliverable, not part of this change.

## Engine channel mapping

`_OAUTH_ENDPOINTS` (vibe/model_hub_runtime/adapter.py) gains:

| vendor | endpoint | engine channel |
| --- | --- | --- |
| `gemini` | `/antigravity-auth-url` | antigravity |
| `kimi` | `/kimi-auth-url` | kimi |
| `xai` | `/xai-auth-url` | xai |

The exact tuple semantics follow the two existing rows; the implementation
must read them rather than guess, and must verify each new vendor's
presentation form (auth_url vs paste vs device code) against the engine
handlers at the pinned commit, then map it onto §1.4's existing forms.

**Verified 2026-09-07** in `internal/api/handlers/management/auth_files_provider_oauth.go`
at `2a6b87ac`. Avibe reads the form from the start *response* rather than from
a per-vendor table, so this record is evidence that the mapping holds at this
pin, not a shape the code depends on:

| vendor | handler | response | §1.4 form |
| --- | --- | --- | --- |
| `gemini` | `RequestAntigravityToken` (`:344`) | `{status, url, state}`; no `flow`. `RegisterOAuthSession(state, "antigravity")` (`:362`), saved `Provider: "antigravity"` (`:485`). `is_webui` starts a forwarder to the management `/antigravity/callback` (`:367-378`); the wait deadline is 5 minutes (`:387`), which is what Avibe's 300s default stands in for when the response omits `expires_in` | **C** — paste callback URL |
| `kimi` | `RequestKimiToken` (`:624`) | `state="kmi-<UnixNano>"` (`:630`), `flow:"device"`, `user_code` only when non-empty, `expires_in` only when > 0 (`:710-716`). Ignores `is_webui` | **B** — device code |
| `xai` | `RequestXAIToken` (`:511`) | `state="xai-<UnixNano>"` (`:517`), `flow:"device"`, `user_code` when non-empty, `expires_in` **always** — falling back to `xaiauth.MaxPollDuration` (`:613-620`). Ignores `is_webui` | **B** — device code |

Both provider identifiers in each `_OAUTH_ENDPOINTS` row are the vendor's
engine channel name, confirmed against `RegisterOAuthSession` and the saved
`Provider` field above.

## Amendment 2026-09-07 — how a hub-only subscription binds

**Defect this repairs.** As first drafted, Acceptance asked that the generic
§1.4 states "drive it to a bound hub Source" while Out of scope forbade "any
change to the protocol proof ladder". Read together those forbade the feature
they specified: a Source cannot be persisted without a protocol to reach its
upstream over, and neither of the two routes that supplied one admitted these
vendors, so every completed grant would authorize and then die in
`discovery_failed`. That is the same dead end this effort exists to remove.
The out-of-scope line was written about the **api-key observation ladder**
(the rung 1/2/3 response ladder behind `POST /api/models/sources`), which is a
different owner and stays untouched.

**Ruling.** A hub-only subscription binds with an **engine-declared serving
protocol**: a per-vendor pin naming the local surface the pinned engine serves
that auth kind on. This is product knowledge of exactly the kind the owner
already ratified for `vibe/data/api_key_vendors.json` — a fact about the
engine Avibe ships, read from its source and re-read at each pin bump — and
not an inference from the vendor's public API.

It applies only where no probe can reach: a hub subscription's credential
never leaves the engine, and the upstream behind it is the vendor's CLI plane
rather than an endpoint Avibe may synthesize a request against. Vendors with a
reachable upstream keep proving their protocol by response.

**Per-vendor pins**, read at the shipped pin `2a6b87ac` (v7.2.149). All three
are `openai_chat`, and each is one of the engine's own serving surfaces:

| vendor | engine evidence at `2a6b87ac` | protocol |
| --- | --- | --- |
| `gemini` | `internal/translator/antigravity/openai/chat-completions/init.go:9` registers `translator.Register(OpenAI, Antigravity, …)`, translating an OpenAI-chat request onto the antigravity upstream (`cloudcode-pa.googleapis.com/v1internal:generateContent`) | `openai_chat` |
| `kimi` | `internal/runtime/executor/kimi_executor.go:54` returns `FormatOpenAI` from `RequestToFormat`, `:119` translates to `openai`, `:150` calls the upstream's `/v1/chat/completions` | `openai_chat` |
| `xai` | `internal/runtime/executor/xai_executor_request.go:517` accepts `FormatOpenAI` and folds its output controls into the Responses body the executor sends to `/responses` (`xai_executor_execute.go:45`) | `openai_chat` |

The native upstream differs per vendor — Gemini's own wire format, Moonshot's
OpenAI-compatible endpoint, xAI's Responses API — and for gemini it is not in
Avibe's `SOURCE_PROTOCOLS` vocabulary at all. The pin therefore names the
**served** surface rather than the upstream one, and picking the surface the
gateway's engine client already speaks is what makes all three converge. Each
pin equals what `api_key_vendors.json` pins for the same vendor id: one
vendor, one protocol, whichever channel holds the credential. Keep the two
tables in agreement — a subscription pinned against its api-key sibling would
need `_validate_source_target` taught about a second pin.

**Semantics.** These rungs prove no response shape. Reachability and
authentication follow the engine-managed flow that just completed: the engine
accepted the credential, which is what completing the grant means, so the
observation is reachable and authenticated with the protocol supplied by the
pin, and model discovery then runs through the ordinary engine-held path.

**Invariant, not a list.** Every vendor that can start a flow binds by exactly
one of the two routes — response-backed observation, or an engine-declared
pin. A start row without a binding route is a flow that can begin and not end,
so the tests assert the partition rather than enumerating the members: a
vendor added to the start table alone fails a test instead of shipping a dead
end.

`contract_version` is unchanged. No new flow state, no `OAuthFlow` shape
change, no new API field.

## Acceptance

- 添加订阅 menu shows five vendors in the order above, marks included.
- Each new vendor's 去登录 obtains a flow through the engine endpoint above;
  the generic §1.4 states drive it to a bound hub Source; models supplied by
  that subscription appear as Gateway upstream supply.
- Claude/ChatGPT flows byte-equivalent (existing scenario cases stay green),
  with one recorded exception: **the seeded Source name** (amended 2026-09-07,
  below). Their flows, states, refusals, and persisted shape are untouched;
  only the name a *newly* created Source starts with changes, from `anthropic`
  / `openai` to the catalog's `Anthropic` / `OpenAI`. Existing Sources keep the
  name they hold, and the field stays user-editable.
- `tests/scenarios/auth_setup/catalog.yaml` gains rows for the three new
  vendors with closed-loop harness cases in
  `tests/scenarios/auth_setup/test_auth_setup_scenarios.py` (repo rule for
  multi-step auth flows); provider-specific parsing stays in focused unit
  tests.
- No live vendor OAuth is exercised in CI; harness cases run against stubbed
  engine management endpoints, matching the existing two vendors' pattern.

## Amendment 2026-09-07 — a Source is seeded with a name, not a routing key

A completed grant seeded the new Source's `display_name` from the vendor id, so
these subscriptions would have shipped as `gemini`, `kimi`, and `xai` — the last
of which contradicts the label ruling above in the very place the user reads it.

The fix is a deletion, not an addition: the api-key create path in the same file
already seeded from the shipped catalog's `label`, so the two sibling paths
disagreed and the subscription one was the outlier. Both now call one
`seeded_source_name(vendor)`, which is the catalog label where the catalog lists
the vendor and the id where it does not.

This reaches the two shipped vendors as well, and deliberately so: seeding only
the new three would leave `Gemini` beside `anthropic` in one list, and an
Anthropic api-key Source already reads `Anthropic` beside a Claude subscription
reading `anthropic`. Closing the whole class is smaller than three special
cases. `codex` has no catalog row and keeps its id, so the ChatGPT flow is
unchanged either way. Nothing renames an existing Source, and the field remains
user-owned — this is the seed, not a display rule, so no i18n row is involved
(a brand name is not localized copy).

Guarded as an invariant over the start table rather than per vendor: every
`_OAUTH_ENDPOINTS` vendor the catalog names must seed with that name, so a
vendor admitted later cannot ship named by its id.

## Out of scope

- Engine pin bump; Qwen Code / iFlow (not at this pin's route table).
- Native CLI custody for the new vendors.
- Any change to api-key presets or the api-key observation ladder (rescoped
  2026-09-07; see the amendment above — OAuth hub binding is a separate owner
  and is in scope).
