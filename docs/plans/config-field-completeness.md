# Config field completeness — dropped `ui.show_agent_activity` investigation

## Symptom

A regression instance's `config.ui.show_agent_activity` was observed flipped
`true → false`, with `config.json` rewritten mid-deploy. Framed as a possible
"settings silently lost on upgrade" data-loss bug.

## What the config writers actually do (verified)

Every writer was audited and exercised hermetically (temp `AVIBE_HOME`):

| Writer | Mechanism | `ui.show_agent_activity` |
| --- | --- | --- |
| `V2Config.save()` | top-level keys hand-listed, but nested `ui` emitted via `self.ui.__dict__` | **preserved** |
| `api.save_config()` (UI save) | deep-merge onto `config_to_payload(load_config())` → `from_payload` → `save` | **preserved** (recursive merge) |
| `api.config_to_payload()` | `ui` via `{**config.ui.__dict__}` | **preserved** |
| `_persist_avault_cli_path` (`vibe runtime prepare`) | `load_config()` → mutate → `save()` | **preserved** |
| boot `_migrate_language_from_settings` | `load()` → set language → `save()` | **preserved** |
| `scripts/incus_regression.py:normalize_runtime_config` | raw `json` round-trip of the whole payload | **preserved** |
| `scripts/prepare_regression.py:_build_config_payload` | **hand-listed `ui` = {setup_host, setup_port, open_browser}** | **DROPPED** |
| `scripts/incus_tenant.py:default_config` | **hand-listed `ui` = {setup_host, setup_port, open_browser}** | **DROPPED** |

Conclusion: **no runtime/upgrade writer drops `show_agent_activity`** — it is
always serialized wholesale via `ui.__dict__` (this is why a plain service
restart and any UI save preserve it). The field is dropped **only** by the
provisioning scripts, which rebuild `config.json` from a hand-listed field
subset. Those run on regression **reset/reseed** (`--reset-mode config|all` or a
missing config) and on **fresh tenant** cloud-init — i.e. when a *new* config is
intentionally created, not on a state-preserving deploy.

On the specific incident deploy the seed step was skipped ("Existing Avibe state
found; skipping regression state seed"), so `_build_config_payload` did not run;
the mid-deploy `config.json` write matches the avault dependency-refresh writer
(`_persist_avault_cli_path`), which is load-modify-save and preserves the field.
The `false` state therefore predates that deploy (an earlier reset/reseed, or a
toggle that did not persist) rather than being flipped by it. Exact attribution
needs the instance's config history, which is out of scope for this change.

## The real bug class (root cause)

Full-config serialization is done by **multiple hand-maintained field lists**
(`V2Config.save`, `api.config_to_payload`, the two provisioning scripts). Any
list that omits a field silently drops it. Two concrete instances were found and
reproduced:

1. **`api.config_to_payload` omitted `agents.avault`.** This payload is the
   deep-merge *base* for every UI save, so **every real-user UI config save
   silently reset `agents.avault.cli_path`** to the dataclass default. (Same
   class as the earlier `config_to_payload` status-bubble omission guarded by
   `test_save_config_preserves_status_bubble_settings_on_partial_save`.)
2. **The provisioning scripts hand-listed `ui`**, dropping `show_agent_activity`,
   `chat_message_font_size`, `trusted_public_origins`, `instance_name` on
   reset/fresh config builds.

## Fix

- `api.config_to_payload`: emit `agents.avault` (mirror `V2Config.save`).
- `scripts/prepare_regression.py` and `scripts/incus_tenant.py`: build the `ui`
  block from `dataclasses.asdict(UiConfig())` + only the bind overrides, so no
  current or future `ui` field can be dropped.
- **Mechanism guard** (`test_full_config_serializers_cover_every_config_field`):
  asserts both `V2Config.save` (on disk) and `config_to_payload` emit every
  top-level `V2Config` field, every `UiConfig` sub-field, and every agent
  backend. A newly-added field hand-listed into only one serializer now fails
  CI — closing the class rather than just the two keys.

## Severity

- **Real-user-facing:** the `config_to_payload`/`agents.avault` omission — every
  UI save reset a persisted avault path. Fixed + guarded.
- **Provisioning-only:** the `ui` subset in the seed/tenant scripts affects
  regression reseeds and fresh-tenant provisioning (a new config is expected
  there); the fix makes those configs field-complete and drift-proof.
- **Not** a runtime/upgrade data-loss path: `vibe runtime prepare`, migrations,
  and the boot path all preserve `show_agent_activity`.

## Evidence layers

- Unit/contract: `tests/test_api_save_config_merge.py` (avault round-trip,
  partial-save preservation for avault + ui fields, field-coverage guard),
  `tests/test_prepare_regression.py` and `tests/test_incus_tenant_scaffold.py`
  (ui field-completeness), existing `tests/test_v2_config_platform_registry.py`
  round-trip.
- Manual/hermetic: reproduced the drops and the fixes against a temp `AVIBE_HOME`.
