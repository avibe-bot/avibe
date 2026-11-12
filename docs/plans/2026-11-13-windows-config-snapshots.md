# Windows config snapshot publication

## Contract and scope

Cooperating readers of `config.json` must not obstruct atomic publication on
supported native Windows filesystems. An already open reader keeps the complete
old snapshot; a later reader gets the complete new snapshot. Reads stay lock-free.
Writer transactions retain `CONFIG_LOCK -> file lock` and the existing SQLite
ordering. Migration comparisons, private temporary creation, fsync, cleanup and
final consent checks retain their existing responsibilities.

The observed Desktop R1 acceptance failed in `write_atomic/os.replace` with
WinError 5 while creating a Model Hub source. The subsequent `engine_down`
response did not identify the cause. This establishes failed config publication,
not Defender, a particular reader, a read-only attribute, or an R1 regression.
This independent fix-forward PR repairs the demonstrable Windows protocol gap.

## Implementation

`config.atomic_io` owns cooperating snapshot reads and replacement. Windows
reuses `core.agent_core.tools.windows_files.open_for_reading` (delete sharing)
and `replace` (native POSIX replacement semantics); their package initializers
have no runtime initialization. Neither half alone fixes publication over an
open target. POSIX keeps ordinary file reads and `os.replace`.

The reused Windows rename helper previously sized its destination by Python
code points instead of UTF-16 bytes, truncating paths containing supplementary
Unicode characters. The approved scope addition fixes only payload sizing and
copying: `FileNameLength` excludes the terminator; flags, fallback and the API
are unchanged. The real config-consumer regression uses both plain and
supplementary-Unicode homes and checks the exact destination and held snapshot.

All identified raw consumers of the same config file use the snapshot reader:
V2Config load/recovery/backup/CAS; settings access-policy comparison; import
platform resolution; controller language migration; update-checker reload; CLI
trace retention; and prerelease selection. Text callers retain universal newline
handling, while byte callers retain exact bytes. Immutable backup files and
unrelated native CLI configuration files keep their original readers.

`write_atomic` and V2Config's migration CAS share the same replacement operation;
the latter keeps its final comparison and lock ownership. The original unique
temporary preparation remains intact. There is no retry, guard suppression,
destination unlink, read-only override, DACL widening, or new lock.

Windows modes are not DACLs: this change retains creation/inherited ACL policy,
not a claim that `chmod(0600)` makes Windows files owner-private. The existing
native helper falls back to `os.replace` when POSIX rename semantics are
unsupported; an open reader may still cause failure on that filesystem.
Non-cooperating external readers can also deny publication. Such errors remain
visible. The standalone job wrapper and its API remain unchanged.

## Verification

The primary regression pauses an actual V2Config load or raw prerelease reader
immediately after acquiring its real OS handle, using a process queue/event
handshake. The parent publishes through a real config transaction or migration
CAS while that handle stays open, then checks new reads and releases the held
consumer to verify the old snapshot. Intercepting `io.open` changes scheduling
only and also reaches the pre-fix readers: old code fails on Windows when the
writer tries to publish.

Ordinary-reader and legacy-rename controls demonstrate why both halves are
needed. Native read-only destination checks preserve errors, old bytes and
temporary cleanup. Migration stale comparisons, lock-free loads, config/SQLite
lock ordering, consent rejection after flush and POSIX mode regressions retain
their checks. The atomic concurrent-writer test now collects worker exceptions
and uses cooperating snapshot reads, so a partial run cannot appear successful.

The existing Windows Model Hub job runs the focused config suite on both
`windows-latest` and `windows-11-arm` with its supported x64 CPython 3.12.
The original installed-service acceptance and success assertion are unchanged.
The existing classifier already routes config, caller, test and workflow
changes to both native Windows jobs; no classifier expansion is necessary.

Local CPython 3.12 evidence: the focused atomic/config/migration/CI-contract
suite passed 395 tests, with one Windows-only permission case excluded on
macOS. After adding both pathname variants and final process cleanup, the
snapshot file passed 15 cases, with its two Windows-only permission variants
excluded on macOS. These local runs are not Windows evidence. Native results
belong to the PR's exact-head `windows-model-hub` and `windows-agent` checks,
including the unchanged installed-service acceptance and existing helper/job
regressions.

This lane has no merge, installation, deployment or service-restart authority.
