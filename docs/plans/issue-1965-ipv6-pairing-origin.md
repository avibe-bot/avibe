# IPv6 pairing origin family consistency

## Intent and boundary

Fix #1965: when Cloud widens a non-loopback IPv6 UI bind to `::`, every pairing-origin consumer must receive an IPv6 loopback URL that can reach that listener. The public signatures and configuration schema remain unchanged.

## Evidence

Audited `master@e8a7cb4a94e93b5413a977d70816b01560e3d156`: both `fd00::1` and `2001:db8::5` yield bind `::`, origin `127.0.0.1`. A disposable asyncio server confirmed IPv4 refusal and IPv6 loopback success. `_origin_host_for_pairing` owns the projection; inventory its direct and indirect consumers before changing it.

## Acceptance invariants

- With Cloud enabled, bind and pairing origin agree on reachable address family; non-loopback IPv6 hosts project to bracketed IPv6 loopback in URLs.
- Existing IPv4, loopback, wildcard, bracket normalization and localhost-resolution behavior is preserved unless a contradiction is demonstrated.
- All existing consumers of this shared projection inherit the correction, including tunnel configuration, health, expected origin and model service. Keep probe failures observable.
- Hermetic tests include a real disposable socket/HTTP connection through the produced origin to the produced bind, with clear platform capability handling; no live pairing or external user state.
- The relevant existing unit and auth/setup scenario tests pass; Ruff passes on all changed Python files. Update scenario metadata if this flow has a catalog case.

## Scope

Allowed: `vibe/remote_access.py`, `vibe/runtime.py` only if needed for shared family ownership, `tests/test_remote_access_vibe_cloud.py`, a focused new test module if isolation improves it, `tests/scenarios/auth_setup/catalog.yaml`, `tests/scenarios/auth_setup/test_auth_setup_scenarios.py`, and this plan. Read other consumers but ask the orchestrator before editing another production module. No UI files, schema change, service restart, deployment, dependency addition or broad refactor.

## Delivery

Smallest complete fix, focused validation, non-draft PR targeting `master`, `Fixes #1965`, exact-head Codex-bot pass, full expected CI and zero unresolved threads. Do not merge or close the issue before integration. Update this plan with verified evidence and residual manual checks.

## Implementation and verified evidence

- Base verified by explicit master fetch: `e8a7cb4a94e93b5413a977d70816b01560e3d156`.
- The shared projection now maps every parsed non-loopback IPv6 address to
  `[::1]`; the existing loopback-preservation and localhost-resolution branches
  are unchanged. No runtime bind, configuration, dependency or API change is needed.
- Consumer inventory at that base: `_origin_host_for_pairing` feeds
  `origin_service_for_pairing`, which feeds the pairing redeem payload (the
  backend configures cloudflared), `_local_ui_healthy`,
  `runtime_status_payload.expected_origin_service`, and
  `model_service._model_service_ui_origins`. All inherit the same correction.
- The audit's seven existing origin assertions describe IPv4, localhost,
  loopback or wildcard cases; none needs weakening or a changed expectation.
  Added family coverage includes private/global, bracketed, scoped and mapped
  IPv6 literals, plus unchanged IPv4 and hostname behavior.
- `AUTH-SETUP-907` starts a disposable aiohttp/asyncio HTTP listener on the
  actual effective bind and connects through the produced pairing and model
  service origins. It checks the real health consumer and reported origin,
  then verifies that a closed listener reports unhealthy. IPv6 cases require
  `IPV6_V6ONLY=1`, so dual-stack acceptance cannot hide a wrong IPv4 origin.
  Separate capability probing skips only unavailable OS IPv6 loopback support;
  errors in the generated bind or origin fail the test.
- Before the fix, focused coverage produced 10 failures and 11 passes,
  including connection failures in all three non-loopback IPv6 socket cases.
  After the fix, the full remote-access, auth/setup and model-service suites
  passed: **425 tests and 12 subtests**, with no skips.
- Mutation check: restoring only the old conditional made all three IPv6 HTTP
  cases fail with connection errors while the IPv4 control passed. Restoring
  the fix passed all four. Ruff passed on all three changed Python files.
- Hermetic boundaries: the existing autouse fixture redirects home, config,
  credential and SQLite state into test-owned directories. Pairing backend and
  connector lifecycle are stubbed; HTTP uses ephemeral local ports with proxy
  bypass. No live pairing, service restart or deployment was performed.

## Residual integration verification

The orchestrator may verify a real cloudflared tunnel after separately authorized
integration: pair an IPv6-configured local regression instance, confirm the
backend ingress uses `http://[::1]:<effective-port>`, then load the remote UI and
check health. This lane proves the local socket boundary and pairing payload;
it does not exercise external Cloud provisioning. Existing already-paired
backend ingress is not rewritten by this projection-only change.

Scope stayed within the assigned production module, existing tests/catalog and
this plan. No cross-lane interfaces or files changed.
