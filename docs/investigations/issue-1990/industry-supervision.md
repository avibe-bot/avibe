# Can Memory remove bespoke process identity checks?

Research date: 2026-09-15. This addresses the owner's architectural question,
not merely which timestamp getter to use. No product code or live services
were changed. Repository observations are at
`199cccf958349881209c00b20124b82726f3ce0c`.

## Recommendation

Remove repeated birth-stamp authentication as a normal-running admission gate.
Use the spawned child object and its exit notification to track the direct
child; use service health to determine whether it serves requests. Preserve
small, action-local protections when signaling numeric PIDs/process groups,
and isolate cross-restart orphan recovery from the normal path.

This removes the mechanism that converts a healthy child's changing display
timestamp into an outage. It does not require a new native identity API, new
record schema, or a recovery loop whose sole purpose is recovering from that
unnecessary running-state check.

Do not equate removing continuous checks with blindly killing any PID found
in an old file. PID reuse and concurrent writers are real concerns at that
different boundary. Public process-library operations can provide ordinary
per-action reuse checks without Avibe implementing repeated authentication.

## Established patterns and their limits

| Pattern | What it provides | What it does not provide |
| --- | --- | --- |
| Foreground child plus exit notification | Parent starts, waits for and restarts its own child; standard subprocess/supervisor model | Arbitrary descendant cleanup or retained ownership after the parent restarts |
| POSIX process group | Signal propagation to children remaining in the group | A persistent process handle; containment of helpers that detach into another session |
| Lifecycle pipe | Child detects loss of its originating parent through EOF/readiness on an inherited descriptor | Forced termination of a wedged child or proof all grandchildren stopped |
| systemd unit/cgroup | Linux service manager tracks and stops unit processes as a group | A portable macOS implementation |
| launchd job | macOS service manager owns launch, exit and restart lifecycle | Zero deployment/configuration work or an assumed universal guarantee for escaped descendants |

Supervisor documents foreground, non-daemonizing children and SIGCHLD-driven
exit tracking. Its stopasgroup/killasgroup options target a process group on
termination, rather than authenticating each child repeatedly during normal
operation. [Foreground subprocess model](https://www.supervisord.org/subprocess.html),
[group stop configuration](https://supervisord.org/configuration.html#program-x-section-values).

Python exposes child objects and wait/terminate/kill operations. On POSIX those
objects are not Windows process HANDLEs or Linux pidfds. CPython 3.12's
`Popen.send_signal()` polls first to reduce PID-reuse races; its source expressly
notes a remaining race. asyncio's threaded watcher calls waitpid before
scheduling its loop callback. Thus `returncode is None` alone is not an atomic
kernel identity proof. Use the library lifecycle rather than a fresh bare PID
lookup, and do not claim perfect signaling guarantees.
[subprocess source](https://github.com/python/cpython/blob/v3.12.0/Lib/subprocess.py),
[asyncio watcher source](https://github.com/python/cpython/blob/v3.12.0/Lib/asyncio/unix_events.py),
[asyncio API](https://docs.python.org/3.12/library/asyncio-subprocess.html).

psutil's public Process equality/is_running/send_signal operations incorporate
process identity checks. Its macOS 7.2.2 identity uses the unadjusted native
birth time, unlike the public display-time getter Avibe currently compares.
Keeping a captured public Process object for a signal operation avoids a new
Avibe timestamp protocol. It remains a user-space check, not an atomic kernel
handle, and does not by itself authorize group-wide signaling.
[psutil implementation](https://github.com/giampaolo/psutil/blob/release-7.2.2/psutil/__init__.py).

CPython multiprocessing itself uses descriptor-based parent liveness:
`_ParentProcess.is_alive()` waits on a parent sentinel. This supports the
lifecycle-pipe pattern; it does not mean arbitrary subprocesses automatically
exit when a parent dies. Avibe would have to implement that behavior in its
sidecar launcher, with strict descriptor inheritance/closure rules.
[parent sentinel](https://github.com/python/cpython/blob/v3.12.0/Lib/multiprocessing/process.py),
[POSIX spawn descriptors](https://github.com/python/cpython/blob/v3.12.0/Lib/multiprocessing/popen_spawn_posix.py).

systemd defaults KillMode to control-group and stops remaining processes in the
unit's cgroup; process-only stopping is discouraged because descendants can
outlive the unit's expected lifecycle.
[official manual source](https://github.com/systemd/systemd/blob/main/man/systemd.kill.xml).
Apple recommends launchd for per-user background agents, with job configuration
and optional KeepAlive. This is an alternative owner, not an identity library
to drop into Avibe's current supervisor.
[Apple guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

## What makes current Avibe complicated

`avibe_memory/process.py` implements three concerns together:

1. Manage a direct child created by asyncio, in its own session.
2. Discover and stop descendants, including remnants after the leader exits.
3. After an Avibe crash, reconstruct authority from files, command lines, UID,
   root/socket paths and process stamps, then clean old execution automatically.

The code also checks for prohibited TCP listeners. Removing birth-identity
polling is not permission to remove this separate service-exposure policy.
In particular, `_monitor_child` must not simply be deleted wholesale.

`core/process_isolation.py` already has generic live-child and orphan-cleanup
helpers, but it also uses public creation-time equality and process markers.
Reusing it wholesale would import similar complexity and the same adjustment
assumption. Inventory and reuse its simple lifecycle pieces where appropriate;
do not call it a verified cure for this incident.

The current provider-root lock is released after successful startup; it is not
a child-held lifetime lock. Removing orphan checks on the assumption that an
existing lock already excludes concurrent writers would therefore be incorrect.

## Can all identity recovery be removed?

Yes, if the process-lifecycle contract changes explicitly. A plausible design
for a cooperative optional sidecar is:

- The child remains foreground and receives a private parent-liveness pipe.
- Parent disappearance requests child shutdown through the pipe's EOF.
- Every writer holds an agreed data-root lifetime lock while it can access data.
- A new owner starts only after it acquires that lock; it never guesses an old
  PID's ownership and kills it solely from persisted metadata.

The lock must protect the actual writing lifetime, including any surviving
writer descendants. A lock held only by the controller or only during startup
does not provide this invariant. Pipe write ends must not leak to descendants.
If an old child hangs and retains the lock, the new runtime waits or reports
blocked; automatic forced cleanup then requires an external manager or a small
verified signaling fallback. Released old children do not yet obey the pipe/
lock contract, so migration cannot simply delete their recovery path.

This can reduce long-term bespoke discovery code, but is a lifecycle change
with a real availability tradeoff. It is not a few-line removal of safeguards.

## Proportional next step

For this issue, recommend standard direct-child lifecycle management and public
library checks at destructive boundaries; retain existing cross-restart
reconciliation as an exceptional compatibility path. Validate the same-process
clock excursion, genuine exit/reuse, retained descendants and old-record startup
without letting timestamp observation interrupt healthy processing.

Treat eliminating orphan recovery entirely via a pipe and lifetime lock as a
separate product decision: either accept blocked startup for an uncooperative
old child or adopt a platform service manager for forced cleanup. Do not add
launchd/systemd registration just to repair this optional child's timestamp
handling. The recommendation is an architectural inference from the sources
and repository, not a completed implementation or end-to-end validation.
