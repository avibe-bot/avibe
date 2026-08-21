# Show Page API Request Timeout

## Background

Show Runtime currently gives every proxied HTTP request the same 30-second
transport timeout. That is appropriate for page assets, but it turns a healthy,
slow synchronous Show Page API handler into the same 503 response used when the
Runtime itself is unavailable.

PR #1634 changes Show Runtime availability and recovery reporting in the same
core and UI proxy files. This change remains based on `origin/master` and adds a
narrow per-call timeout contract so the two changes can be reconciled without
adopting #1634's uncommitted worktree state.

## Goal

- Give Show Page API handlers a 90-second default deadline.
- Expose the deadline as `runtime.show_page_api_timeout_seconds` through the
  existing V2Config and `GET`/`POST /api/config` contract.
- Return HTTP 504 with `show_runtime_request_timeout` for an expired handler,
  while preserving HTTP 503 with `show_runtime_unavailable` for other Runtime
  failures.
- Preserve the existing 30-second behavior for ordinary page assets, shared
  Runtime assets, prewarming, access control, and header filtering.

## Solution

The UI proxy resolves the V2Config value when it forwards a `/api` request and
passes it explicitly to `ShowRuntimeManager.request`. The manager keeps 30
seconds as its default, so every existing caller is unchanged. Config writes
therefore affect subsequent API requests without restarting Avibe.

The response boundary classifies `httpx.ReadTimeout` before the generic
Runtime-unavailable path. Connection and other transport failures still mean
the Runtime is unavailable. This keeps the machine contract stable without
coupling V2Config into the Runtime manager or adding an environment-only source
of truth.

## Validation

- V2Config default, validation, persistence, projection, and partial-save tests
- Runtime transport timeout tests for the 30-second default and per-call override
- Private and public Show Page API proxy tests for the 90-second default,
  immediate config updates, distinct timeout errors, access behavior, and
  security-header filtering

Residual manual validation is one real handler that completes after 30 seconds
and before 90 seconds through both private and public URLs.
