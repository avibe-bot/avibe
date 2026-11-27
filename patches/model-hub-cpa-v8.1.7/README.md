# Avibe-managed CPA v8.1.7 candidate

Upstream: `router-for-me/CLIProxyAPI` v8.1.7 (`47f18e4d82bd72f7d2b5ecf5ede93e4837a4625e`). The [candidate manifest](../model-hub-engine-candidates/model-hub-engine-v8.1.7-1/model-hub-engine-manifest.json) reuses the first seven patches from [v8.1.2](../model-hub-cpa-v8.1.2/README.md) in the same order, with identical bytes. It appends one adapter; registering this candidate does not change the bundled engine.

## Patch 8: preserve error comparability

Apply `status-error-comparability.patch` **after** `claude-retry-headers-on-error-responses.patch`. Patch 7 retains upstream Claude retry headers in the shared `statusErr` value. Embedding `http.Header` (a map) directly makes that value non-comparable: `errors.Is` cannot match the original cause by equality. Store a pointer to the retained headers instead; `Headers()` must return nil when absent and preserve the same headers when present.

On the patched CPA source tree, check both sides of the contract:

```sh
go test ./internal/runtime/executor -run '^(TestMissingAntigravityProjectIDError_PreservesCauseTOSViolationAndMetadata|TestClaudeExecutorFastHTTPDecodeErrorKeepsRetryHeaders)$' -count=1
```

At the next rebase, try omitting patch 8 if upstream provides equivalent header handling or patch 7 is removed or rewritten. Retire it only when both behaviors pass without the adapter. If retiring patch 7 also removes its regression test, carry the header check into qualification rather than counting a missing test as a pass.
