# Issue 1990: macOS Memory identity and recovery

Historical investigation, 2026-09-15. For implementation verification, see
[the layered acceptance report](implementation-acceptance.md). The probe and
replay scripts below are archived evidence against the stated investigated
source, not current-tree regression tests.

Investigated on 2026-09-15. Scope: diagnosis and repair design, without changing
product code or operating the installed Memory service.

## Conclusion

Avibe uses psutil's adjusted, display-oriented process creation time as a macOS
process identity. psutil 7.2.2 deliberately uses a different, unadjusted value
for its own macOS process identity. A controlled probe reproduced a one-second
excursion in the former while the same child's native birth time and psutil
identity remained unchanged. Separately, a lifecycle replay reproduced the
failure to resume after identity becomes verifiable again.

This establishes a concrete mechanism and two implementation defects. It does
not establish that a particular NTP event, sleep/wake, kernel bug, or clock
change occurred in the reported production incident.

The owner selected standard child lifecycle management with minimal checks at
termination and orphan-recovery boundaries. The resulting
[repair design](../../plans/memory-macos-identity-recovery-1990.md) replaces the
earlier native identity and reconfirmation proposals. See the
[industry comparison](industry-supervision.md) for the rationale and alternatives.
The independent [PM review](pm-design-review.md) returned PASS WITH CHANGES
with no blockers. Its six requested clarifications are incorporated in the
repair design; no product implementation is included in these artifacts.

An additional [public-interface probe](public_process_probe.py) verified public
psutil equality, is_running and terminate/wait against a private child while
the display time shifted one second. [Result](public-process-result.json): the
same child remained identified, terminated with SIGTERM and was reaped. No
private psutil identity getter is needed in the proposed product design.

## Evidence provenance

- Incident: [issue #1990](https://github.com/avibe-bot/avibe/issues/1990), reporting
  Avibe 3.0.15rc9, Memory runtime 1.2.3, macOS arm64, psutil 7.2.2.
- Local source read and replayed: `199cccf958349881209c00b20124b82726f3ce0c`.
- Incident source was separately fetched read-only at
  `7f3070863440bcccf0bffb2694f4d6c9d3abd098`. Its `supervisor.py` is identical;
  the only differences in `process.py` and `runtime.py` concern profile
  enablement, not the identity, monitoring, stop, or Wake methods replayed here.
- Native probe host: Darwin arm64; project `.venv` psutil 7.2.2.
- psutil `release-7.2.2` is an annotated tag. Tag object
  `714a292bd14cf0c28b9454d34f5afc26dbb9b0dc` resolves to source commit
  `9eea97dd6f1d16ea33f5144c8925f1ce7a0688e1`; the tag object is not a source commit.

## Library mechanism

The pinned psutil sources show:

1. The native macOS getter converts `kinfo_proc.kp_proc.p_starttime` to seconds.
   [Darwin C getter](https://github.com/giampaolo/psutil/blob/9eea97dd6f1d16ea33f5144c8925f1ce7a0688e1/psutil/arch/osx/proc.c#L64-L102).
2. Public `Process.create_time()` applies a correction based on the difference
   between boot time observed when psutil was imported and boot time observed
   now. An absolute difference below one second is ignored; either sign of a
   one-second difference adds one second to the returned creation time.
   [Darwin adjustment](https://github.com/giampaolo/psutil/blob/9eea97dd6f1d16ea33f5144c8925f1ce7a0688e1/psutil/_psosx.py#L241-L265).
3. The boot-time getter returns only `kern.boottime.tv_sec`, discarding the
   microsecond component. This explains why whole-second changes are possible
   in the adjustment input; it does not identify the incident's triggering event.
   [BSD boot-time getter](https://github.com/giampaolo/psutil/blob/9eea97dd6f1d16ea33f5144c8925f1ce7a0688e1/psutil/arch/bsd/sys.c#L13-L24).
4. psutil's `_get_ident()` explicitly calls its platform getter with
   `monotonic=True` on macOS. This bypasses the adjustment. The public
   `Process.create_time()` method does not expose that argument.
   [Identity implementation](https://github.com/giampaolo/psutil/blob/9eea97dd6f1d16ea33f5144c8925f1ce7a0688e1/psutil/__init__.py#L334-L365),
   [platform getter](https://github.com/giampaolo/psutil/blob/9eea97dd6f1d16ea33f5144c8925f1ce7a0688e1/psutil/_psosx.py#L427-L432).

Here “unadjusted” means the native birth timeval, not a claim that the exported
number is elapsed seconds since boot. XNU's sysctl output copies `p_start` into
the exposed process start fields. [XNU source](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/kern_sysctl.c#L1127-L1131).

## Native probe

Run on macOS:

```sh
.venv/bin/python -B docs/investigations/issue-1990/native_probe.py
```

The probe creates one temporary, stdin-controlled child with an isolated
environment. It patches only the parent probe's psutil boot-time reader; it
does not change the machine's clock or inspect production processes. It reads
the child's birth timeval through public `proc_pidinfo(PROC_PIDTBSDINFO)` and
compares it with psutil's unadjusted getter and public process equality. The
private getter is used for research comparison only.

[Recorded result](native-result.json):

| Injected boot read delta | Public creation-time delta | Native birth-time delta | psutil identity equal |
| --- | --- | --- | --- |
| 0 | 0 | 0 | yes |
| +1 second | +1 second | 0 | yes |
| 0 | 0 | 0 | yes |
| -1 second | +1 second | 0 | yes |
| 0 | 0 | 0 | yes |

The native timeval also remained unchanged across the child's `exec`. After
explicit child completion and reaping, `proc_pidinfo` returned zero bytes and
`ESRCH`. The installed SDK's public `proc_bsdinfo` layout and the ctypes probe
both have size 136 bytes on this host. This is one-host ABI evidence, not a
cross-version or Intel certification. No real PID-reuse stress test was run.

## Recovery replay

```sh
.venv/bin/python -B docs/investigations/issue-1990/replay.py
.venv/bin/python -B docs/investigations/issue-1990/replay.py --expect-recovery
```

The first command asserts and prints the observed current behavior. The second
additionally asserts the desired automatic recovery and intentionally exits 1
on the investigated source:

```text
AssertionError: BUG: identity returned, but processing remains paused
```

The replay exercises production `_monitor_child`, ownership matching, tree
termination, supervisor retry scheduling, and `_wake_locked` stop-failure
handling. OS signaling, waiting, store and provider boundaries are substitutes;
no native Memory process is created. Home and configuration roots are temporary.

[Recorded result](replay-result.json): three automatic attempts fail with
`memory_wake_failed`, no signals are issued while identity mismatches, the
child remains present, and claims remain paused with no recovery task after
the stamp returns. An explicit subsequent stop succeeds in the replay.

The replay intentionally holds the discrepancy through the retry window and
then removes it. The incident does not establish the exact duration of its
discrepancy. Healthy reads are an incident observation, not evidence produced
by this replay. The replay is an investigation tool; the implementation must
add regression coverage at the real runtime/provider integration seam.

## Existing regression baseline

Three focused existing tests passed in 1.24 seconds:

- `test_memory_supervisor.py::test_retained_child_exhausts_recovery_without_dual_ownership`
- `test_memory_wake.py::test_wake_reuses_existing_root_and_proves_native_readiness`
- `test_memory_wake.py::test_wake_never_routes_needs_repair_into_deletion`

They establish the existing bounded-retry, root-preservation and non-destructive
Wake contracts. They do not cover late identity recovery with a healthy,
readable retained sidecar; that missing combined scenario is central to the fix.
