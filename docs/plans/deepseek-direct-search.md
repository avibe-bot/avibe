# DeepSeek direct search and fetch

Status: **proposal**, 2026-10-19. DeepSeek's standalone Search/Fetch HTTP APIs are model-independent; Avibe exposes
them through the existing hosted-tool surface instead of adding a second search entry point.

## Shape

- `web_search` keeps one user-facing name and schema. Its executor chain now includes a **direct** candidate for an
  eligible `deepseek` API-key Source: `POST https://api.deepseek.com/v1/tools/search`, no model call.
- `web_fetch` is a new hosted tool with one schema, `{url}`, and the same direct candidate:
  `POST https://api.deepseek.com/v1/tools/fetch`. Every vendor model executor is unsupported for it, so DeepSeek is
  its only execution path today.
- The Tools page shows DeepSeek as `DeepSeek Search API` in the existing `web_search` and `web_fetch` cards. The user
  can order it against model executors; the turn freezes the order as it does for any executor.

## Credential

The DeepSeek Source's engine credential is not exposed to a non-model HTTP call. The direct executor reads
`DEEPSEEK_API_KEY` from Avibe Vault through `vibe vault fetch` (`--no-approval-wait`), so the key stays at egress and
the Source must be an API-key Source of vendor `deepseek` on the hub. A missing secret or a refused host is a failed
record with the provider's reason; it does not silently fall back to a model executor.

## Record

- Search maps `title`, `url`, `snippet`, and `date` (as `page_age`) from the API's `results`.
- Fetch maps `title` and the Markdown `content` into one source, so the model receives the page text through the
  canonical `record_text` path.
- `execution` is `mode: "direct"`, `executor_origin.provider: "deepseek"`, `executor_origin.api` is the Source's
  protocol (a valid `Origin` value), and `executor_origin.model` is the API label. Cost is the overlay list price,
  $0.75 per 1,000 successful requests.

## Why not a vendor model executor

A model executor would require a DeepSeek model call and would bill model tokens in addition to the search request.
The standalone API is cheaper, independent of the model serving the turn, and is the user's requested path. The
direct executor does not change the engine's Source health or the turn's model hop.

## Limits

- Search `query` is the canonical one-field argument; Fetch `url` is validated as HTTP/HTTPS, at most 8192 characters.
- The direct call has the chain's 180 s deadline. Search results are capped at 10 sources and each snippet is bounded
  by the existing record rules; Fetch content is capped at 20,000 characters in the record.
- The direct call is metered as a list-price attempt on the DeepSeek Source. A failed or empty result is not billed by
  DeepSeek, but the local cost line still reports the list price for the attempt.

## Tests

`tests/test_model_hub_direct_search.py` covers the candidate, Vault command, record shaping, empty Fetch and a Vault
failure. The hosted config/resolution/executor suites cover the new tool in the catalog, turn freeze, executor order
and status schema. `tests/test_vibey_agent.py` covers the Vibey tool path and the existing hosted records.
