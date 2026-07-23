# OpenCode overlay contract

How Avibe generates the OpenCode runtime config overlay (`OPENCODE_CONFIG`) in
hub mode. Owner-locked identifier rules (spec §4.4) restated as testable
requirements.

## Provider entries

- One provider entry per **standard vendor id** present in any hub-channel
  source's model list: `anthropic`, `openai`, `zhipuai`, … Models whose vendor
  cannot be identified go under a single `custom` provider.
- No `avibe-` namespace anywhere (owner 07-23). Identifiers read exactly like
  native OpenCode: `anthropic/claude-opus-4-6`, `zhipuai/glm-5.2`,
  `custom/<model-id>`.
- Each generated entry redirects transport to the local hub (base URL
  `127.0.0.1:<port>` + per-protocol path) and injects the **local gateway
  token** (never upstream credentials). Protocol/SDK per entry follows the
  vendor's protocol as recorded on the supplying source(s).
- Subscription-backed vendors: under Option 1, subscription sources are
  `native_cli` channel and therefore **never** materialize as OpenCode
  providers. `anthropic/…` in OpenCode is eligible only when a hub-channel
  (api_key) source supplies those models. This enforces S2 scenario (b)
  (Claude sub → non-Claude-Code clients: prohibited) structurally, in
  addition to the code-level `allowed_origins` guard.

## Menu projection

- The generated provider entries enumerate exactly the models `checked` in
  `agent-supply.menu` (plus display names). `featured|full` is a UI view
  state; the overlay always reflects `checked`.
- Custom model entries (manual provenance) appear under their source's vendor
  prefix, or `custom/` when the vendor is unidentifiable.

## Stability invariant (test requirement, L7)

For a fixed set of checked models, the generated identifier strings are
byte-identical across: hub⇄direct mode switches, source add/remove/reorder,
source cooldown/failover, and engine restarts. A scenario test asserts this by
diffing generated overlays under each perturbation.

## Long-lived `opencode serve`

The overlay content hash is recorded in process metadata at launch. When the
effective overlay changes (menu edit, source vendor set change), Avibe waits
for active work to finish, then restarts the serve process; a
`resolution-event` of kind `channel_switch`/`switch` is NOT emitted for
restarts (they are config events, not supply switches).
