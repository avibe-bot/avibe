# Desktop Runtime startup reuse

## Change contract

When the macOS/Desktop shell is reopened while the Runtime it previously started
is still healthy, bootstrap must adopt that Runtime before preparing the bundled
Runtime tree. A successful warm reopen must therefore avoid Runtime extraction,
shape validation, endpoint CLI startup, and login-shell lookup. If the cached
endpoint is absent, stale, or not ready, the existing prepare/query/start path
remains authoritative and refreshes the endpoint before launch.

## Root cause

Desktop Runtime processes intentionally outlive the shell window, but bootstrap
resolved the bundled launcher by preparing the private Runtime before it probed
whether the existing service was healthy. A contaminated legacy install could
therefore pay a full ~559 MB repair on every shell open even though the service
was already serving. The current Runtime/backend separation prevents new writes,
but it cannot make an already-running service cheap to rediscover.

## Solution

- Persist the last validated loopback endpoint as a bounded, atomic, best-effort
  cache beside the private Runtime data.
- Resolve the bundled launcher from its signed manifest only; defer private tree
  preparation until an endpoint cache miss or a start/lifecycle operation needs
  the executable.
- Classify the cached endpoint with the current bundle Runtime identity. Adopt a
  healthy matching Runtime immediately. On non-adoption, refresh the endpoint
  through the prepared Runtime before starting so a changed setup host/port
  cannot leave bootstrap probing a stale cached origin.
- Keep endpoint cache failures non-fatal and remove the cache with private
  Runtime removal.

## Validation

- Runtime-host unit coverage proves a cached private endpoint does not prepare a
  missing Runtime tree.
- Run Rust formatting and focused runtime-host tests; local Rust execution may be
  blocked by an unaccepted Xcode license on the developer machine.
- Run the Desktop packaging/build checks required by CI before delivery; do not
  restart the live Avibe service or Desktop app as part of verification.
