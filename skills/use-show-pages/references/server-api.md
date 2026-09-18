# Public server-to-server handlers

A public Show workspace can explicitly admit an exact POST handler without a
browser Origin or cookies. Use a separate minimal workspace for this purpose;
keep reports and other private pages private. This is an ingress capability,
not an executor, scheduler, or a promise that your handler authenticates safely.

Create `api/github-webhook.ts` with an exported `POST(request)` function, and
place this optional `.show-api.json` at the workspace root:

```json
{
  "schema_version": 1,
  "server_to_server": [
    {
      "path": "api/github-webhook",
      "method": "POST",
      "auth": "handler",
      "max_body_bytes": 1048576,
      "forward_headers": [
        "x-hub-signature-256",
        "x-github-event",
        "x-github-delivery"
      ]
    }
  ]
}
```

Only `POST /p/<current-share-id>/api/github-webhook` is admitted. Obtain the
actual share URL from `vibe show status`; publication requires the user's
corresponding authorization. The declaration does not publish the page.
Private, limited, offline, revoked, undeclared and non-POST paths keep their
existing protection. Removing the declaration revokes future admission.

Paths contain static ASCII letters, digits, `_`, `-` and `/` segments under
`api/`. They must name a real workspace-confined `.ts` file. Queries, encoded
aliases, dynamic paths, trailing slashes and index fallbacks are not admitted.
The optional manifest is limited to 16 KiB and 16 unique routes; invalid or
unsupported declarations disable this admission without preventing startup.

The handler receives original body bytes and content type plus up to 16
explicit metadata headers. Metadata names are ordinary `x-` names (maximum 64
characters); credential, proxy, internal protocol and hop-by-hop names are
forbidden, case-insensitively. Values are limited to 2048 bytes. Duplicate
headers, compressed bodies and requests exceeding the configured body limit
are rejected before handler execution. The hard body cap is 1 MiB; reading has
a 10-second limit and the complete ingress operation has a 30-second deadline.
Request headers have a 16 KiB / 64-field limit.

`auth: handler` assigns authentication and business authorization to your
trusted server code. Validate a sender's signature against original bytes
before parsing payload business data or producing inbox/dispatch effects.
GitHub's HMAC covers the body, not its delivery or event headers. Validate those
separately and coalesce signed replays. Cookies, Authorization, Origin, Referer,
annotation tokens and caller-supplied Runtime identity never reach the handler.
Avibe supplies its isolated `shared` Runtime context.

The response is a status receipt: 2xx returns `{"ok":true}`; 4xx/5xx returns
`{"error":"show_server_api_rejected"}` with the handler's status. Redirects,
invalid response encodings and oversized responses produce a generic 502.
Responses are no-store; handler bodies and response headers are not exposed.
Use 200 or 202 for receipt bodies (HTTP 204/205 have no response body).
Runtime responses are capped at 64 KiB and 16 KiB / 64 header fields.

An unavailable Runtime returns generic 503. Anonymous traffic never installs,
starts or repairs it: prewarm through the authorized Show/Runtime lifecycle
before commissioning. The runtime's package and handler must be available on
the instance; editing this file does not deploy or activate a package. Verify a
real sender delivery, durable inbox write and consumer receipt before declaring
an external integration live.
