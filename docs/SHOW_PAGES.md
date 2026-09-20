# Show Pages

## Synchronous API Requests

Show Page API handlers under `/show/<session>/api/` and `/p/<share>/api/` have a
90-second request timeout by default. A handler that exceeds the deadline returns
HTTP 504 with this machine-readable body:

```json
{"error":"show_runtime_request_timeout"}
```

A Runtime startup, installation, or transport failure remains a different
condition and returns HTTP 503 with `show_runtime_unavailable`.

Owners can change the synchronous API deadline through the normal V2Config API:

```json
{
  "runtime": {
    "show_page_api_timeout_seconds": 120
  }
}
```

Send this as a partial `POST /api/config` payload using the same CSRF-protected
settings API as other global configuration. The value must be a finite number
greater than zero and applies to subsequent Show Page API requests without a
service restart. Ordinary Show Page assets keep their shorter Runtime transport
timeout.

Use synchronous handlers only when the work can reliably finish inside the
configured deadline. Longer operational work should start a background refresh,
return an accepted response, store a cached snapshot, and let the page poll that
snapshot. Raising the deadline should not replace a background job for work that
can take several minutes or survive a browser disconnect.
