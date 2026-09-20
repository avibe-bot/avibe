# Model Hub config migration ownership during mixed-version upgrades

## Problem and scope

Installing a new CLI does not replace an already running service. Avibe
3.1.1rc1's ordinary `V2Config.load()` writes the new
`model_hub.runtime_default_applied` field. A live 3.1.0rc3 reader rejects this
unknown field, recovers the entire Hub section to direct-mode defaults, and
rejects an already-running gateway request with `mode_switch_blocked` / 409.
The persisted sources and modes are still present: this is an unintended
writer during the upgrade overlap, not lost credentials or a Codex-only bug.

## Contract

1. Ordinary config loading parses and projects legacy shapes without
   committing schema migrations. This applies at the shared loader, including
   CLI Agent/Session commands, settings reads, status/diagnostics, and upgrade
   preflight. No per-command exception list or version probe is required.
2. Service startup explicitly persists migrations **after** acquiring the
   existing data-directory service-instance lock and before constructing the
   controller. A candidate that cannot acquire that lock cannot migrate the
   live service's config.
3. Keep the existing explicit `persist_migrations` API, in-memory compatibility
   projection, strict validation, recovery evidence, backup/CAS transaction,
   and bounded stale-snapshot reread. A reread preserves the caller's explicit
   migration-persistence choice.
4. Keep the shipped marker, fresh-install defaults, backend routing modes,
   source custody, and one-time runtime promotion unchanged. A subsequent
   deliberate Stop stays stopped across reads and new-service startups.
5. Explicit settings/config writes remain writes. This change does not promise
   arbitrary old-reader support after a new version deliberately saves a new
   schema, nor roll back a config already migrated by an earlier release.
   It removes incidental migration writes from ordinary observation.

The service lock and migration CAS already express the necessary ownership;
do not add another upgrade coordinator, sidecar marker, schema negotiation,
blanket unknown-field tolerance, or a gateway retry/fallback.

## Validation invariants

- A legacy Hub config survives real CLI/settings/upgrade-reader entry points
  byte-for-byte, without migration backups or recovery. Its old-reader field
  vocabulary remains valid and the invocation-resolution consumer continues
  to resolve its configured source instead of returning mode-switch 409.
- Cover enabled and disabled legacy runtime intent, Unicode paths/data,
  unrelated fields, existing marker/explicit opt-out, and legacy shape
  projection. Tests use temporary HOME/XDG/state and synthetic sources only.
- New-service startup commits the existing upgrade once, with an exact
  restricted-permission backup. A rejected second service does not reach
  migration. Backup failure and concurrent writes retain their existing
  fail-closed behavior.
- Re-run the existing migration, settings, CLI, service-lock, and upgrade
  suites. Separately replay the installed historical reader against a
  fixture touched by the changed reader; no native CLI, credentials,
  production config, live request, or service restart is needed.

## Known-by-design boundaries

- `persist_migrations=False` remains a compatibility projection, not a dump
  of the raw file. Recoverable malformed files can still produce the existing
  backup/warning evidence; no recovery default is silently written over them.
- Existing explicit config mutations can serialize the current schema.
  Backward compatibility after such an authorized write, or a manual
  downgrade after migration, is not added by this read-side fix.
- The generic recovery-to-mode-switch error in already shipped old services
  cannot be changed in place. Preventing the incompatible incidental write
  removes this incident's cause without weakening the gateway guard.
- No deployment, restart, merge, native account acceptance, regression reset,
  or local persistent regression update is part of this implementation.
