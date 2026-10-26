# WF-2G gateway delivery

Status: 2026-10-26, WF-2G complete: #2962, #2968 and #2976 merged.
WF-2G kept `WEB_FETCH_ACTIVE=False`; WF-3 now prepares the coordinated activation
and public disclosure. The owner chose a combined reader/activation release
(see §8 of the owning plan). The sections below retain the split's history.
The behavior contract remains [web-fetch.md](web-fetch.md), especially §§3–6.
This plan narrows delivery units, not the activation/release scope.

## Delivery units and ownership

1. **Local engine and packaging:** PR #2962 merged. No activation.
2. **Chain and keyless services:** PR #2968 merged with the URL/DNS gate, local
   wiring, keyless Exa/Jina, ordered fallback, no-send/skipped contract,
   additive fetch usage attribution, and endpoint routing.
3. **Configured services:** PR #2976 merged from updated `master` after #2968,
   adding Tavily Extract, keyed Exa `/contents`, and DeepSeek Fetch.
   Both producer prerequisites are complete; WF-3 still requires review/CI and acceptance.

The gateway lane prepares exact-head review/CI evidence; the orchestrator owns
merging. No service restart, regression deployment, release or live paid probe is
part of this lane. Tests use test-owned state and loopback fixtures only.

## Convergence boundary (historical #2968 split)

The third reviewed head of #2968 again had multiple provider-mapping/settlement
findings, triggering the orchestrator's split guard. The configured transport,
content mapping, price/reported-cost mapping, and Source credential-health
settlement are removed from #2968, not merely hidden behind the global flag.
The chain and adapter independently reject configured fetch candidates before
admission. Existing catalog identities and saved user choices remain intact.

The pre-split implementation is preserved in commit
`7f29c924082251f4868804c37f5210e1e2a141f2`; it is a reference, **not** a reviewed
implementation to restore wholesale. In particular, its paid pre-status billing
uncertainty and admission-generation propagation are known defects. The follow-up
must not cherry-pick the entire chain or become a stacked PR.

## Remaining producer invariants

Prior tests checked mapped results without exercising the full admission →
response → settlement lifecycle. Boundary tests now own these invariants:

| Boundary | Required evidence |
| --- | --- |
| Activation boundary | Global flag rejects stale turns before local or configured transport admission. Configured providers became eligible only with #2976; WF-3 exercises defaults without flag-on test patches. |
| Admission | Every service submission re-gates only the original URL; a refusal stops forwarding but retains usage for earlier sent attempts. |
| Attempt ownership | One capped deadline starts before the shared Exa lock, includes subsequent transport work, and allows Jina fallback on expiry. |
| No-send | Disabled/unavailable/contended/over-budget candidates have only skipped facts, no usage, no capability success and no cooldown write. |
| Shared health | The actual sent callback carries the cooldown generation into settlement; stale generations cannot overwrite newer search/fetch state. Built-ins never reserve or write credential health. |
| Scope | Catalog MCP refusals precede generic errors; target failures/refusals never cool the service. Service failures and 429 do, honoring server advice. Jina's no-advice backoff starts at 60 seconds. |
| Protocol vs document | Top-level JSON-RPC errors, tool errors and missing/wrong-shaped result payloads are service faults unless a recognized target/refusal/rate signal overrides them. Valid JSON alone is not a successful envelope. Successful text documents (including JSON/scalars/error-looking prose) remain literal content. Search and fetch reuse the error-shaped MCP rate-limit classifier. |
| Truncation | An extractor's `content_overflow` survives normalization into the stored budget-truncation fact and model-facing marker, even when sanitization leaves fewer than 48,000 bytes. |
| Accounting | Every sent local/keyless attempt, including cancellation and failure, has exactly one zero-cost fetch usage row; mapping/settlement survive cancellation. |
| MCP transport | Search and fetch reuse one session; fetch sends `urls` and the explicit character bound, rejects missing controls/unknown required inputs before `tools/call`, and handles JSON/SSE. |

Primary tests: `tests/test_model_hub_fetch_chain.py` and the real loopback MCP
fixture in `tests/test_model_hub_keyless_exa.py`. Existing search-chain and fetch
reader tests cover shared cooldown/ledger/read compatibility. Local engine tests
remain the gate/transport/extraction owner; no live providers are test or CI inputs.

## Configured-provider follow-up acceptance (delivered in #2976)

The separate follow-up used an explicit table across provider × response phase ×
failure scope:

- request shapes: Tavily basic/Markdown `urls`, Exa `/contents` `ids` and bounded
  text, DeepSeek `url`; use only the catalog endpoint and opaque credential owner;
- distinguish valid target-empty/failed results from malformed/missing successful
  envelopes and service faults;
- sent + no response status means uncertain paid billing unless a documented
  provider-specific unbilled exception applies; received 200 with unrecoverable
  body stays uncertain, including DeepSeek;
- a proven successful billable result is priced even if normalization later
  fails; validate Exa reported cost without inventing a definitive zero;
- reserve and propagate credential-health and service-cooldown generations at
  actual admission; stale responses cannot damage recovered Source health;
- test transport cancellation, response-mapping cancellation, invalid endpoint,
  disabled/replaced Source, target refusal and real sent settlement through the
  chain boundary, rather than only the mapper;
- keep the same C-1 record/endpoint/usage contracts and keep activation off.

Use focused hermetic checks through `~/.local/bin/hd1-test`, then exact-head Codex
review and required CI for each PR. A passing smaller PR does not waive the other
producer PR or WF-3's activation gate.
