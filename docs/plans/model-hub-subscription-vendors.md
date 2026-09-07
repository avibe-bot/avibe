# Model Hub — subscription vendor expansion: Gemini, Kimi, xAI

Status: owner-directed 2026-09-07 (this conversation). Design authority for
the change. `docs/plans/model-hub.md` OAuth sections and
`docs/plans/model-hub-ui-spec.md` §1.4 receive dated amendments referencing
this file.

## Why

The Model Hub engine (pinned CLIProxyAPI v7.2.105, commit `4a2eb54d`) already
implements OAuth login for more coding-plan subscriptions than Avibe
surfaces. Verified at the pinned commit,
`internal/api/server_management.go`:

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
  api-key preset PR; kimi and xai already exist). Subscription rows carry the
  vendor mark, consistent with the api-key picker.

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

## Acceptance

- 添加订阅 menu shows five vendors in the order above, marks included.
- Each new vendor's 去登录 obtains a flow through the engine endpoint above;
  the generic §1.4 states drive it to a bound hub Source; models supplied by
  that subscription appear as Gateway upstream supply.
- Claude/ChatGPT flows byte-equivalent (existing scenario cases stay green).
- `tests/scenarios/auth_setup/catalog.yaml` gains rows for the three new
  vendors with closed-loop harness cases in
  `tests/scenarios/auth_setup/test_auth_setup_scenarios.py` (repo rule for
  multi-step auth flows); provider-specific parsing stays in focused unit
  tests.
- No live vendor OAuth is exercised in CI; harness cases run against stubbed
  engine management endpoints, matching the existing two vendors' pattern.

## Out of scope

- Engine pin bump; Qwen Code / iFlow (not at this pin's route table).
- Native CLI custody for the new vendors.
- Any change to api-key presets or the protocol proof ladder.
