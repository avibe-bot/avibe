# Backend-reported Model Hub terminal failures

## Change contract (2026-10-13)

A source-origin Vibey `stalled` failure reached the Hub failure bridge as text
only. `fail_hub_attempt` wrote `protocol_error` and `stream_started: true`, even
when neither fact was established. This change corrects recorded diagnostics;
it does not change retries, timeouts, route selection, Source health, or Stop.

- The existing BaseAgent -> ModelHubRuntimeRouter -> correlation failure bridge
  accepts an optional structured `error_kind`. Vibey supplies its existing
  `RunEnded.cause.kind` only for source-origin failures. Do not classify by
  diagnostic substrings, infer phase from retryability, or use a prior response.
- Backend-only fallback records use `terminal_error.reason = backend_error`,
  optional `error_kind`, and optional `backend_error_detail`. A `stalled` kind
  remains `stalled`, including in copied JSON and localized detail presentation.
  Other backends without typed facts use the neutral backend-error reason.
- `backend_error_detail` is a credential-redacted, control/bidi-stripped,
  whitespace-collapsed, at-most-400-character diagnostic, not an upstream API
  answer. Reuse the existing Model Hub diagnostic sanitizer by moving it to
  an appropriate shared owner rather than maintaining a weaker second policy.
- `stream_started` may be null on a backend-error record: the callback has no
  exact failing-request phase evidence. Unknown is neither false nor true.
  All gateway-owned terminal reasons keep their existing boolean phase contract.
  Propagating additional provider phase telemetry is not required for this fix.
- An already authoritative gateway failure (terminal projection OR concrete
  terminal_error, even when that reason has no copy projection) must survive a
  backend notification. Keep Stop/frozen-outcome and late-success protections.
- Existing released provenance records remain readable without the new fields.
  Do not rewrite old history by guessing what really happened. Update the owning
  JSON schema, TypeScript mirror, API contract, and English/Chinese UI copy.
- Failure details label the backend diagnostic distinctly, never claim it is
  upstream response text, and show the typed kind (a localized stall label for
  stalled; other kinds may be displayed as diagnostic machine identifiers).

## Validation obligations

The primary regression must exercise the real Vibey adapter and shared failure
bridge into persisted provenance: existing coverage stubs out that bridge and
only checks notice copy. A scripted stalled provider must not become a protocol
error, and the old implementation must fail the new assertion.

Extend the existing correlation tests for authoritative gateway errors without
copy projections, generic native-backend notifications, preservation of late
success/Stop rules, and historical reads. Exercise non-ASCII diagnostics and
redaction. Add UI consumption coverage distinguishing backend diagnostics from
upstream API facts; run focused tests, changed-file Ruff, UI build, exact-head
Codex review and full CI. No production-state writes, deployment, release,
service restart, or merge is authorized.

## Implementation and owner verification

- The main regression and the updated late-success/correlation expectations
  fail on the base implementation for the intended reason; the main regression
  uses the real shared bridge, not a mock of its classification.
- The implementation preserves an existing concrete gateway terminal only when
  no pending request supersedes it. It freezes that terminal before the late
  success can replace it. The regression covers both branches and mutation of
  the freeze.
- Owner review found the old upstream-text redactor insufficient for newly
  retained native diagnostics: credentialed URLs, Basic authentication, and
  JWT-shaped tokens survived. Reuse `redact_provider_text` on backend diagnostics
  before the shared display sanitizer. Three consuming cases fail without this
  step and all pass with it. The existing upstream message path is unchanged.
- The stall label describes the observation (a model request timed out waiting
  for data), not an unproved claim about which remote component stopped.
- Focused owner verification includes the Vibey-to-provenance regression,
  gateway terminal preservation and native diagnostics, released-record reads,
  all of `test_model_hub_provenance.py`, late-success/Stop tests, 32 UI tests,
  Model Hub authority closure, changed-file Ruff, and `npm run build`.
- Scenario: `MH-TURN-RESULT-001`. Repository-wide CI and exact-head Codex review
  remain the asynchronous delivery gates. No live provider/Incus regression was
  run; this change introduces no model request or retry behavior.

## Independent audit follow-up

Read-only audit of implementation `53e7da45a` found three boundary defects;
owner reproduced them on the pushed `22b9342c4` before editing. Four new checks
failed for their intended reasons (one case has two projection variants):

1. A newly admitted request retained the preceding request's terminal error
   and copy projection, so an FSM drop could report the wrong Source. New
   actual attempt admission now atomically retires both terminal artifacts,
   while retaining fallback history and the existing frozen/Stop veto.
2. An unattributed backend callback froze the trace before checking for a
   pending/served payload, hiding subsequent gateway evidence. Such a callback
   is now a no-op; only a concrete terminal or a recordable backend error freezes.
3. The schema allowed boolean phase on backend_error despite writing null.
   Its positive branch now requires null; both boolean counterexamples fail.

The owner chose these local corrections, not a lifecycle or retry redesign.
The three changed correlation suites pass **889 tests**, and the two Vibey
adapter/context suites pass **158 tests** after the changes. Ruff and authority
closure pass. GitHub Codex passed the previous head `22b9342c4`; a new exact-head
review is still required after this correction. No GitHub findings-bearing head
has recurred; the independent audit findings were reconciled together once.

## Known-by-design

- Records remain generation 11 as with earlier additive diagnostic additions;
  old records are not rewritten. Downgrading to an older UI may show an unknown
  label for a new backend-error record, without making stored data unreadable.
- Only Vibey currently supplies a typed kind, from `NAMED_ERROR_KINDS`; generic
  native callbacks retain no guessed kind. The shared identifier-shape check is
  not a classifier or authority for future backend kind vocabularies.
- The 600-second silence bound, gateway idle recovery, Source health and routing,
  and the separate raw warning-log format are outside this change.
