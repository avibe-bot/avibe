# Bounded local storage growth — Show Runtime archives & agent trace events

**Status:** In progress (2026-08-17)
**Owners:** storage workstream
**Related:** avibe#1506 (feature issue and measured numbers)

## Background

One active installation measured (avibe#1506): 50 archives / ~974 MiB in
`runtime/show-runtime/downloads` (49 stale, no open handles) and a
`~/.avibe/state/vibe.sqlite` of ~1.27 GiB dominated by 362k `agent_events`
rows, of which 361,958 were `visibility='trace'` / `event_type='tool_call'`.

Both are internal runtime artifacts with no lifecycle owner. User-visible
data (`messages`, deliveries, sessions/runs, Vault audit) is out of scope and
must never be touched by this work.

## Lane A — Show Runtime archive cache (small, independent)

Code facts that shaped the design:

- The manifest download flow stages into `<sha256>.tmp`, verifies size +
  full sha256, then atomically renames to `downloads/<sha256>.tgz`
  (`_resolve_manifest_archive`). A file with the strict content-addressed
  name is therefore always a completed, verified archive; `.tmp` files and
  unknown names are never candidates.
- `current.json` and each install's `.vibe-show-runtime.json` record
  `archive_sha256`, so the protected set (current + retained rollback
  installs, following the existing `keep_previous` contract) is exact.
- `_packaged_runtime_archive` writes a non-sha name — excluded by the strict
  `<64-hex>.tgz` filter, so the packaged flow's cache is never pruned.

Changes:

- `ShowRuntimeManager.clean(dry_run=...)` now also prunes stale
  content-addressed archives (strict name + regular file via `lstat`,
  symlink/unknown/`.tmp` untouched) and reports
  count/bytes (candidates + removed).
- `_clean_after_managed_install` runs the same archive cleanup after a
  successful managed manifest install, inside the existing
  never-break-prepare guard.
- `vibe runtime clean --dry-run` previews all removals; `vibe doctor` gained
  a read-only archive-cache item (`show_runtime.archive_cache_reclaimable`).

## Lane B — agent_events trace retention (storage-owned)

Design decisions:

- One retention owner: a new `storage/agent_events_retention.py`. Call sites
  (controller daily task, CLI) never write ad hoc deletes.
- Eligibility is a single property predicate: `event_type='tool_call' AND
  visibility='trace' AND created_at < cutoff` (default 30 days, configurable,
  explicit opt-out). Everything else — messages, non-trace events, newer
  traces — is preserved by construction.
- Alembic partial index on `created_at WHERE event_type='tool_call' AND
  visibility='trace'` keeps the age scan bounded.
- Deletes run in small batches in per-batch transactions (WAL allows
  concurrent writers between batches); a persistent `state_meta` marker
  throttles runs to at most once per day and serializes concurrent runners.
- Physical compaction only after a preflight (free disk > db + WAL + margin,
  checkpoint first); otherwise deferred with a reported reason. Never an
  automatic VACUUM that can exhaust a nearly-full volume.
- Trigger: controller background task (daily check via marker) + manual CLI
  with dry-run/status; never in user request hot paths.

## Todos

- [x] Lane A implementation + tests (`tests/test_show_runtime_archive_cleanup.py`)
- [ ] Lane A PR review loop
- [ ] Lane B migration (partial index) — next alembic head after `20260817_0055`
- [ ] Lane B retention service + controller task + CLI + tests
- [ ] Lane B PR review loop
- [ ] avibe-docs: en/zh CLI + operations docs for `vibe runtime clean --dry-run`
      and trace retention
