# Model Hub — REST API contract

All endpoints live under `/api/models/`. Envelope: success `{ok: true, ...}`;
failure `{ok: false, error: <machine_code>, detail?: <i18n key or safe text>}`.
Every response includes `contract_version: 1`. Auth follows existing UI-server
session auth; localhost curl is rejected the same way as other `/api/*` routes.

| Method & path | Req / Resp schema | Notes |
| --- | --- | --- |
| GET `/api/models/sources` | → `{sources: Source[]}` | list, ordered by priority |
| POST `/api/models/sources` | `SourceCreate` (kind, vendor, base_url?, key? / oauth flow ref) → `Source` | api_key create validates + discovers models (test-and-add, frame 06r). The pasted key is TRANSIENT: L2 provisions it into the engine-owned store (`provision_credential`) and persists only the returned `credential_ref`; on persist failure it revokes. Secrets never enter config, logs, or any response. |
| PATCH `/api/models/sources/<id>` | partial Source (display_name, base_url) → `Source` | never accepts credential material in plaintext beyond initial create |
| DELETE `/api/models/sources/<id>` | → `{ok}` | refuses while source is the only supplier of a checked/ mapped model unless `force=true` |
| POST `/api/models/sources/<id>/test` | → `{ok, discovered: n}` | re-discovery |
| PUT `/api/models/priority` | `Priority` → `Priority` | authoritative full order; server re-echoes canonical order |
| GET `/api/models/agents` | → `{agents: AgentSupply[]}` | includes `current` per backend |
| PATCH `/api/models/agents/<backend>/mode` | `{mode}` → `AgentSupply` | hub⇄direct switch; never silent (plan §4) |
| PUT `/api/models/agents/<backend>/mappings` | `{mappings}` → `AgentSupply` | fixed-menu backends only |
| PUT `/api/models/agents/opencode/menu` | `{menu}` → `AgentSupply` | open menu config |
| POST `/api/models/custom-models` | `{source_id, model_id, display_name?}` → `Source` | appends manual-provenance model entry (frame 08) |
| DELETE `/api/models/custom-models` | `{source_id, model_id}` → `Source` | |
| GET `/api/models/events?limit=n&before=<id>` | → `{events: ResolutionEvent[]}` | adapter-owned feed (最近切换) |
| POST `/api/models/oauth/start` | `{vendor, channel}` → `OAuthFlow` | runtime-declared presentation |
| GET `/api/models/oauth/status/<flow_id>` | → `OAuthFlow` | 2s polling, server holds flow |
| POST `/api/models/oauth/submit` | `{flow_id, value}` → `OAuthFlow` | value = pasted code or callback URL per `presentation.expects` |
| POST `/api/models/oauth/cancel` | `{flow_id}` → `{ok}` | |
| POST `/api/models/migration/scan` | → `MigrationScan` | read-only |
| POST `/api/models/migration/apply` | `{item_ids: []}` → `{applied: n, sources: Source[]}` | copy-only; originals untouched (tested) |
| GET `/api/models/runtime/status` | → `RuntimeDependency` | engine manifest + health |

Error codes (minimum set): `source_not_found`, `flow_not_found`, `flow_expired`,
`discovery_failed`, `invalid_priority_order` (must be a permutation),
`mapping_target_unavailable`, `mode_switch_blocked`, `engine_down`,
`consent_required` (hub-held subscription paths while the experimental flag is
unset), `migration_item_conflict`.

Serializer completeness: every field in these schemas must round-trip through
`config_to_payload` (or the runtime status assembler) and is covered by the CI
completeness guards (issue #939 pattern) in the same PR that introduces it.
