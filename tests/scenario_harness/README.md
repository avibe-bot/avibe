# Scenario Harness

This package holds reusable test-side primitives for capability-level scenario tests.

Rules:

- keep these helpers lightweight
- prefer service-boundary simulation over full E2E machinery
- only promote a fake or helper here after at least one real scenario proves the reuse
- keep provider-specific parsing logic out of this layer

Current contents:

- `core.py`
  Generic scenario-harness primitives: event probe, fake IM client, base controller, fake process, base harness, runner, expectation helpers
- `auth_setup.py`
  Auth/setup-specific harness built on top of the generic layer
- `message_delivery.py`
  Result/scheduled-delivery harness built on top of the generic layer
- `memory_repair.py`
  Public-ASGI Memory Repair harness with a hermetic internal runtime boundary

Recommended layering:

1. `core.py`
   The reusable layer every capability can share.
2. `<capability>.py`
   Capability-specific harness or fixtures.
3. `tests/scenarios/<capability>/test_<capability>_scenarios.py`
   The scenario transcript itself.
