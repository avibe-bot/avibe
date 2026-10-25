# WF-2V: dormant Vibey / Web fetch producer

Change contract, 2026-10-25. Implements the Vibey and UI portion of
[`web-fetch.md`](web-fetch.md) on the WF-1 reader contract. WF-2G independently
owns gateway policy, network transports, extraction, normalization, settlement
and packaging. No gateway contract change is needed by this lane.

## Invariants

- The production `WEB_FETCH_ACTIVE` remains false. No function registration or
  active Tools card appears. Historical C-1/activity readers remain usable.
- When activated, the first hop's tool set supplies a `{url}`-only function on
  every protocol, including Claude and Google's Chat gateway surface. A later
  hop cannot add a tool; execution uses the current hop's live-turn credentials.
- Runtime calls only `POST {base_url}/hosted-tools/web_fetch` with `arguments`.
  There is no target request, retry, pricing or budget-normalization in Vibey.
  A malformed gateway answer is a failed result, not another paid request.
- WF-1's deterministic quotation is both the model-facing ordinary content and
  the validated record's representation. Page instructions stay untrusted tool
  text. Persist/reload and cross-protocol replay do not fetch or settle again.
- Tools activation is server-owned: the GET omits the entire fetch row while
  off. UI controls write only the changed fetch field; resetting order sends
  null. Search/fetch switches, orders and retry endpoints remain independent.
- The shared activity projection owns forwarding disclosure: `policy_sources`
  lists non-skipped service attempts before an address-policy refusal. Only an
  empty list permits “not forwarded.” WF-2G guarantees every no-send service
  path records a skip (or no attempt), rather than a failed sent attempt. No raw
  page content enters IM summaries.
- Built-ins remain catalog identities, not credential Sources. Local has no
  global retry/key/removal. Exa shares its existing detail and cooldown. Jina
  has a switch, service retry and terms detail, without credential controls.

## Fixture acceptance

All tests use test-owned state and stub networking. No paid/live provider calls.

- `tests/test_vibey_web_fetch.py`: declared/off/unadvertised tables over four
  protocols; gateway results and live-turn headers; produced evidence replay.
- `tests/test_web_fetch_activity.py`: shared Web/IM projections, labels, precise
  small costs and policy disclosure; pins both generated JSON fixture files.
- `WebFetch.test.tsx`, `HostedFetchRow.test.tsx`, and the extended settings/retry
  tests: automatic/disabled orders, field-only saves, badges, inert text and
  independently serialized retry + read.
- `ui/e2e/web-fetch`: real Tools and chat components over those projections,
  Chinese-locale screenshots, external requests blocked. Run:

  ```sh
  cd ui
  WF2V_SCREENSHOT_DIR=/path/to/shots ~/.local/bin/hd1-test npx playwright test --config playwright.web-fetch.config.ts
  ```

The existing WF-1 downgrade tests remain the authority for released-shape
loading and lossy old config/usage rewrites. They are not lossless downgrade
claims. The ordinary text path remains compatible with #2920.

## Known by design

- No activation, release/tag, public-doc activation, deployment, service restart
  or local/remote regression update. WF-3 must first establish a reader release.
- The Tools read has no live local-transport state. Per orchestrator approval,
  Local detail shows the default Chrome 150 profile, fallback policy and limits;
  only actual call records show transport degradation and its reason.
- Expanded fetch cards show metadata, sizes, truncation and attempts first.
  Quoted page text is a separate opt-in rendered as plain text (no active HTML,
  remote images or executable links). A missing final URL stays “not reported.”
- The UI uses the existing model-hub tokens/primitives, order-write helper,
  Source create flow, shared-key detail and service-month usage projection.
  Fixture wrappers are not a new production route or feature toggle.
