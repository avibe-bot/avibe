# Setup Backend Runtime Reconciliation

## Background

The setup wizard persists backend enablement after its provider modal closes.
The modal intentionally defers backend restart because the controller may not be
running yet. When a controller is already running, however, `POST /api/config`
updates the V2 config and built-in Agent records without updating the
controller's in-memory backend registry. A newly enabled Codex Agent can then
route to a backend that is absent until the whole service restarts.

The same configuration/runtime split can affect OpenCode registration and
Claude runtime fields, even though Claude is registered unconditionally.

## Goal

Make a successful backend runtime config save authoritative for both persisted
state and any live controller, without requiring a full service restart.

## Design

1. Compare the previous and saved V2 configs through `to_app_config`, so the
   change detector follows the exact runtime projection consumed by backend
   adapters.
2. Send the changed backend IDs from the UI process to a new controller-only
   Unix-socket endpoint. The payload contract is:

   ```json
   {"backends": ["opencode", "claude", "codex"]}
   ```

3. The controller validates and deduplicates those IDs, then sends each through
   the existing `BackendRestartCoordinator`. That path drains active work and
   registers, unregisters, or refreshes the backend from the saved config.
4. If no controller is running, keep the save successful and report that the
   config will apply on the next start. If a service process is running but its
   internal socket cannot reconcile, schedule the existing service restart
   fallback rather than leaving a live process stale.

No credentials cross the internal boundary. The UI process produces only
backend IDs; the controller loads the persisted config itself.

## Validation

- [x] Unit: runtime change detection for Codex, OpenCode, and Claude
- [x] Contract: internal client/server request and response shape
- [x] Scenario `AUTH-SETUP-902`: first-run Codex enablement hot-registers before
      the first Agent turn
- [x] Focused Python tests, Ruff, and UI build
