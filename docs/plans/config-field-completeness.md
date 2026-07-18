# Config field completeness — dropped `ui.show_agent_activity` investigation

## Symptom

A regression instance's `config.ui.show_agent_activity` was observed flipped
`true → false`, with `config.json` rewritten mid-deploy. Framed as a possible
"settings silently lost on upgrade" data-loss bug.

## What the config writers actually do (verified hermetically, temp `AVIBE_HOME`)

| Writer | Mechanism | `ui.show_agent_activity` |
| --- | --- | --- |
| `V2Config.save()` | top-level keys hand-listed, nested `ui` via `self.ui.__dict__` | **preserved** |
| `api.save_config()` (UI save) | deep-merge onto `config_to_payload(load_config())` → `from_payload` → `save` | **preserved** (recursive merge) |
| `api.config_to_payload()` | `ui` via `{**config.ui.__dict__}` | **preserved** |
| `_persist_avault_cli_path` (`vibe runtime prepare`) | `load_config()` → mutate → `save()` | **preserved** |
| boot `_migrate_language_from_settings` | `load()` → set language → `save()` | **preserved** |
| `scripts/incus_regression.py:normalize_runtime_config` | raw `json` round-trip of the whole payload | **preserved** |
| `scripts/prepare_regression.py:_build_config_payload` | hand-listed `ui` subset; **fresh/reset** rebuild only | default (fresh) |
| `scripts/incus_tenant.py:default_config` | hand-listed `ui` subset; **fresh tenant** cloud-init only | default (fresh) |

Conclusion: **no runtime/upgrade writer drops `show_agent_activity`** — it is
always serialized wholesale via `ui.__dict__` (this is why a plain service
restart and any UI save preserve it). The provisioning scripts emit only the
fields they override and let `V2Config.from_payload` fill the rest with
`UiConfig` defaults; they run only when a *new* config is intentionally created
(regression `--reset-mode config|all` / missing config, or fresh-tenant
cloud-init), so a default `show_agent_activity` there is the correct fresh state,
not lost user data. On a state-preserving regression deploy the seed step is
skipped entirely (`run_prepare_state` early-returns), so those scripts do not run.

On the reported non-reset deploy the seed was skipped; the mid-deploy
`config.json` write matches the avault dependency-refresh writer
(`_persist_avault_cli_path`), which is load-modify-save and **preserves** the
field. The `false` state therefore predates that deploy (an earlier reset/reseed,
or a toggle that did not persist) rather than being flipped by it. Exact
attribution needs the instance's config history — out of scope for this change.

## The real bug (root cause)

Full-config serialization is done by **multiple hand-maintained field lists**
(`V2Config.save`, `api.config_to_payload`). The confirmed, real-user-facing
instance:

**`api.config_to_payload` omitted `agents.avault`.** That payload is the
deep-merge *base* for every UI save (`save_config`), so **every UI config save
silently reset `agents.avault.cli_path`** to the dataclass default. Same class as
the earlier `config_to_payload` status-bubble omission already guarded by
`test_save_config_preserves_status_bubble_settings_on_partial_save`.

## Fix

- `api.config_to_payload`: emit `agents.avault` (mirror `V2Config.save`).
- **Mechanism guard** (`test_full_config_serializers_cover_every_config_field`):
  asserts both `V2Config.save` (on disk) and `config_to_payload` emit every
  top-level `V2Config` field, every `UiConfig` sub-field, and every agent
  backend. A newly-added field hand-listed into only one serializer now fails
  CI — closing the class on the two runtime serializers that operate on an
  existing user config.

### Deliberately not changed

The provisioning scripts (`prepare_regression.py`, `incus_tenant.py`) keep their
minimal ui override. They build *fresh* configs where `from_payload` fills the
rest with `UiConfig` defaults (correct by construction), and `incus_tenant.py` is
by contract stdlib-only so it can run on a fresh Incus host before the package is
installed — importing `config` there would break every subcommand. A reset/reseed
correctly re-establishes defaults; the non-reset path (`_normalize_existing_state`)
already preserves the existing config.

## Severity

- **Real-user-facing:** the `config_to_payload` / `agents.avault` omission — every
  UI save reset a persisted avault path. Fixed + guarded.
- **Not** a runtime/upgrade data-loss path for `show_agent_activity`:
  `vibe runtime prepare`, migrations, and boot all preserve it. This corrects the
  initial "settings lost on upgrade" framing with evidence.

## Evidence layers

- Unit/contract: `tests/test_api_save_config_merge.py` — avault round-trip,
  partial-save preservation for avault + ui fields, and the field-coverage
  mechanism guard; existing `tests/test_v2_config_platform_registry.py` round-trip.
- Manual/hermetic: reproduced the drop and the fix against a temp `AVIBE_HOME`
  (never touched real `~/.avibe`).
