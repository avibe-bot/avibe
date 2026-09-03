# OpenCode overlay contract

<!-- authority-consumer: protocol anthropic openai_responses openai_chat -->

How Avibe generates the OpenCode runtime config overlay (`OPENCODE_CONFIG`) in
Gateway mode. Owner-locked identifier rules (spec §4.8) restated as testable
requirements.

## Provider entries

- One provider entry per normalized **provider id** referenced by OpenCode's stored
  menu Route chains: `anthropic`, `openai`, `zhipuai`, … Models whose provider
  cannot be identified use the single `custom` provider id. Provider normalization
  occurs at Add/edit time and the stored result is authoritative.
- No `avibe-` namespace anywhere (owner 07-23). Identifiers read exactly like
  native OpenCode: `anthropic/claude-opus-4-6`, `zhipuai/glm-5.2`,
  `custom/<model-id>`.
- Each generated entry uses the OpenAI-compatible Chat frontend and redirects it to
  the local Gateway (`127.0.0.1:<port>/v1`), injecting the **local gateway token**
  (never upstream credentials). This frontend transport is stable for the provider
  entry even when its models resolve to Sources with different stored protocols. The
  Gateway uses the exact configured hop to reach an upstream whose protocol is one of
  `anthropic | openai_responses | openai_chat`. A custom `base_url`, not a fourth
  protocol, represents a relay or self-hosted upstream.
- Any Hub-held subscription may supply OpenCode through an exact configured hop.
  A `native_cli` Source remains bound to its sanctioned backend and therefore cannot
  materialize as an OpenCode provider.

## Menu projection

- The generated provider entries enumerate exactly the checked menu models with a
  stored nonempty Route chain (plus display names). `featured|full` is a UI view
  state; it cannot add a model to the overlay.
- Custom model entries (`origin: manual`) appear under their Source's normalized provider
  prefix, or `custom/` when the vendor is unidentifiable.
- The invocation selects the exact stored `(source_id, model_id)` hop. Runtime never
  normalizes a provider, matches inventory, or substitutes a model.

## Add-time matching (`matching-v1`)

OpenCode matching occurs once while adding a Source, adding a menu model, or reconciling
a newly available built-in, using the inventory owned by that write. An exact checked identifier wins. Otherwise a bare model id is
accepted only when exactly one checked identifier ends with `/<bare>`; zero matches and
ambiguous matches are left unconfigured. The stored Route carries the concrete upstream
model id and this normalization is never repeated by runtime or refresh.

## Stability invariant (test requirement, L7)

For a fixed set of checked models, the generated identifier strings are
byte-identical across: Gateway⇄Direct mode switches, Source-order edits, Source
cooldown/failover, and engine restarts. Adding/removing a Source without changing the
stored OpenCode Route chains also leaves identifiers byte-identical. A scenario test
asserts this by diffing generated overlays under each perturbation.

## Long-lived `opencode serve`

The overlay content hash is recorded in process metadata at launch. When the
effective overlay changes (menu edit, source vendor set change), Avibe waits
for active work to finish, then restarts the serve process; a
`resolution-event` of kind `channel_switch`/`switch` is NOT emitted for
restarts (they are config events, not supply switches).
