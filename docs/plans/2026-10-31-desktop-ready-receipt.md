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
  Runtime uses it together with `--no-open-browser` and hidden
  `--desktop-ready-timeout <seconds>` carrying the shell-validated budget; ordinary callers do not
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
  Python marks the startup committed immediately before publishing the ready
  frame. Optional install-generation housekeeping runs after that publication
  and is warning-only; it cannot delay Workbench admission or revoke the
  committed Runtime. A broken pipe or other ordinary output failure after that
  point cannot roll back the Runtime or emit a failed frame.
  The reader validates the entire read batch (at most 8192 bytes) containing
  the first complete frame. A second protocol prefix in that batch rejects it.
  Rust accepts that one terminal result only after checking origin and any
  already observed helper failure. A malformed frame or oversized prefixed
  line is terminal immediately, even without a newline. The verdict is sent
  after this batch; admission does not wait for the reader's subsequent drain
  to EOF. Acceptance is a temporal
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
  deadline. The Python desktop readiness probe uses the existing httpx
  dependency with `trust_env=False` and `follow_redirects=False`, and probes
  only the exact loopback `/ready`. LAN `/health` is not a desktop gate.
  Each entire response (including its body, bounded to 1024 bytes) is bounded
  by the smaller of 5 seconds and the remaining deadline; streamed bytes cannot
  reset that budget. Service provenance still passes through the existing
  `claim_ready_service`. Rust owns desktop policy, receipt validation, the bounded
  attempt budget, adoption/handover rules, and the final `/ready`
  identity/presence check. If Rust's observation budget expires while the
  helper is still in-flight, the helper remains retained by the existing launch
  state so Retry cannot launch a duplicate.
- **Transport lifetime (2026-11-01 source decision):** use ordinary blocking
  `std::io::Read` on the existing dedicated thread, with a bounded channel to
  the admission state machine. No platform pipe adapter or added dependency.
  Channel waits spend only the shared remaining deadline. The helper reaper
  signals exit; a successful exit allows at most 100 ms to receive an already
  written frame. Accept/fail/timeout is final for that observer; later results
  cannot change it. After its first frame the reader discards remaining stdout
  through EOF without affecting admission. Current service/UI spawn primitives
  redirect stdout/stderr to log sinks and close unrelated handles; the sinks
  also redirect their output and close unrelated handles. A real cmd_start
  fixture must prove EOF after helper exit while its child is still alive, on
  both macOS and Windows CI. Residual: a future descendant incorrectly inheriting
  receipt stdout can leave one idle reader thread blocked until it exits.
  Admission, rollback, navigation and Retry deduplication remain bounded and
  unaffected. This is an explicit accepted limitation, not a claim that EOF is
  guaranteed for arbitrary descendants.
- **Rust consumer:** bundled launch reads the actual child stdout while it is
  running, drains it without waiting for pipe EOF, rejects malformed,
  oversized, stale, or wrong-origin receipts, plus duplicate/contradictory
  prefixes within the documented read batch, and navigates only after a
  `ready` receipt plus a final
  `Presence::Mine` or allowed unmanaged/adopted result. A missing receipt while
  the helper is alive is pending; helper failure, timeout, broken output, or a
  failed receipt is terminal failure. A final probe contradiction never falls
  back to receipt success. The endpoint helper remains for endpoint discovery
  and is not replaced by an unsafe Rust config guess.
- **Exit mapping:** `ready` requires helper success or an observed still-running
  helper plus the final readiness check; a nonzero exit observed before
  acceptance is failure.
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
single final check immediately before navigation. This removes duplicate
probes, not helper processes, and may or may not reduce elapsed cold-start time; it does not
promise a Windows speedup from historical Mac measurements. Existing bounded
bootstrap diagnostics now mark receipt arrival, receipt acceptance, final
presence verification, and successful navigation alongside the existing
endpoint/start records. Helper/probe counts and elapsed time can therefore be
compared on identical hermetic fixtures. Early-ready and slow-controller
fixtures must show that navigation does not move later than the current path.
The timing report separates time to Python readiness, receipt acceptance, the
final probe, and navigation; a timeout comparison alone is not startup-speed
evidence.

## Verification

### Review breaker scope (2026-11-01)

The orchestrator independently confirmed the second findings-bearing head and
authorized one combined correction: preserve the replaced Rust probe's 5-second
whole-response allowance, pass the shell's validated startup timeout to Python,
and acknowledge an already-presented FAILED result on its retained attempt.
No identity, stop authority, ownership, rollback, or platform transport changes
are part of this correction. Tests must cover healthy 700 ms / 2.5 s responses,
one non-resetting total deadline, the shell override reaching actual cmd_start,
and retained-attempt transitions before and after helper exit. The compatibility
inventory below will record every preserved or intentional difference against
master `bc6cd410ccb26cacc5fbd422f8198371bd272892`.


### Replaced-wait compatibility inventory

All old locations below refer to that frozen master commit, not this branch's
moving line numbers. The final Rust presence probe and its identity classifier
remain in place; Python supplies readiness observation, never ownership.

| Property | Old Rust behavior / master source | New producer and consumer behavior | Status / reason | Pinning evidence |
| --- | --- | --- | --- | --- |
| Whole-response request budget | 5 s, `desktop/runtime-host/src/bootstrap.rs:40`; reqwest total timeout in `health.rs:48` | `min(5 s, remaining)` around the entire HTTP response, including streamed body | Preserved; the accidental 500 ms cap is removed | `test_desktop_startup_readiness_uses_direct_no_redirect_http` real 700 ms and 2.5 s healthy responses; drip case below |
| Poll cadence / first probe | Sleep 500 ms before each Rust request, `bootstrap.rs:507` | Immediate Python observation, then 100 ms between incomplete observations; the existing Python readiness cadence remains | Intentionally changed: consolidate onto Python's existing cadence without delaying early readiness behind a new 500 ms sleep | Repeated actual `cmd_start_receipt_admission`; `starting` and transport-error cases require multiple requests under one budget |
| Default total budget / override | 120 s; `AVIBE_DESKTOP_READY_TIMEOUT_SECONDS`, integer 1..600; invalid values ignored, `bootstrap.rs:24`, `:33`, `:80`, `:94` | Shell still parses/validates it. `launch_with_ready_timeout` passes the resolved duration using `--desktop-ready-timeout`; actual CLI parser routes it to cmd_start | Preserved, including overrides above 120 s. Python no longer silently hard-codes 120 s for a shell-selected duration | `a_timeout_override_must_be_a_positive_bounded_number_of_seconds`; real `cmd_start_receipt_timeout_override` (env → shell settings → host → production argv builder → Python parser/actual wait), CLI 1/600 s remaining-budget cases; OS argv contract |
| Deadline across retries / phases | One outer deadline; checked before sleep/probe, `bootstrap.rs:507` (a final probe could overrun it) | One producer deadline covers service/UI launch and remaining HTTP wait. Each request/sleep is capped by remaining time. Shell observation and final verification also cannot reset their deadline; a still-live helper is retained when shell observation expires | Preserved non-resetting budget, intentionally stricter bound on the last request. Interpreter/config/handover work retains its existing lifecycle limits; observation timeout grants no kill authority | `test_desktop_ready_request_body_has_one_deadline_and_size_bound`; CLI shared-readiness test; final-probe deadline and retained-helper tests |
| Proxy / redirects | `.no_proxy()`, redirect policy none, `health.rs:49` | `trust_env=False`, `follow_redirects=False`; only exact loopback `/ready`, not LAN `/health` | Preserved | Real inherited-proxy traps and redirect listener tests |
| Refused connection / timeout / incomplete response | Refused → Absent; other errors → Unknown, `health.rs:65`; both keep waiting while helper is viable, `bootstrap.rs:514` | HTTP failure or non-affirmative payload keeps waiting while the service is viable, spending remaining budget. Service death still fails via existing Python cleanup | Preserved retry classification, with existing Python service-death cleanup retained | Controlled HTTPX connection/timeout errors; `starting`, `malformed`, `drip` body cases; cmd_start created/reused rollback tests |
| HTTP / payload / identity | Strict successful schema, product, boolean and identity format; 503 identity mismatch is non-ready, `health.rs:75`, `:135`, `:170` | Shared Python strict payload predicate; only affirmative `/ready` can produce READY. Initial/final Rust `Presence` checks still enforce Mine/Foreign/Unmanaged and recovery policy | Preserved. Receipt never substitutes for the final current endpoint/identity check | Existing payload/identity fixtures, malformed body, final-presence contradiction, adoption/recovery tests |
| Origin | Literal safe loopback via `origin.rs:58`; URL origin canonicalization | Python supplies config-derived endpoint; receipt parsed through the same Rust boundary, including default port and IPv6 | Preserved | `receipt_origin_comparison_uses_the_loopback_canonical_form` (IPv4 + IPv6); Python origin tests; unsafe origin boundary tests |
| Response encoding / size | UTF-8 only, maximum 1024 bytes; reject oversized Content-Length and streamed bodies, `health.rs:9`, `:106` | Raw identity-encoded bytes and explicit UTF-8 decode (no implicit decompression), 1024-byte header/body bound before accumulation; same whole-response timeout even with drip output | Preserved; inventory also closed Python bytes-JSON auto-detection and missing early Content-Length rejection | `utf16`, `compressed`, `oversized`, `oversized-stream`, `drip` cases in the real HTTP body-bound test |
| Attempt failure / Retry | A previously unseen nonzero exit is reported once; live helper retained, `bootstrap.rs:557`, `:571` | Origin-valid FAILED is acknowledged on that attempt after publication. A live helper remains deduped; once it exits, first Retry releases its slot and launches exactly once. Unreported exits retain their old notice/Retry semantics | Preserved user-visible Retry behavior with receipt-before-exit ordering accounted for | `retained_receipt_attempt_transitions_preserve_first_retry_and_live_deduplication` |

The only intentional differences in the moved wait are the existing Python
100 ms cadence / immediate observation, and stricter enforcement of the last
request's remaining deadline. Neither weakens usable-state evidence or makes
an observation timeout authority to stop reused state.

### Retained-attempt transition inventory

The table test covers each exit as generic failure, handover refusal, and
handover failure; the acknowledgement lives on the attempt's LaunchWatch and
cannot transfer to a newer attempt. Origin validation precedes acknowledgement.

| First observation | Retry while helper is live | First Retry after nonzero exit |
| --- | --- | --- |
| Valid FAILED receipt, already published | Same failure, no duplicate launcher | Release slot and launch once in this Retry |
| Wrong-origin FAILED | Refuse, no duplicate launcher | Report previously unseen exit; following Retry may launch |
| EOF without receipt | Refuse, no duplicate launcher | Report previously unseen exit once |
| Observation deadline / cancelled receipt observation | No overlapping helper | Report previously unseen exit once |
| Healthy attempt, Runtime still ready | Adopt live Runtime | Adopt live Runtime; late helper exit cannot revoke readiness |
| Legacy helper without receipts | Retain while pending | Preserve old notice and subsequent-Retry behavior |

Explicit Stop/newer-run races continue to be pinned separately by
`a_newer_attempt_or_stop_fences_a_receipts_in_flight_final_probe` and the existing
stop/ownership fence tests. A cancelled observation is not Runtime Stop.

### Verification gates

1. Parser-backed Python CLI tests cover the hidden invocation and preserve
   ordinary `vibe start` semantics.
2. Python tests exercise complete, failed, duplicate, and cleanup outcomes.
3. Rust tests cover the receipt parser and bootstrap state transitions,
   including wrong origin/identity, Retry/Stop during final verification, malformed
   or oversized output, helper
   failure, delayed output, retry, close, handover, and reuse.
4. A hermetic cross-language fixture sends real Python-produced stdout through
   the Rust parser/state-machine boundary, including spaces and non-ASCII paths.
5. Temporal tests cover transport failure before successful publication, the
   ready commit boundary, terminal malformed/oversized output, duplicate output
   already buffered in one real stdout batch, helper observation timeout with
   retry deduplication, canonical default-port origins, and final probe
   contradiction.
6. The hermetic timing fixture has early-ready and delayed-controller outcomes
   with repeated samples and real Python stdout consumption. Baseline and
   candidate traces report helper/probe counts and time to each startup
   milestone separately. The report distinguishes actual reductions from
   remaining Windows packaged/manual acceptance.
7. Focused Python tests, Ruff, Rust tests, and desktop shell boundary checks
   run before the PR. Windows packaged/manual acceptance remains a handoff item.

## Local integration evidence (2026-11-01)

The explicit cross-language gate runs actual `cmd_start` and its parser,
readiness HTTP, private status writes and stdout through the production Rust
reader and `RuntimeHost` with `HttpHealthProbe`, including the expected identity.
The bootstrap log records receipt arrival/acceptance and final verification. Process creation and Controller
scheduling use controlled fixture seams; this is not a full installed Runtime
or native Windows benchmark. Every subprocess has an empty inherited environment
and private HOME/XDG/Avibe/credential directories under a path with spaces and
non-ASCII characters. It never contacts user services. No endpoint helper is
removed: production still uses one discovery helper and one start helper.

Three fresh helper processes per mode and scenario (after fixture Python imports
and endpoint initialization), same debug build and
production 500 ms legacy polling interval, all reached successful admission:

| Scenario | Legacy admission (ms) | Receipt admission (ms) | Rust probes, legacy → receipt |
| --- | --- | --- | --- |
| Early ready | 505.0, 507.1, 510.7 | 106.8, 94.7, 93.5 | 2 → 2 |
| Controller delayed 1.1 s | 1526.6, 1517.0, 1516.5 | 1158.2, 1160.9, 1157.4 | 4 → 2 |
| Housekeeping delayed 1.2 s | 505.3, 504.4, 504.5 | 93.5, 92.2, 96.9 | 2 → 2 |

Admission means `RuntimeHost` permits navigation, not rendered WebView latency.
The initial adoption probe is included. Python used one `/ready` for early and
housekeeping cases; delayed Controller used 11–12 baseline and 11 candidate
probes. Baseline additionally called `/health` on each Python poll; candidate
made zero `/health` requests. All cases started one fixture service and one
fixture UI, then reused the live endpoint on Retry without another launch.
Candidate admission preceded housekeeping completion in every delayed sample
(the helper completed around 1293.1–1311.2 ms). The former 30 ms comparison is a
**timeout/probe-count test only**, not time-to-ready performance evidence.

Run the integration gate explicitly (ignored in ordinary Cargo tests because it
needs the repository Python dependencies):

```sh
AVIBE_TEST_PYTHON="$PWD/.venv/bin/python" cargo test \
  --manifest-path desktop/Cargo.toml -p avibe-runtime-host \
  cmd_start_receipt_admission -- --ignored --nocapture
```

Both desktop CI platforms install the existing locked Python dependencies
without an editable project build and explicitly run
`cmd_start_stdout_reaches_eof_while_runtime_children_remain_alive`. This uses
actual `cmd_start` and the service/UI spawn primitives plus their real log
sinks, then proves receipt-pipe EOF after launcher exit while both spawned
children acknowledge a later liveness request. The fixture owns all child
state and stops them with bounded test controls. It does not mock pipe reads,
Popen, log-sink setup, descriptor inheritance, or process isolation.
Its HTTP listener binds a numeric loopback address without reverse DNS.
The macOS CI initialization stack exposed `HTTPServer.server_bind` waiting in
`socket.getfqdn`; a DNS trap reproduces the original fixture failure and the
corrected fixture reaches READY and EOF with that lookup forbidden. This
changes fixture setup only, outside the measured startup interval, and leaves
the 15-second initialization and 1-second post-exit EOF checks intact.

Windows CI covers the native pipe reader and shell build. Packaged cold/warm
NSIS launch, release-matched identity/handover, UI/controller failures, and
close/retry acceptance still require the Windows artifact. These local results
make no Windows elapsed-time claim.
