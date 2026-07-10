# Shared backend model catalog service

**Status:** implemented for review
**Scope:** Claude Code and Codex model discovery for Web, IM, and `vibe agent models`
**Supersedes:** PR #853 implementation approach (requirements and regression cases remain valid)

## Background

Claude Code and Codex model choices are currently assembled in `vibe/api.py`,
while reasoning-effort choices are partially duplicated across Python IM
renderers and TypeScript. Adding a newly released model therefore requires an
Avibe release, and incremental attempts to add remote/live sources exposed four
coupled concerns: source precedence, hidden-model withdrawal, cache freshness,
and request-path latency.

The product requirement is narrower and cleaner:

1. silently refresh a GitHub-owned catalog for Claude Code and Codex;
2. merge it with local and bundled knowledge;
3. expose per-model reasoning efforts to every selection surface;
4. never delay a Web or IM interaction on network or CLI discovery.

## Goals

- One shared catalog contract and merge implementation for Claude and Codex.
- Immediate snapshots from memory/disk/bundled/local sources.
- Background-only remote refresh with strict validation and atomic persistence.
- Source visibility is authoritative: hidden entries are tombstones and cannot
  be reintroduced by lower-priority fallbacks.
- Web, Slack, Discord, Telegram, and Feishu consume the same per-model effort map.
- Existing free-form model entry remains supported for unreleased/custom models.

## Non-goals

- No synchronous `codex debug models` probe. Codex's maintained
  `~/.codex/models_cache.json` is the local runtime source; request paths never
  launch the Codex CLI.
- No OpenCode redesign. Its live provider server remains its source of truth.
- No rejection of custom model or reasoning strings at persistence boundaries.
- No service restart or migration of user configuration.

## Contract

The bundled and remote JSON use schema version 1:

```json
{
  "schema_version": 1,
  "backends": {
    "codex": {
      "models": [
        {
          "id": "gpt-5.6-terra",
          "label": "GPT-5.6-Terra",
          "reasoning_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
          "visibility": "visible",
          "priority": 10
        }
      ]
    }
  }
}
```

Normalized snapshot returned to callers:

```json
{
  "ok": true,
  "backend": "codex",
  "models": ["gpt-5.6-terra"],
  "model_labels": {"gpt-5.6-terra": "GPT-5.6-Terra"},
  "reasoning_options": {
    "gpt-5.6-terra": [
      {"value": "__default__", "label": "(Default)"},
      {"value": "ultra", "label": "Ultra"}
    ]
  },
  "catalog_refresh_pending": false,
  "sources": ["remote", "local", "bundled"]
}
```

`models`, `model_labels`, and `reasoning_options` are the only selection data
consumed by UI/IM adapters. Source details are diagnostic metadata.

## Source and merge rules

### Claude

1. cached remote GitHub catalog;
2. bundled catalog;
3. repository Claude catalog and aliases;
4. explicit model values from `~/.claude/settings.json`.

### Codex

1. hidden tombstones from `~/.codex/models_cache.json`;
2. hidden tombstones from the cached remote GitHub catalog;
3. visible entries from the cached remote GitHub catalog;
4. bundled catalog;
5. remaining visible entries from `~/.codex/models_cache.json`;
6. legacy built-in fallback ids;
7. explicit model and migration ids from `~/.codex/config.toml`.

Hidden decisions from the runtime or remote catalog are tombstones; lower-priority
visible entries cannot resurrect those ids. Remote and bundled entries determine
the presentation order so newly published models stay prominent, while reasoning
efforts from the local Codex cache override matching catalog entries because they
reflect the installed CLI's account-visible capabilities. Lower sources may fill
missing metadata but cannot replace populated values.

## Refresh model

- `snapshot()` never performs network I/O.
- If the persisted remote payload is absent/stale, `snapshot()` starts one
  daemon refresh thread and returns immediately.
- Remote payloads are strictly validated before replacing the cache.
- Success/failure timestamps use separate TTLs; writes are atomic.
- Responses expose `catalog_refresh_pending`. The shared TypeScript loader delivers the
  immediate snapshot, then performs bounded silent re-fetches while pending.

## Integration

- `vibe/api.py`: `claude_models()` and `codex_models()` become thin adapters to
  the service; `agent_model_options()` keeps its existing public envelope.
- `core/modals.py`: routing data carries Codex per-model reasoning options.
- IM adapters: select efforts through one shared Python resolver.
- Web: `fetchBackendModels` handles Claude and Codex identically; effort
  resolution consults per-model options for both.
- Claude dispatch keeps its existing normalization behavior, fed by the same
  bundled/remote effort lookup rather than duplicated model-name heuristics.

## Verification matrix

- remote visible + bundled visible: remote metadata wins, no duplicate;
- remote hidden + local/bundled visible: absent;
- local Codex hidden + remote visible: absent;
- malformed remote payload: previous cache retained;
- first request with stale cache: returns before network refresh completes;
- refreshed snapshot: newly fetched model/effort appears without restart;
- backend/model switch: stale model and effort options cannot leak;
- Slack, Discord, Telegram, Feishu: catalog-only effort such as `ultra` renders;
- existing custom model: remains typeable/preservable.

## Delivery

- Unit: parser, merge, tombstones, cache TTL, local adapters, effort resolver.
- Contract: API snapshot and `agent_model_options()` envelopes.
- Scenario: no catalog scenario exists; platform payload tests cover IM surfaces.
- Residual manual: local Incus cross-platform picker verification is deferred
  until the PR is review-clean and CI-green.
