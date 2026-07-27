# Custom Hostname Migration Compatibility Implementation Plan (Historical)

> **Status:** Superseded by the `org` integration. Do not execute the unchecked
> steps against the current tree; they are preserved as a record of the original
> feature-branch repair.

## Org Integration Resolution

`org` already contains the released `20260716_0030` and `20260721_0031`
migrations. Its linear chain uses `20260723_0032` for session visibility,
`20260725_0035` for resource ACLs, and `20260725_0038` as the schema head. The
follow-up recovery therefore retained that chain and added only the hermetic
released-`0030` upgrade regression from this plan.

**Goal:** Start the custom-hostname heartbeat branch against released Avibe state and complete remote authentication through `max.fileguard.io`.

**Architecture:** Linearize the branch's Alembic history after the released `20260716_0030` and `20260721_0031` revisions, then verify the real service-to-backend-to-browser flow. All automated migration probes use temporary databases; the default state is touched only by the managed service startup after focused tests pass.

**Tech Stack:** Python 3.11, Alembic, SQLite, pytest, Avibe CLI/API, Cloudflare tunnel, OIDC.

---

### Task 1: Reproduce Released-State Upgrade Failure

**Files:**
- Modify: `tests/test_sqlite_state_migration.py`

- [ ] Add `test_run_migrations_upgrades_released_0030_to_acl_head`, which creates a temporary database at `20260716_0030`, runs `run_migrations`, and asserts the final revision, the `silent` inbox predicate, and ACL tables.
- [ ] Run `pytest tests/test_sqlite_state_migration.py::test_run_migrations_upgrades_released_0030_to_acl_head -q`.
- [ ] Confirm it fails because `20260716_0030` cannot be resolved by the branch.

### Task 2: Linearize Alembic History

**Files:**
- Create: `storage/alembic/versions/20260716_0030_harness_input_index.py`
- Create: `storage/alembic/versions/20260721_0031_silent_marker_inbox_index.py`
- Rename: `storage/alembic/versions/20260720_0030_resource_access_policies.py` to `storage/alembic/versions/20260723_0032_resource_access_policies.py`
- Modify: `storage/migrations.py`
- Modify: `tests/test_sqlite_state_migration.py`

- [ ] Copy the two released migration definitions exactly from `origin/master`.
- [ ] Change the ACL migration revision to `20260723_0032` with `down_revision = "20260721_0031"`.
- [ ] Set `LATEST_SCHEMA_REVISION` and the test `HEAD_REVISION` to `20260723_0032`.
- [ ] Re-run the focused released-state upgrade test and confirm it passes.
- [ ] Run all of `tests/test_sqlite_state_migration.py` and the focused remote-access test files.
- [ ] Run Ruff on every changed Python file.

### Task 3: Apply and Verify the Runtime

**Files:**
- Runtime only; no additional source files expected.

- [ ] Reinstall the current branch into the `uv tool` environment.
- [ ] Start a managed restart with `AVIBE_ALLOW_DEV_STATE_MIGRATION=1` because this is an intentional source-checkout migration of default state.
- [ ] Poll the local `/status` endpoint until the main service has a live PID and remains healthy.
- [ ] Confirm logs show a successful runtime-status heartbeat and no Alembic startup error.
- [ ] Confirm the active-hostname snapshot includes `max.fileguard.io` through read-only runtime evidence.
- [ ] Request `https://max.fileguard.io/` without following redirects and confirm an OIDC 302 response.
- [ ] Complete the browser login flow and confirm the callback stays on `max.fileguard.io`.
