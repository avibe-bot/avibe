# Memory x EverOS reuse audit

This audit compares Avibe's Memory integration with the capabilities in the
pinned EverOS 1.2.3 artifact. It is the decision record behind
`docs/plans/memory-rebuild-and-recovery-ladder.md`, not a mandate to replace
working Avibe guarantees with superficially similar EverOS internals.

Evidence baseline: Avibe `d6c7cf2d`; EverOS `560fb80`, whose source tree matches
the 1.2.3 release commit `48fc908` (the release commit adds only the changelog).

Verdicts use five terms:

- **Reuse now**: a supported EverOS capability directly removes Avibe code in
  the current recovery work.
- **Keep**: Avibe owns a real gateway, durability, security, or product gap.
- **Defer**: the idea lacks a prerequisite or measured payoff and should not
  produce an implementation PR now.
- **Investigate separately**: promising overlap whose storage/process contract
  is not yet safe enough to change.
- **Upstream**: EverOS needs a stable seam before Avibe should shrink.

## Executive decision

The recovery implementation reuses `cascade rebuild` and the existing EverOS
health block. It does not add a replacement indexer, parse CLI output, or invent
a second durable rebuild journal. The persisted candidate configuration plus
`embedding_change_pending` is the crash fence; EverOS's reset-queue-first rebuild
is the retry mechanism.

The original audit overstated F2, F3, F4, and F5. Their corrected contracts are
recorded below. The recovery plan is the priority workstream and may itself land
as Rebuild-and-Apply followed by Factory reset. The findings here are separate
optimization PRs or upstream work, not bundled recovery scope.

| Finding | Validated verdict | Delivery |
|---|---|---|
| F1 message batching | Defer | Blocked on durable receipts |
| F2 smaller snapshots | Defer | Research only after measured cost |
| F3 ambiguous resend | Keep; upstream receipt | Upstream prerequisite |
| F4 `cascade sync` / `fix` | Reuse `sync`; defer `fix` | Projection-repair PR / upstream contract |
| F5 count flush | Keep | No change |
| F6 provider recording | Keep; harden admission | Artifact-hardening PR + upstream |
| F7 modality drift | Add pinned contract | Same artifact-hardening PR |
| F8 idle/max-age flush | Keep; upstream seam | No local simplification yet |

## Recovery reuse selected now

EverOS markdown is authoritative for its business LanceDB projections, and
`cascade rebuild` is its supported destructive repair. Avibe should invoke that
command with the pinned artifact rather than delete or reconstruct EverOS tables
itself. The exact command is:

```text
<artifact-python> -I -m everos.entrypoints.cli.main cascade rebuild --yes
```

The command resets `md_change_state` before dropping/recreating the business
tables, then scans and drains. It preserves markdown and `unprocessed_buffer`.
`system.db` in its entirety is not a rebuildable projection. EverOS's pinned
runbook still lists the older drop/recreate/reset order, but the 1.2.3 command
source deliberately resets first to make every crash window converge.

A configured embedding outage can still fail markdown rows before any keyword
row is written, while the CLI exits 0 after handling the batch. Avibe therefore
settles vector-space identity after command completion but reports the existing
`/health.cascade` fields after sidecar startup. EverOS's `healthy`/`reasons`
describe operational health, while failed-row counts are informational; Avibe
presents the latter as a separate data-quality warning. It does not promise
keyword completeness or an exact pending-vector count. The
post-rebuild start path must not run Avibe's ordinary endpoint preflight, which
exists to protect a still-healthy old sidecar; after the old index has been
dropped it would only turn an endpoint outage into a self-inflicted start block.
A momentary `pending` count alone is normal queue telemetry.

## F1. Batch captured messages - defer until durable receipts

EverOS accepts multiple messages per `/add`, while Avibe currently delivers one
outbox row per call. Batching may reduce boundary-detection calls, but EverOS
message IDs include the payload position. A retry must resend the identical batch
or the overlap can receive different IDs.

Any change therefore needs a persisted batch composition/fence and explicit
tests for partial delivery, lease expiry, process death, and session ordering.
That is an outbox protocol change, not recovery cleanup.

## F2. Snapshot less rebuildable data - defer until measured

The earlier claim that a short authoritative-file allowlist could replace the
whole provider-root snapshot was unsafe. The root also includes
`.index/sqlite/ome.aps.db`, SQLite WAL/SHM state, Avibe's root-control file, and
`system.db` state beyond `unprocessed_buffer` (audit/task/LSN and other system
tables). A restore followed by rebuild also adds a new recoverable failure phase;
the journal's correctness model would change.

Do not change Clear/backup snapshots in the recovery PR. A later design may
prove that selected LanceDB directories can be excluded, but it must first define
SQLite-consistent capture, a versioned manifest, restore-plus-rebuild recovery,
and compatibility with existing snapshots. Prefer excluding proven projections
from a whole-root policy over maintaining an easy-to-miss authoritative-file
whitelist.

## F3. Retry before `manual_required` - keep; upstream receipt

EverOS deduplicates deterministic message IDs only while those messages remain
in `unprocessed_buffer`. If the first ambiguous request already extracted and
replaced/cleared the buffer, an identical resend has no durable receipt or
tombstone and can be accepted and extracted again.

Avibe must retain the current ambiguous-add -> `manual_required` fence. Flush
ambiguity remains fenced as well. The safe way to shrink this behavior is an
EverOS-supported durable idempotency/receipt or reconciliation API; bounded
blind resend is not equivalent.

## F4. Reuse `cascade sync`; defer `cascade fix --apply`

These are useful native projection-repair commands, but the original description
overstated their behavior:

- pathless `cascade sync` performs a full markdown scan and drains the queue;
  only the optional positional path explicitly force-enqueues one path;
- `cascade fix --apply` resets retryable failed rows' retry counts and drains,
  which can intentionally repeat embedding calls and cost;
- both commands may invoke embedding and both can exit 0 while rows still fail;
  exit 0 is command completion, not semantic repair;
- `sync` is explicitly documented live-safe. `fix --apply` uses the same atomic
  claim path, but the pinned docs do not give it the same online-safety promise;
  its retry-budget reset is also a user decision, not automatic remediation.

The pinned implementation and its prose disagree here: the CLI help/runbook says
pathless `sync` only drains, while `CascadeOrchestrator.sync_once()` actually
calls `scan_once()` before draining. Treat the source behavior as verified for
1.2.3, but require a contract test or upstream documentation fix before exposing
it as a stable product promise.

An eventual UI should expose projection repair in product language, not raw
EverOS command names. The follow-up must decide separately when live-safe
scan/drain is enough and when resetting retry budgets deserves cost confirmation
and sidecar quiescence; it should not blindly chain both commands. Each command
runs through the bounded, allowlisted one-shot child capability introduced for
rebuild and reads `/health.cascade` afterwards. A live `sync` child needs an
auxiliary ownership slot that cannot overwrite the sidecar record; this is a
role-aware extension of the same process owner, not a second supervisor.

## F5. Message-count flush - keep

The original audit used EverOS's agent-mode primitive defaults (8,192 tokens / 50
messages). Avibe does not ship that mode: it generates `memorize.mode = "chat"`.
The pinned chat path passes EverOS's configured defaults of 65,536 tokens / 500
messages to boundary detection. Avibe's `MAX_UNFLUSHED_MESSAGES = 100` therefore
fires earlier and is not dead code.

Keep the count, idle, and max-age triggers. Making the timer thresholds product
configuration is a separate feature decision and has no recovery payoff by
itself.

## F6. Provider-call recording - keep; upstream stable seams

EverOS has no equivalent payload recorder with Avibe's scrubbing and retention
contract. Its usage recorder captures token counts, and its content capture does
not provide the request/response audit surface Avibe needs. The feature remains
justified.

The current integration does patch version-sensitive EverOS symbols and reads
private SQLite schemas. A pinned-wheel CI contract already checks the patch
targets and signatures, so recreating that test would add no protection. The
near-term gap is artifact admission: run a lightweight compatibility probe before
activation. Recorder installation may disable/degrade recording without taking
Memory down. Error-scrubber installation is different: it prevents provider URLs
and API keys from reaching EverOS diagnostic persistence, so incompatibility must
reject the artifact/sidecar unless another proven path disables or scrubs that
persistence. Long-term, request an upstream `LLMClient.chat` wrapper injection
point and a supported provenance/read API.

## F7. Modality allowlist drift - add a pinned contract

Avibe's modality extension table currently equals the pinned everalgo set minus
intentional office-document and SVG exclusions. Add one exact pinned-artifact
contract at admission/CI so an upstream change fails visibly. Do not dynamically
import provider internals as runtime policy. Deliver this with F6 hardening.

## F8. Idle/max-age flush - keep until an upstream seam exists

EverOS contains idle-trigger and conversation timestamp machinery but no
supported idle-session flush behavior. Avibe's timers remain necessary because a
session that receives no more messages is never revisited by EverOS.

An upstream idle/max-age strategy could eventually let Avibe reduce coordinator
scheduling. Until that exists as a supported capability, keep the current
outbox-owned timers.

## Justified Avibe ownership

The audit confirms that these modules are not duplicate EverOS implementations:

- process supervision, UDS confinement, environment allowlisting, ownership
  recovery, and no-TCP enforcement;
- real processing probes (EverOS capability flags only prove configuration is
  present);
- attachment pinning for durable retry;
- the local outbox and conservative ambiguity fence;
- payload scrubbing, retention, and the user-facing processing record;
- confined filesystem operations, admission policy, authentication, and UI
  access control.

## Follow-up order

The verified implementation work is four focused PRs after this documentation
change:

1. **Rebuild-and-Apply (priority):** role-aware rebuild child, Runtime cutover,
   Controller operation, settings/API/UI, health presentation, and user docs.
2. **Artifact compatibility (parallel with 1):** CLI/scrubber/recorder admission
   and the pinned modality contract for F6/F7, without duplicating the existing
   real-wheel CI contract.
3. **Factory reset (after 1):** fresh-Runtime replacement and confined mutable
   root deletion as the final recovery rung.
4. **Projection repair (after 1):** reuse the child capability for an explicit
   `cascade sync` action after pinning its observed behavior. Keep `fix --apply`
   out until EverOS documents its online-safety contract; any later retry-budget
   reset remains separately confirmed and may require quiescence.

External follow-up remains:

5. Ask EverOS for durable delivery receipts and recorder/provenance seams before
   revisiting F3/F1/F6.
6. Research a versioned projection-excluding snapshot format for F2 only after
   measurements justify it; do not ship a file whitelist without a complete
   restore proof.
7. Track F8 upstream. Revisit local coordinator simplification only after the
   capability is released and pinned.

There is no implementation PR for F1, F2, F3, F5, or F8 in this workstream.
Those items are respectively blocked, speculative, a required safety fence,
already correct, or waiting on an upstream capability.
