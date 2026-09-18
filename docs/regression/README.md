# Regression and Acceptance

Choose the regression target from the explicit task instruction or the
established developer, workspace, or task choice, then preserve that selection
through the workflow. This guide is the shared entry point for regression and
acceptance; an older task plan or historical test record does not silently
override the current selection.

If the task has no established target, resolve that ambiguity before a
dependent operation. A shared acceptance instance is persistent and may be in
use by one owner's workflow. It is not a common deployment sink for unrelated
branches or tasks: preserve its current state, coordinate updates with its
current use, and keep parallel work on its selected isolated target.

Selecting a target does not itself request deployment, restart, reset, or a
destructive test. The target owner must separately authorize mutation, and any
instance-specific access or deployment procedure must already be established.
Do not infer credentials or commands from a hostname. HTTP reachability and
service health alone do not prove an E2E result.

## Scope and State

For a shared persistent target, preserve accumulated product configuration,
credentials, pairing, agent homes, Harness/session state, and Show workspaces
unless a reset is explicitly requested. Select scenarios that fit this
shared-state boundary; destructive/reset scenarios need separate authorization
and an isolation plan. Do not access other tenants, demos, production systems,
or arbitrary cloud instances.

Hermetic local unit, contract, and browser-fixture tests remain appropriate.
Keep their entire write path in test-owned state; do not point fixture or
reset-oriented suites at the shared acceptance instance just because it is the
default manual regression target. Never restart the local `vibe` service for
routine verification; it may host the agent doing the work.

## Access and Deployment

This repository does not establish an instance-specific login, credential, or
deployment procedure for every possible target. A reachable URL alone does not
establish authenticated access, the running revision, or a deployment
mechanism. Use an established procedure for the selected target when available;
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

The existing runner remains available to contributors whose task or workspace
explicitly selects an existing local Incus environment. [The local Incus
runbook](local-incus.md) preserves setup, commands, lifecycle, metadata, and
safety contracts. It is a selected developer workflow, not a universal target
or a reason to recreate a retired environment; historical local test records
remain valid records of the environments used at the time.
