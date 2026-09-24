# Desktop startup receipt and service-owner stop gate

## Outcome and boundaries

The desktop shell needs to distinguish a Runtime it started from one it adopted.
Python produces launch provenance at the actual spawn/reuse branch; the shell
retains stop authority only for a `started` receipt. A pre-launch process snapshot
is not provenance: another launcher can start the service before `start_service`
reads the pidfile. Service and UI startup both capture their actual process
identity and reuse decision without changing their existing integer return value.

This change does not modify desktop Rust code, deployment, routing, data location,
or the existing unscoped shutdown behavior.

## Frozen schema-v1 startup line

Every successful `vibe start`, including `vibe start --no-open-browser`, writes
exactly one additional stdout line with this exact prefix and compact JSON:

```text
@avibe-start-receipt:{"schema_version":1,"outcome":"started","service_pid":1234,"ui_pid":5678,"service_create_unix_ms":1789010100123.5,"ui_create_unix_ms":1789010100456.5}
```

- `schema_version`: integer `1`.
- `outcome`: provenance from the spawn-vs-return-existing branch inside
  `start_service`, never a caller's pre-launch snapshot. `started` means its
  spawn branch, including a scoped launch resolving its authoritative PID;
  `reused` means its return-existing branch. UI reuse does not change it.
- `service_pid`, `ui_pid`: positive integer process IDs.
- `service_create_unix_ms`, `ui_create_unix_ms`: finite positive epoch
  milliseconds from `psutil.Process(pid).create_time() * 1000`, captured during
  startup, not wall-clock sampling or the human-readable status file. Fractional
  milliseconds are allowed. If a scoped launcher resolves to another service
  PID, its identity is updated to the authoritative service process.

Consumers match the prefix at the beginning of a line and parse only its JSON
suffix; other stdout remains human-readable. Failed startup must not emit a
success receipt. Unreadable process identity must not be fabricated.
The desktop consumer treats a `reused` receipt as adoption regardless of which
process invoked start.

## Frozen scoped-stop interface

```text
vibe stop --receipt '<the receipt JSON, without the stdout prefix>'
```

Before any shutdown side effect, the service pidfile must contain the receipt's
`service_pid`, and that live process's psutil create-time must match
`service_create_unix_ms` with an inclusive absolute tolerance of **2 ms**.
Malformed/unsupported receipts, missing identity, replacement PIDs, and recycled
PIDs fail closed: **exit 3**, one JSON object on stderr with a `reason` string,
no fallback to unscoped stop, no status/pidfile edits, and no shutdown calls.

Reason codes are `invalid_receipt`, `service_pid_mismatch`,
`service_identity_unavailable`, and `service_create_time_mismatch`.

A match runs today's full graceful stop, including the service sweep, UI,
OpenCode server, and tunnel. Its normal success/stop-failure exit codes remain
`0`/`2`. Without `--receipt`, output, exit codes, and behavior are unchanged.
Python validates identity, not ownership policy: `outcome=reused` is valid input;
the desktop consumer must not offer or invoke stop for adopted runtimes.

## Security rationale and known limits

A PID alone can refer to an unrelated process after recycling; checking the
recorded PID and the live kernel-backed create-time prevents a stale desktop
receipt from authorizing shutdown of a replacement Runtime at gate time.

This is a **service-owner gate**, not a per-process capability or an
authentication token. It preserves today's full-stop semantics after the gate
passes. It does not prevent same-install concurrent replacement after the gate,
nor independently authorize each UI, OpenCode, or tunnel process. A future
per-process guarantee requires a new contract version. Receipts contain no
secrets, and a same-user caller can still invoke the existing unscoped stop.

## Validation contract

- Startup output preserves human prose and emits one exact-prefix receipt with
  launch-captured identities, regardless of browser-opening configuration.
- Provenance reflects the startup function's branch even when the caller's
  earlier view of the service differs; service and UI reuse are observed at
  their owning layer.
- Every rejected scoped stop leaves shutdown collaborators and state untouched.
- Matching identities preserve full-stop behavior, including exit `2` failures;
  omitting the receipt preserves the legacy path.
- Parser and dispatch tests exercise the documented command shape.
- Fake-process tests remain hermetic. Real desktop-to-Python acceptance is
  deferred to the orchestrator's integration pass; never restart the local
  agent-hosting Runtime for verification.
