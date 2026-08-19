# Local Data Lifecycle

## Background

Ordinary Avibe service operation can produce application logs, raw subprocess
logs, and migration backups. These are Avibe-owned diagnostic or rollback
artifacts, but their previous lifecycle was either unbounded or only manual.
User-owned attachments, Show Pages, and conversation data are outside this
policy.

The Show Runtime install cache is handled by the separate
`fix/show-runtime-install-gc` lane. This change does not touch Show Runtime
downloads, sources, prebuilt assets, or version directories.

## Policy

- Rotate `vibe_remote.log` at 20 MiB and retain five rotated files. Stdout
  logging keeps its existing foreground/background behavior.
- Route each raw service/UI stdout or stderr stream through a dedicated bounded
  log sink. The sink is the file's single writer and compacts it in place after
  10 MiB, retaining the newest 5 MiB. This preserves the inode used by live tail
  readers and avoids racing a subprocess that is actively appending output.
- Before an existing SQLite database advances to another schema revision,
  create a consistent SQLite online backup, and bound the rollback window in
  the same call. Creating the copy is unconditional, so the bound has to be
  too: gating it on the upgrade finishing leaves the window unbounded in the
  one situation that produces attempt after attempt.
- Bound the window to the newest two SQLite migration/repair rollback points
  and the newest three legacy JSON migration snapshots. A rollback point is a
  distinct database state -- the revisions a copy was stamped with plus a
  fingerprint of its schema, both read from the copy itself. Two copies of the
  same state are the same rollback point taken twice, and the newer one holds
  every write the older one holds, so the window keeps the newest copy of each
  state, newest state first, and spends any slot left over on the copies that
  were superseded. On a healthy machine every migration finishes and every copy
  is a state of its own, which is the newest two. Under a migration that keeps
  failing partway, every attempt after the first copies the same half-migrated
  database, so those attempts are one state holding one slot between them and
  cannot crowd out the snapshot taken before the damage.
- Prune only strict Avibe formats: self-identifying managed backup directories,
  historical `sqlite-state-migration-*` directories with valid manifests, and
  the exact legacy `vibe-pre-<revision>[-release-head]-repair-<timestamp>` file
  family. Unknown files, symlinks, partial backups, active database files, WAL,
  SHM, attachments, and Show Pages are never candidates.

## Validation

- Logging handler tests cover rotation limits and stdout preservation.
- Runtime tests cover in-place tail preservation, symlink refusal, continuous
  size bounds, and spawn integration.
- Backup tests cover SQLite consistency, per-kind retention, legacy companion
  cleanup, unknown-file preservation, and active DB/WAL/SHM preservation.
