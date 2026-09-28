# Local gateway error details

## Contract

- Keep the existing localized gateway-failure summary and retry behavior.
- Preserve the operating-system reason for local runtime failures, including
  ENOSPC, EACCES, EROFS, and EMFILE, through runtime installation and gateway
  preparation. Only a numeric OS errno and its system message are public:
  exception filenames, credentials, request bodies, and arbitrary exception
  text are not diagnostic inputs.
- The exact gateway Turn owns the diagnostic. Preserve existing ambiguity,
  cancellation, and concurrent-request ownership guards.
- Store the optional `local_error_detail` on the terminal provenance error and
  the notification metadata. Reuse `FailureDetails` for a collapsed plain-text
  display, including when the provenance read is unavailable. Management-only
  visibility is enforced by transcript and live-event projections, not just the
  existing details panel. Public metadata reads recursively redact the field.
- Carry the snapshot inside the existing `turn_failure_notification` settlement
  contract so immediate, deferred, and late-attached Runs retain it. Owed notices
  copy it and replay it without depending on a still-retained provenance record.
- Read OS reasons through explicit exception causes and preserve errno at each
  exception-to-result conversion in runtime installation. Do not infer a cause
  from arbitrary exception text or unrelated implicit exception context.
- Existing records and notifications without the optional field keep their
  current behavior. No migration, new dependency, restart, or deployment.

## Validation

Exercise a real runtime-lock OSError through the installer, runtime adapter,
service, gateway, notification, and persisted provenance. Assert the summary
is unchanged and the diagnostic survives. Verify the UI expands/collapses the
diagnostic with and without a provenance record, including remounting, and
retains its authorization boundary. Run focused Python/UI tests, Ruff, and the
UI production build. GitHub CI and current-head Codex review gate delivery.

Review regressions additionally cover real supervisor spawn failures, installer
claim/pointer/candidate-validation failures, immediate/deferred durable replay,
and manager versus chat-only history/live-event reads.

## Boundary audit and scope decision

The installer-loss class recurred on reviewed heads `11a2165ea` and `9b4b150dc`.
Forwarding caught exceptions only at `_failure()` call sites is insufficient:
manifest/archive helpers convert exceptions to a stored reason and a sentinel
before the caller builds its failure result. Evolve that existing reason owner
into a reason/errno pair, clear diagnostics whenever a reason is replaced, and
preserve explicit causes through the shared OS-error extractor. Do not change
download retries, cache fallback, or best-effort cleanup into terminal failures.
Verify failed -> successful -> unrelated failed operations do not reuse errno.

The other consumer boundaries are independent of retry permission and of how a
message became visible. A management-authorized local snapshot can be expanded
on a detached/cross-Session replay without a Turn read or Retry action. Normal
append and suppressed-message promotion must both retain the internal snapshot
until recipient projection. Reuse their existing projection option and the
existing backend-failure identity check instead of creating another detail path.

The class recurred on `83a304338`: installed-runtime inspection and the admitted
call's completed `engine_down` outcome also erase errors. Audit the three entry
paths (installation, installed inspection, invocation) and their neighboring
record/health/loopback conversions before another push. Use the existing status
snapshot as the supervisor's single inspection result (path, reason, errno);
do not mix two inspections. Add optional numeric errno to the existing
`RawCallOutcome`, with one validated formatter at the service boundary.
Preserve errno in process-record and health sentinels as well, scoped to the
failed operation and captured before cleanup can replace it. Keep cleanup,
retry, process exclusion, source health, and transport ownership policies
unchanged. No exception prose or upstream body becomes a local diagnostic.

On `1c1f52c50`, inspect the remaining consumers of those owners: Retry reloads a
notice into both a POST reply and `message.updated`, and process identity capture
has its own fail-closed sentinel. Preserve internal message diagnostics until
both HTTP and event recipient projection; cover new and updated event types.
Keep process capture's existing `None` behavior for all callers, adding an
optional error observer used by the supervisor to retain only numeric errno.
Neither capture failures nor diagnostics authorize killing an unverified process.
Preserve the current status operation's manifest failure when there is no
installed pointer; an actual inspection failure replaces it. CI's existing Git
runtime, Doctor localization, and interface-mirror checks cover those consumers.

On `0586e6bcd`, boolean filesystem probes, marker recovery, and the gateway's
own response spool erase local failures. The reported Harness exposure was
disproved by real list/detail/bootstrap requests: `_harness_store` already
requests public metadata, and `_enrich_runs` applies the recursive redactor.
Keep that existing owner and add regressions rather than another filter. Use
errno-preserving regular-file probes for required runtime inputs. Recovery's
marker sweep remains the authority for retaining a record; retain its failed
operation's errno across the subsequent record write, without changing reap
policy. Keep upstream settlement/metering separate from a gateway buffer failure:
the original call still owns usage and source health, while the gateway owns
the failed local delivery and its Turn diagnostic. Do not commit buffered
success before the local reads/rewrites needed to produce the response finish.
The CI structure guards also require every ending to use `_settle_metered_turn`
and capture completion where the body is consumed. Keep buffered consumption in
the response owner and defer only projection through that existing settlement
wrapper; do not introduce another route to handle settlement or weaken guards.

On `a057c8ed1`, pair a failed spawn rollback with the record error that actually
blocks recovery, rather than the superseded spawn cause. For required executable
files, the permission predicate is itself a policy rejection: preserve that
denial as EACCES at one shared check instead of retaining a bare boolean.
Keep required-file stat failures distinct, and do not run or chmod rejected
binaries to obtain a diagnostic. Cover status, candidate verification, actual
non-executable mode bits, and a denied-access predicate, alongside two different
spawn/rollback failures and successful cleanup.

## Known by design

- Historical errors cannot acquire diagnostics that were discarded before this
  change. Ambiguous or canceled turns do not borrow another request's details.
- This change reports local OS errors; it does not retain arbitrary upstream
  error bodies or alter gateway recovery.
