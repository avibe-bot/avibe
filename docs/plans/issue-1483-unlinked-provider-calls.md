# Issue 1483: Provider Calls Without Memcells

## Background

The Memory Processing Record lists memcells and reaches provider-call details only
through a selected `memcell_id`. A provider call recorded before a request is
rejected or otherwise stops can therefore exist in the private call log without
any product-visible path.

## Goal

Expose recent retained provider calls that are not attributable to a memcell in
the Web Processing Record. Preserve principal/project isolation, the existing
call projection's redaction and payload bounds, and an honest empty state when
recording or retained evidence is incomplete.

## Solution

Add a bounded read-only endpoint:

```text
GET /api/memory/log/unlinked?limit=20
```

Successful payload:

```json
{
  "status": "ok",
  "calls": [
    {
      "id": "call-id",
      "principal_id": "u-...",
      "project_id": "default",
      "started_at_ms": 1722816004000,
      "duration_ms": 12,
      "kind": "llm",
      "stage": "boundary",
      "model": "model",
      "status": "error",
      "error": null,
      "finish_reason": null,
      "prompt_tokens": null,
      "completion_tokens": null,
      "request": {},
      "response": null,
      "request_bytes": 2,
      "response_bytes": null,
      "dropped_before": 0
    }
  ],
  "truncated": false,
  "sections": {
    "everos": {"status": "available", "observed_at": "..."},
    "capture": {"status": "available", "observed_at": "..."},
    "calls": {"status": "available", "observed_at": "..."}
  },
  "recorder": {"state": "active", "reason": null},
  "retention": {"max_age_ms": 1209600000, "max_rows": 5000}
}
```

The reader resolves call scope only from an unambiguous `request_id` linkage to
capture queue or settlement evidence. It excludes request IDs attributable to a
valid singleton-owner memcell. Malformed scope, cross-scope request-ID
collisions, or missing attribution are omitted fail-closed.

The Web UI renders the calls with the existing provider-call component and
shows their principal/project scope. The empty state names unavailable or
degraded recording and always states the bounded retention policy, so absence
of retained rows is not presented as proof that no provider call occurred.

No call-log schema or retention behavior changes.

## Todo

- [x] Add scoped and admin reader projections with fixed result bounds.
- [x] Add runtime, internal socket, UI server, and frontend API contracts.
- [x] Add the Processing Record UI and English/Chinese copy.
- [x] Cover unlinked calls, scope isolation, recorder unavailability, redaction,
      payload bounds, and route validation.
- [x] Run focused Python/UI tests, Ruff, and the production UI build.
