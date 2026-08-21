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
- Take the copy unconditionally, and never reuse one already in the window. A
  copy is a rollback point only if restoring it loses no committed data, and
  nothing readable from a copy proves that: a migration that commits row
  changes and then fails moves neither the schema nor the revision stamp, and
  an operator who restores a copy and keeps serving writes moves the contents
  under a stamp that never changed. Every rule that recognised a copy as "the
  same rollback point" was a label standing in for the contents, and each one
  could be made to agree while the contents differed.
- Bound the window to the newest two SQLite migration/repair rollback points
  and the newest three legacy JSON migration snapshots, with the copy a call
  has just written protected from that call's own prune. Copies left by a
  machine whose clock ran ahead are dated into the future permanently, so
  ordering alone cannot defend a fresh rollback point.
- The window bounds disk, and that is all it promises beyond holding the
  database as it stands at each call. It cannot also promise to keep the last
  copy taken before a migration started damaging the database: under a
  migration that keeps failing, the attempts after the first copy an
  already-damaged database, and no property measurable from those copies
  distinguishes them from the clean one. That property belongs to not retrying
  a failed migration once per service entry point, not to retention.
- Record in each manifest the revisions read back from the copy, not the ones
  the caller reported: another process can advance the database in between, and
  an operator choosing a rollback point reads the manifest to do it.
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
