# Desktop provider result pages

The provider callback listener currently releases its ephemeral port as soon as
one valid callback, cancellation, or timeout ends the attempt. Refreshing that
URL or returning late therefore produces a browser connection error.

Contract: the authentication attempt still accepts at most one state-validated
code and never resumes after cancellation or expiry. Its IPv4 loopback port
continues to serve one immutable, localized result page for ten minutes after
the attempt ends, then closes (or closes with the App). Every subsequent request
is presentation only. Browser success follows the coordinator's successful
account persistence; MFA still owed says to continue verification in the App.
The responder retains no PKCE verifier, state, authorization code, or account
session. Upstream failure descriptions are bounded, escaped secondary text.

The existing loopback owner keeps the socket and first valid request until the
coordinator settles. On release it transfers only the listener and rendered
result to a bounded responder. Abandoned owners render cancellation. Query
callbacks keep the current grammar and state checks. A local inline bridge
for a state-matching callback with only a fragment moves that fragment into
the same validated query path. It cannot select an external destination.

Boundaries: Desktop runtime-host, the Tauri catalog/focus-only App link, and
Desktop's existing sign-in error position. No hosted authentication, identity
linking, iOS behavior, or waiting-screen redesign changes. The four actionable
error classes are account conflict, provider server failure, provider refusal,
and timeout.

Validation: extend the owning Rust callback/coordinator tests to prove terminal
availability and no further exchanges, including cancellation, timeout, and
persistence failure; production UI plus real callback sockets in an isolated
browser proves fragment handling, refresh, and localized errors. Run focused
frontend tests, typecheck/lint, both UI builds, Rust tests/clippy/build. Packaged
Desktop and real-provider acceptance remain explicit residual checks.

Reproduction (no real account or native window): build `desktop`, build the
`provider_callback_fixture` Rust example, then run
`node desktop/scripts/provider-callback-browser-probe.mjs <evidence-directory>`.
The fixture runs the actual loopback listener; its UI bridge simulates the
coordinator reply. The separate native coordinator tests own token exchange,
state fencing, MFA, persistence failures and exactly-once redemption. The browser
probe runs 22 EN/ZH scenarios, checks first/repeated/refreshed pages, callback
fragments, escaped detail, cancellation/expiry, actionable App copy, and a
favicon request. All browser egress is restricted to its own loopback servers;
no deep-link button is clicked. Ten-minute expiry is tested with an injected
short grace duration against real sockets, not a ten-minute wall-clock wait.

Status: implemented; local validation and delivery evidence are recorded in the PR. The incident's exact historical close
trigger is unproven; the immediate port-release behavior above is reproduced.
