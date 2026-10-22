# S2b: search-service producer and activation

## S2b-2 activation change contract

S2b-1 merged as `424b3fd`. S2b-2 sets `SEARCH_SERVICES_ACTIVE = True`: the Tools read,
S4's existing `service_vendors` UI predicate, frozen turn choices and execution activate together.
The retry route is now required by the client/server inventory, with no dormant exception.
The shipped-default activation test does not monkeypatch the flag; its read and chain assertions
must both pass. Existing producer tests run under that default, and prior dormant assertions
now cover the active shape. No vendor request, mapping, billing or custody rule changes.

Validation covers focused hosted-tools/read/chain/API tests through `hd1-test`, the UI build and
relevant Vitest cases, with synthetic responses only. No live paid search or deployment is included.
The release workflow owns notes (`docs/release.md`); the PR body supplies English/Chinese disclosure
covering DeepSeek auto-enrollment/opt-out, query egress, list prices and downgrade loss (§12).
The owner coordinates merge with S6 public disclosure; this lane does not merge.

The close-out release check found `gh-v3.2.3rc1` carrying S2a/S2b-1's v19; the
2026-10-22 recheck also found `gh-v3.2.3rc2`. Activation therefore opens v20, advances all
version mirrors and preserves persisted TurnProvenance v5–v19 without rewriting it.

## S2b-1 change contract (historical)

Implement `web-search-services.md` §§3–5 behind `SEARCH_SERVICES_ACTIVE = False`.
The flag-off chain, hop facts, frozen turn and Tools read retain their released behavior.
Activation is a separate S2b-2 PR coordinated with S6 disclosure; this PR does not activate or merge itself.

Owning boundaries:
- `hosted_tools` / `hosted_turn`: one-role candidate order, read-time placement, disabled tail,
  frozen prefer-native/results/identities and the same chain in the read.
- `search_services`: catalog request/response mapping, bounded records, outcomes, billing and
  service-scoped cooldown fencing; no credential access or model-engine requests.
- `ModelHubService`: final admission, Source settlement fencing, one-shot recovery trials,
  cancellation-safe metering, evidence and mixed service/model calls under one deadline.
- Model Hub RPC/UI API and authorization: explicit cooldown clear, plain success response.
- C-6 and Model Hub contract: authoritative search behavior and fixtures.

Invariants and primary regression coverage:
- A turn's frozen roles/settings cannot be altered by later settings writes; re-identification
  can only prevent admission. Automatic and deliberate orders use one placement rule.
- Every reached candidate has exactly one attempt fact, including a pre-send failure or skip;
  model-only chains retain the released record shape. Skip precedence and terminal selection
  do not mistake disabled providers for enabled blockers.
- Only credential/balance/catalog-limit responses write Source blockers; service retries
  never change model-route health. Source and service generations fence independent state.
- Exactly one trial reserves immediately before sending. Connecting contenders share the
  re-enable fence until the admitted winner starts its request; a skipped loser cannot invalidate
  that winner. A slow contender admitted after recovery or re-arming takes fresh fences, using the
  trial epoch rather than another request's generation to validate its authorization. Async final admission
  holds a short lock after connection, never during DNS/TLS, and runs only after the HTTP client owns the
  connection so rejection closes it. Trial release and queued usage drain survive persistence failures.
  Identity changes retire trials; stopped calls leave no record and settle observed status/billing.
  Stop alone creates no failure/cooldown; a received failure status still settles its outcome.
- The chain has one 180 s budget. Only service attempts have a 30 s limit; model executors
  retain the remaining chain budget rather than paying for an additional attempt after 30 s.
- Sent/status/body facts determine billing once: known-unbilled is explicit zero, unknown
  charge is never falsely priced. Each sent request is metered exactly once.
- Mapping/JSON parsing run off the controller loop, preserve ranking, bound every output
  field and request excerpts only. Empty result arrays complete.
- Retry is available regardless of activation and obeys Model Hub Member authorization.

Tests use synthetic vendor bytes and a fake adapter search boundary, never paid traffic or
real keys. A local TCP/real-aiohttp test additionally owns transport-closure coverage that fake sessions
cannot provide; it never contacts a vendor. Focused tests run through `~/.local/bin/hd1-test`; full repository checks run in CI.
Existing released-chain tests are the flag-off control; new tables target each owner boundary.
Scenario: `MH-SEARCH-SERVICES-001` in `tests/scenarios/model_hub/catalog.yaml`.

Local validation (2026-10-21):
- Initial broad API/runtime/authorization/settings/custody/C-6/producer run: 1,755 passed.
- After review fixes: runtime, producer, released executors/settings/read/config: 909 passed;
  the scenario registry's new canonical-test entry was corrected and checked separately.
- Baseline S2a `01fc7b0`: all nine selected vendor producer/order cases fail for missing
  requests or service ordering; the exact same flag-off control passes. No paid calls.
- First-head review regressions: six owner-boundary cases fail on `9f25a9162` (slow trial contenders,
  vendor error text, persistence cleanup, and hidden config reads); three real-connector cases fail
  because rejected/cancelled admission leaks the connected socket. All pass with the fixes.
- Review-fix runtime/structure/producer/transport suite: 873 passed. CI-failure parity/structure/mirror
  checks and real transport tests: 200 passed. Python 3.10.19 isolated producer/transport run: 132 passed,
  including Stop during timeout teardown; cancellation does not rely on Python 3.11 Task APIs.
- Changed Python files pass Ruff. CI owns the full suite; no runtime restart/deployment or
  live-vendor/manual UI acceptance is included in this dormant change.

## S2b-1 contract version (historical)

At start, 2026-10-21: `git tag --contains 01fc7b0` returned none; latest RC release
was `gh-v3.2.2rc13`, before S2a. Recheck before first push and close-out. Use v19 while
unshipped; open v20 if a release has carried S2a in the meantime.

Pre-push recheck, 2026-10-21: no tag contains `01fc7b0`. The new `v3.2.2` release
(published 03:00:15 UTC) points to `399fc34c1`, before S2a. Therefore this change
extends the still-unshipped v19 rather than opening v20.

## Known by design

The design's §12 governs downgrade loss, official endpoints only, no paid checks, no page
content, vendor result quality and existing native/model-executor behavior. No UI or public
docs changes are part of this producer PR; S4 is the consumer and S6 owns disclosure.
