# WF-2V: dormant Vibey / Web fetch producer

Change contract, 2026-10-25. Implements the Vibey and UI portion of
[`web-fetch.md`](web-fetch.md) on the WF-1 reader contract. WF-2G independently
owns gateway policy, network transports, extraction, normalization, settlement
and packaging. No gateway contract change is needed by this lane.

Activation follow-up, 2026-10-26: WF-3 sets the shared flag to true after all
WF-2G producers merged, with default-on boundary acceptance and public en/zh
disclosure. At 01:56 Asia/Shanghai the owner chose to ship readers and activation
together, without a separate reader-first release. The invariants below describe
WF-2V's original dormant landing; the current contract is `web-fetch.md` §§7–8.

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

## WF-3 runtime acceptance and hosted-record audit

The activation's direct tool test did not reach transcript commit. Real Vibey
acceptance now exercises Anthropic wire → Agent loop → hosted tool/gateway HTTP
→ result frame → SQLite transcript/media registration → dispatcher/mirror →
activity API, then creates a fresh adapter for next-turn replay with fetch no
longer declared. The table covers Local, keyless Exa, Jina and policy refusal on
Web and Telegram, without live providers. A 200 must produce one committed
`WebFetchRecord`, one card/IM line and an ordinary final answer, not an interrupted
call; replay preserves that row and frame without another fetch or media entry.

Audit of production search/image-only record access:

| Owner | Type boundary |
| --- | --- |
| `storage/agent_transcript.py::_register_images` | Narrow to `HostedRecord` before enumerating images. `WebFetchRecord` is not a media container; previously this generic consumer raised before the result transaction committed. |
| `core/agent_core/agent/loop.py::_commit_result` | Image collection requires both `HostedRecord` and `image_generate`; a fetch can never enter it. |
| `core/agent_core/messages.py::_record_to_dict` | Dispatch fetch serialization before reading search sources/image fields. |
| `core/agent_core/ai/hosted.py::record_text` | Dispatch to the bounded fetch frame before search/image formatting. |
| `core/hosted_activity.py::proxy_activity` | Narrow fetch by type and project empty search sources/images plus the fetch record. Native activities accept only `HostedToolBlock`, whose record is validated as `HostedRecord`. |
| Vibey image tool, media sink and shared mirror | Image tool decoding validates the image tool's record; the media sink accepts image bytes, not generic records. The mirror reads the projected card's image list, empty for fetch. |

Primary regression: `tests/test_vibey_agent.py::test_web_fetch_survives_the_real_agent_commit_delivery_and_next_turn_replay`.
It reproduced the exact `WebFetchRecord` missing `images` failure before the fix.
Existing transcript media-sweep and image-generation tests retain positive image
registration/delivery coverage; no dummy image/search fields are added to fetch.
The orchestrator owns the separate local-Incus live-provider rerun before merge.

## Known by design

- No activation, release/tag, public-doc activation, deployment, service restart
  or local/remote regression update in the original WF-2V scope. WF-3's coordinated
  activation and owner-controlled acceptance are described above.
- The Tools read has no live local-transport state. Per orchestrator approval,
  Local detail shows the default Chrome 150 profile, fallback policy and limits;
  only actual call records show transport degradation and its reason.
- Expanded fetch cards show metadata, sizes, truncation and attempts first.
  Quoted page text is a separate opt-in rendered as plain text (no active HTML,
  remote images or executable links). A missing final URL stays “not reported.”
- The UI uses the existing model-hub tokens/primitives, order-write helper,
  Source create flow, shared-key detail and service-month usage projection.
  Fixture wrappers are not a new production route or feature toggle.
