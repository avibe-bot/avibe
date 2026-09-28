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

## Known by design

- Historical errors cannot acquire diagnostics that were discarded before this
  change. Ambiguous or canceled turns do not borrow another request's details.
- This change reports local OS errors; it does not retain arbitrary upstream
  error bodies or alter gateway recovery.
