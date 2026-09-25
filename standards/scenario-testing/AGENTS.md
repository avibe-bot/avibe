# Scenario Testing Standard

This directory is the portable entrypoint for capability-first,
scenario-driven testing. It adds a reusable middle layer between focused unit
tests and full end-to-end checks.

## When to use it

Use this standard when designing or refactoring testing architecture,
introducing a scenario harness, defining reusable scenario metadata, or turning
recurring manual regression into automated coverage.

Start with:

- `ADOPTION.md` when adopting the standard in a new repository
- `tests/scenarios/INDEX.yaml` when working in an already-adopted project
- the target capability's `catalog.yaml`, `observations.yaml`, and scenario tests

Read `STANDARD.md` for methodology and `WORKFLOW.md` for day-to-day delivery
rules. The project-specific implementation remains under the project's
`tests/scenarios/` and `tests/scenario_harness/` directories.

## Canonical project layout

- `tests/scenarios/INDEX.yaml` — capability index
- `tests/scenarios/<capability>/catalog.yaml` — stable scenario IDs
- `tests/scenarios/<capability>/observations.yaml` — dependency observations
- `tests/scenarios/<capability>/test_*.py` — executable scenario evidence
- `tests/scenario_harness/` — shared harness primitives

Scenario work starts from user-visible capabilities, uses stable IDs, prefers
shared harness primitives, and keeps simulations deterministic. It supplements
unit and E2E tests; it does not replace either or require every real-world
check to become automated.

When changing this standard, update project guidance to point here instead of
copying the standard into another `AGENTS.md`.
