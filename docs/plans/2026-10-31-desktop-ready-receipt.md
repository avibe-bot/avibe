# Desktop startup readiness receipt

## Outcome

The bundled desktop shell will launch the Runtime through an opt-in startup
mode that returns one bounded, machine-readable result when Python has proved
both the Controller and the UI usable. The shell will use that result to avoid
running a second full `/ready` polling loop, then perform one bounded final
identity/presence check against the exact endpoint before navigation.

Ordinary `vibe start`, external launchers, `AVIBE_DESKTOP_ORIGIN`, Runtime
identity/provenance, scoped stop, handover, rollback, and the NSIS preparation
path retain their existing contracts.

## Change contract

- **CLI:** `vibe start --desktop-receipt` is hidden and opt-in. The bundled
  Runtime uses it together with `--no-open-browser`; ordinary callers do not
  receive or depend on the new line.
- **Wire framing:** Python writes at most one line beginning with
  `@avibe-desktop-start:` followed by compact UTF-8 JSON and `\n` on stdout.
  The complete UTF-8 frame, including the prefix and newline, is bounded to
  4096 bytes by both producer and consumer. Byte length, rather than character
  count, is the contract.
- **Schema:** version `1` contains `schema_version`, `outcome`, and `origin`.
  `outcome` is `ready` or `failed`; `reason` is required only for `failed` and
  is one of the stable bounded startup/refusal codes documented below.
  `origin` is the validated loopback origin computed by Python from the
  existing V2 config/listener owner. A pre-resolution failure, including a
  claim or handover failure before a validated origin exists, emits no receipt
  and exits through its existing nonzero mapping. Once the origin is known, a
  failed receipt is emitted after rollback has finished.
- **Startup commit boundary:** `ready` is an irreversible observation of the
  startup commit. Python completes all failure-prone startup and ownership
  work, including the shared service/UI readiness check, status publication,
  identity claim, and any rollback decision, before writing the ready frame.
  Optional housekeeping runs before the commit point and is warning-only once
  readiness has been established. Python marks the startup committed immediately
  before publishing the ready frame; a broken pipe or other ordinary output
  failure after that point cannot roll back the Runtime or emit a failed frame.
  Rust accepts the first complete ready frame as the transport acceptance point,
  after draining any invalid or duplicate events already queued before that
  point. A duplicate, contradiction, malformed frame, helper failure, or
  transport failure observed before that acceptance rejects the attempt. The
  reader stops after a complete valid frame so an inherited descendant pipe
  cannot hold a successful bootstrap thread on EOF. Acceptance is a temporal
  boundary: a helper exit or additional output racing after it cannot
  retroactively defeat startup. The final exact-origin presence/identity check
  remains authoritative and may still reject navigation; later Runtime loss
  belongs to the existing monitor and is not converted into a second startup
  verdict. This deliberately does not claim that a stream reader can reject
  events that occur after navigation.
- **Readiness owner:** Python owns startup readiness and cleanup. The
  receipt-capable path starts the service and UI through the existing
  `runtime` primitives, launches the UI without waiting for service readiness,
  and checks the UI and Controller together under one shared remaining startup
  deadline. Rust owns desktop policy, receipt validation, the bounded attempt
  budget, adoption/handover rules, and the final `/ready` identity/presence
  check. If Rust's observation budget expires while the helper is still
  in-flight, the helper remains retained by the existing launch state so Retry
  cannot launch a duplicate.
- **Rust consumer:** bundled launch reads the actual child stdout while it is
  running, drains it without waiting for pipe EOF, rejects malformed,
  oversized, duplicate, contradictory, stale, or wrong-origin receipts before
  acceptance, and navigates only after a `ready` receipt plus a final
  `Presence::Mine` or allowed unmanaged/adopted result. A missing receipt while
  the helper is alive is pending; helper failure, timeout, broken output, or a
  failed receipt is terminal failure. A final probe contradiction never falls
  back to receipt success. The endpoint helper remains for endpoint discovery
  and is not replaced by an unsafe Rust config guess.
- **Exit mapping:** `ready` requires helper success or an observed still-running
  helper plus the final readiness check; a nonzero helper exit is failure.
  Receipt `failed` is failure and preserves existing retryability. Legacy
  launchers continue using the existing `vibe start` plus Rust readiness path.
- **Failure reasons:** after origin resolution, Python emits only
  `startup_failed`, `handover_refused`, `handover_failed`,
  `runtime_claim_refused`, or `runtime_not_ready`. The Rust consumer maps
  `failed` to the existing retryable startup failure notices and preserves
  exit/refusal distinctions from the helper. A `RuntimeError` raised inside the
  guarded service/UI/readiness region maps to `runtime_not_ready`; other
  unexpected failures map to `startup_failed`. Pre-resolution failures carry no
  receipt, so Rust never has to trust a guessed origin.
- **Ownership:** the receipt is an observation/transport optimization. It does
  not grant stop, quit, uninstall, handover, or process ownership authority.
  The expected Runtime ID and the final exact-origin `/ready` identity/presence
  check remain authoritative; the receipt intentionally does not duplicate
  identity fields.

## Removed wait and evidence

The bundled path removes the Rust full startup `/ready` polling loop that
duplicates Python's service/UI readiness work. The remaining Rust probe is a
single final check immediately before navigation. This removes probe/process
duplication and may or may not reduce elapsed cold-start time; it does not
promise a Windows speedup from historical Mac measurements. Existing bounded
bootstrap diagnostics now mark receipt arrival, receipt acceptance, final
presence verification, and successful navigation alongside the existing
endpoint/start records. Helper/probe counts and elapsed time can therefore be
compared on identical hermetic fixtures. Early-ready and slow-controller
fixtures must show that navigation does not move later than the current path.

## Verification

1. Parser-backed Python CLI tests cover the hidden invocation and preserve
   ordinary `vibe start` semantics.
2. Python tests exercise complete, failed, duplicate, and cleanup outcomes.
3. Rust tests cover the receipt parser and bootstrap state transitions,
   including wrong origin/identity, malformed or oversized output, helper
   failure, delayed output, retry, close, handover, and reuse.
4. A hermetic cross-language fixture sends real Python-produced stdout through
   the Rust parser/state-machine boundary, including spaces and non-ASCII paths.
5. Temporal tests cover transport failure before successful publication, the
   ready commit boundary, duplicate/failure output before acceptance, helper
   observation timeout with retry deduplication, and final probe contradiction.
6. Baseline/candidate timing and probe-count traces use identical hermetic
   fixtures for early-ready and slow-controller cases. The report distinguishes
   actual reductions from remaining Windows packaged/manual acceptance.
7. Focused Python tests, Ruff, Rust tests, and desktop shell boundary checks
   run before the PR. Windows packaged/manual acceptance remains a handoff item.
