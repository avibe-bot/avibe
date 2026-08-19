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
  hold a consistent SQLite online backup of it, and bound the rollback window
  in the same call. Creating the rollback point is unconditional, so the bound
  has to be too: gating it on the upgrade finishing leaves the window unbounded
  in the one situation that produces attempt after attempt.
- Take no new copy when the window already holds one stamped with the revisions
  the database is stamped with now. That is the same rollback point. A
  migration failing partway is retried by every service entry point that
  touches the store, and copying the database again on each attempt is what
  makes the window grow -- and what puts the snapshot taken before the damage
  at risk, since no rule that inspects the copies can reliably tell it from the
  ones taken after. A partial upgrade that commits row changes without touching
  the schema or the revision stamp is identical to the clean database in
  everything a backup can measure. Not taking the copy keeps the clean snapshot
  by never producing anything that could displace it.
- Bound the window to the newest two SQLite migration/repair rollback points
  and the newest three legacy JSON migration snapshots, with the copy a call
  has just written protected from that call's own prune. Copies left by a
  machine whose clock ran ahead are dated into the future permanently, so
  ordering alone cannot defend a fresh rollback point.
- Record in each manifest the revisions read back from the copy, not the ones
  the caller reported: another process can advance the database in between, and
  the next attempt uses the manifest to decide whether the rollback point it
  needs already exists.
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
