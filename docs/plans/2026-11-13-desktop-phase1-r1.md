# Desktop Phase 1: local Runtime identity and guarded pairing

## Change contract

Provide the local descriptor and prepare API, extend the existing pair operation
with opt-in Desktop correlation, retain retry inputs before redemption, and
project the remote instance ID only alongside current authorization.

The installation UUID belongs to the data root. Reads do not initialize it.
An existing binding or another account's pending operation is never replaced by
Desktop registration. The existing pending-pairing record and config lock own
redemption, recovery, and compare-and-apply; no second pairing registry exists.
Legacy records remain readable. First pairing uses the existing enable/start
path; matching-owner login only reads the binding and preserves explicit Off.

The two new routes and the guarded pair extension admit only direct loopback.
Mutations additionally require the exact local Origin and existing CSRF cookie
and header. Descriptor and remote session responses contain no recovery material.

## Evidence plan

- AUTH-SETUP-172: actual prepare/pair/readback handlers recover a lost redemption
  reply using the same durable request, including after a fresh consumer starts.
  Existing recovery tests begin after a successful redeem reply and miss this gap.
- AUTH-SETUP-173: concurrent prepare and delayed pair publication cannot replace
  a foreign pending account or changed binding. Test the real lock and storage,
  with network responses held at the transport boundary.
- AUTH-SETUP-174: first pair enables and starts; later prepare on a paired Runtime
  preserves a stopped connector. Existing pair tests do not exercise relogin.
- AUTH-SETUP-175: current remote authorization projects the configured instance;
  unavailable/revoked/unauthenticated states do not.
- Direct-loopback, Origin, CSRF, damaged records, and released journal shapes use
  focused boundary tests. Test homes, state, network, and connector processes are
  fixture-owned. No live account, tunnel, or installed Runtime is involved.

## Integration and delivery

R1 is independent of native/UI PRs. B2's optional `redeem_request_id` supplies
durable response replay. The native lane assembles actual candidate backend,
Runtime, coordinator, and bundled UI handlers before claiming Merge-ready.
Signed package, Keychain, WebView stores, and live passkey association remain
separate RC acceptance. No merge, deployment, installation, or service restart
is authorized by this implementation task.

## Accepted transport details

The local jar reads `csrf_token` from `/api/csrf-token`, retains
`vibe_csrf_token`, and sends `X-Vibe-CSRF-Token` with the exact local Origin.
This retains the Runtime's existing wire spelling and policy.

An exact in-flight redemption can return backend HTTP 409 with
`{"error":"pairing_redeem_pending","retry_after_seconds":1}`. The local pair
route retains HTTP 400 and exposes that specific error, backend `status:409`,
the bounded integer delay (1–60 seconds), and
`pairing:{ok:false,recoverable:true,cause:"pairing_redeem_pending"}`.
Other status/error combinations are not pending advice. Retry retains the
same operation, recovery identifier, and entire request.

## Runnable evidence

Create a worktree-only environment with
`uv sync --frozen --group dev --no-install-project`. Build the existing UI
assets (`npm ci --prefix ui`, `npm run build --prefix ui`) before running
authorization tests that serve the SPA.

Run the actual-handler enrollment fixture:

```sh
uv run --no-sync python -m pytest \
  tests/scenarios/auth_setup/test_auth_setup_scenarios.py \
  -k desktop_enrollment_recovery_and_relogin_closed_loop -q
```

The fixture uses test-owned HOME/XDG/Avibe state, actual CSRF middleware,
prepare/pair/status handlers, config publication, binding reconciliation and
stop intent. Only the remote redemption and connector start are peers. It
uses the device name `小明的 Mac` and instance `唯一实例`; lost reply and explicit
pending variants both recover with exactly the saved request. This is Runtime
boundary evidence, not the final cross-product integration claim.

Focused compatibility validation covers `test_desktop_local_instance`,
`test_pairing_recovery`, `test_remote_access_vibe_cloud`,
`test_remote_authorization_revision`, `test_instance_authorization`, and the
auth/setup scenario harness. Before the pending-response addition this run
passed 787 tests and 16 subtests. The final focused run, including both pending
and lost-response variants plus scenario catalog citations, passed 365 tests
and 16 subtests. Changed Python files pass Ruff.

The `lint` workflow owns the required CI. This diff selects the cheap Linux
checks, unit-test shards, UI tests, scenario evidence, hosted Model Hub wire,
web-fetch runtime, and Windows agent/Model Hub/control IPC checks. Browser,
native Desktop/iOS, install-upgrade, macOS fetch, and native Model Hub lifecycle
producers are explicitly skipped by the current scope classifier. The aggregate
unit-tests, install-upgrade-regression, and windows-install-smoke gates must
still pass. Review and integration gates remain pending.
