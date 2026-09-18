# Regression and Acceptance

Use [the shared cloud acceptance instance](https://avibe-cloud-e2e-app.avibe.bot)
for Avibe regression and acceptance, including `回归测试`. This is the owner's
2026-09-18 decision and supersedes the former workstation-local Incus/Lima
default, including older delivery-skill suggestions to update local `master`.
Current environment selection belongs to this guide and the owner's decision;
older task plans and historical test records do not override it.

For example, after a messaging fix, verify the affected journey on this
instance and its intended test channels. Do not run `scripts/run_regression.sh`
to prepare that acceptance check: it manages local Incus, not this cloud target.

## Scope and State

The cloud target is a shared, persistent acceptance instance, not a disposable
test fixture. Preserve accumulated product configuration, credentials, pairing,
agent homes, Harness/session state, and Show workspaces unless a reset is
explicitly requested. Select scenarios that fit this shared-state boundary;
destructive/reset scenarios need separate authorization and an isolation plan.

This target selection does not itself request a deployment, restart, or test
run. It does not authorize access to other tenants, demos, production systems,
or arbitrary cloud instances. Do not use or recreate the retired local
Incus/Lima regression environment for routine acceptance, including when cloud
access is blocked.

Hermetic local unit, contract, and browser-fixture tests remain appropriate.
Keep their entire write path in test-owned state; do not point fixture or
reset-oriented suites at the shared acceptance instance just because it is the
default manual regression target. Never restart the local `vibe` service for
routine verification; it may host the agent doing the work.

## Access and Deployment

This repository does not establish an instance-specific login, credential, or
deployment procedure for the designated cloud target. A reachable URL alone
does not establish authenticated access, the running revision, or a deployment
mechanism. Use an established procedure for this exact instance when available;
otherwise report the missing access or deployment procedure as a blocker before
performing dependent work. Do not infer SSH access, Incus `--remote` arguments,
credentials, or a deployment command from the hostname.

For an authorized update, preserve existing state and verify the actual running
revision and service health before reporting deployment success. Deployment and
scenario verification are separate outcomes.

## Acceptance Evidence

Report the target URL, observed running version/commit (or that it is unknown),
scenarios exercised, actual results, and remaining blockers. Use the affected
Web or intended IM test surface, such as Slack, Discord, Feishu/Lark, or WeChat,
and verify the user-visible result through the relevant journey.

An HTTP 200 establishes reachability only. Health checks do not prove
end-to-end behavior; do not report a cloud E2E pass without running and observing
the relevant scenario.

## Scenario Metadata Navigation

Automated scripts and pytest complement manual acceptance. `docs/regression/`
is the human-readable entry layer; deterministic scenario metadata lives in:

1. `tests/scenarios/INDEX.yaml`
2. `tests/scenarios/<capability>/catalog.yaml`
3. `tests/scenarios/<capability>/observations.yaml`
4. `tests/scenarios/<capability>/test_*.py`

For multi-step auth/setup journeys, update
`tests/scenarios/auth_setup/catalog.yaml` and
`tests/scenarios/auth_setup/test_auth_setup_scenarios.py`.

## Explicit Local Incus Testing

The existing runner remains available to contributors who explicitly choose
local Incus testing. [The local Incus runbook](local-incus.md) preserves setup,
commands, lifecycle, metadata, and safety contracts. This is opt-in tooling,
not the default acceptance path; historical local test records remain valid
records of the environments used at the time.
