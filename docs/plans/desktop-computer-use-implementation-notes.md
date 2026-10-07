# macOS computer use Phase 1 implementation notes

This note records implementation and acceptance facts without changing the
approved behavior tables in
[`desktop-computer-use.md`](desktop-computer-use.md). Evidence is from the
isolated lane owned by Agent Session `ses9qeb46suvv`.

## First installed artifact

The first installed artifact was built from
`89a0634a0454f67df58067f5f49097d23efb20b3` and held byte-for-byte fixed at:

- app: `/Users/max/Applications/Avibe CUA Acceptance.app`
- bundle identifier: `bot.avibe.desktop.cua.acceptance`
- outer cdhash: `1a0d31caf13f8d82789a9bece36022ca682a4cf5`
- nested driver cdhash: `7939ebd23e7c44f54533c377a77216dd4764bfda`
- installed tree-manifest SHA-256:
  `bd55dd471beaf1d76dac4ba57a1d4358e86508cc5e69d88e880b934ee369e5ef`

It was launched against an isolated Runtime on `127.0.0.1:15177`. The
acceptance bundle's app-data directory, daemon socket, Runtime home, backend
configuration, and CLI homes were separate from the owner's installed Avibe
and default `~/.avibe` state.

## Acceptance incidents and corrections

### Missing Workbench assets

The first isolated Runtime launch served `/ready` and desktop APIs but returned
404 JSON for `/`, `/workbench`, and `/index.html`. That shape was rejected:
an installed desktop artifact must show the actual Workbench. The isolated UI
bundle was built and installed into the test Runtime, after which the page and
its JavaScript and CSS assets returned 200. The product acceptance checks the
page and its assets, not only the API.

### Activation burst

An acceptance probe repeatedly activated the app while looking for the native
menu item. It advanced the daemon generation to 18. Timestamped reads then
showed generation 18 steady while the feature remained
`needs_permission/screen_recording`; there was no continuing Runtime-adoption
or menu-refresh churn. The exact probe and timestamps are retained under:

`/tmp/avibe-cua-phase1-acceptance-ses9qeb46suvv-be8e909/evidence-20261007/`

The product response is a monotonic stale-preflight throttle: a burst of 18
activations over 3 s permits one fallback, no second fallback before 5 s, and
one later activation may try again. A silent preflight that sees both grants
starts immediately and is not throttled. This adds no background permission
retry or prompt.

### Screen Recording registration failure

Two fixed candidates failed to appear automatically in **System Settings →
Privacy & Security → Screen & System Audio Recording**.

The `89a0634a` acceptance artifact called
`CGRequestScreenCaptureAccess` and `captureImageInRect`, but the permission
path was not dispatched to the AppKit main thread and ignored the asynchronous
capture result. The owner manually dragged that app into the pane so the
vertical-slice investigation could continue. That was an owner-assisted
workaround and is not acceptance of the product request path.

Candidate 2 was built from
`7ddb10741411b290469e39d6a7922a987755405b` and held byte-for-byte fixed at:

- app: `/Users/max/Applications/Avibe CUA Candidate 2.app`
- bundle identifier: `bot.avibe.desktop.cua.candidate2`
- outer cdhash: `5180ece1bc73874b19e5378eec73600c845360ed`
- installed tree-manifest SHA-256:
  `843b0aa5bef45d89319bb370f972b79524ae408535913a3beda61420ad24c1a5`

Candidate 2 moved Accessibility, `CGRequestScreenCaptureAccess`, and the
capture probe onto the AppKit main thread and recorded the asynchronous
result. The capture callback reported an image, but the Settings row was still
absent. Unified logs show that TCC received the explicit non-preflight request
from the correct shell PID and bundle, returned `authValue=0` /
`authReason=5`, and took `DB Action:None`. The following
`captureImageInRect` path only produced preflight checks and no persisted
ScreenCapture row. An image callback therefore does not establish either a
grant or registration.

The historical Phase 0 comparison used the same AX request followed by
`CGRequestScreenCaptureAccess`. Its persistent serve-only mode also failed to
register. The run that next entered ordinary `SCShareableContent` enumeration
and a filter-based capture did create the denied TCC record that exposed the
grant path. This is the smallest observed behavioral difference.

The next candidate therefore replaces `captureImageInRect` with ordinary
`SCShareableContent` enumeration followed by a display-filtered capture. Both
the source rectangle in the selected display's logical coordinate system and
the output are explicitly 1 x 1. Enumeration and capture share one 5 s
deadline; a denied enumeration ends with its concrete `NSError`, and a late
enumeration callback cannot start capture. The image is discarded, and no
window or application enumeration is logged or retained. This remains a
testable hypothesis until a new bundle identity appears in Settings before any
manual add or grant.

Primary diagnostics:

- `screen-registration-diagnostics.log`, SHA-256
  `0d929f482959283eb74b7cfe98f0f2a6d40a69ee3b4daeb93f5bb2b5d689a973`
- `capture-call-full.log`, SHA-256
  `63eacc4a4ab2048653104b15dcb636ebc239f32ebec58e6f00e36fff74b6935e`
- `candidate2-tcc-history.log`, SHA-256
  `723a65110073da6a9b9e7c749c8215c974dd572eccc3f826cb07adf29c027e2e`
- `phase0-historical-ExampleAgentHarness.swift`, SHA-256
  `fb7e7662bee1cc65647e6d85bd69f1b4c8feebc93eab367881a7d9cebca18e30`

### macOS Quit & Reopen and test isolation

macOS **Quit & Reopen** relaunched the fixed artifact without the lane's launch
environment. That shell adopted the owner's older Runtime on port 5123 and
correctly entered `needs_runtime/runtime_too_old`; it did not start a driver.
The lane stopped only that recorded shell PID and relaunched the same unchanged
artifact with the isolated origin and executable overrides. Before resuming
GUI work, the lane verified that the replacement shell connected only to
15177, `D` was `ready`, the driver was its child, and the owner's Runtime
process start times and `/ready` health were unchanged.

All later acceptance relaunches must use the recorded isolated command and
verify the actual Runtime origin before any GUI action. This is a test-harness
boundary and does not add a product setting.

## First real Claude tool-chain slice

The fixed `89a0634a` artifact completed this path:

`native toggle → shell daemon → D ready → Avibe computer MCP server → Claude Code → disposable background app`

Claude Code 2.1.282 used `claude-sonnet-5`, isolated HOME/XDG/Claude
directories, `--strict-mcp-config`, and one `avibe_computer` server exposing
exactly 28 tools. It observed a dedicated app window, set
`切片通过-2026` through an AX element token, pressed the app's save control,
observed the window again, read back `Saved 1 times: 切片通过-2026`, and ended
the named session. The target app wrote:

```json
{"save_count":1,"text":"切片通过-2026"}
```

Both observations delivered one PNG image block. The stable Avibe server folded
the upstream `structuredContent` into a trailing text block and preserved the
image block; it did not preserve a `structuredContent` field. The first click
attempt omitted `pid` and was rejected by the upstream schema before any GUI
side effect. The corrected click saved once.

Primary evidence:

- `claude-vertical-slice-summary.json`, SHA-256
  `15c168461c572500fb4c3ad76d80ae8b6fef1eea917f055de312a93b06279883`
- `claude-stream.jsonl`, SHA-256
  `4fcaa037382a91a61e7826f4a9424d733fc0e3b04499fa5b5b147880a1dc9484`
- `target-result.json`, SHA-256
  `64c7f5b4866f991e62994dbd9bc6c4cc77cee4a4928dccc61462c9e7d81735c5`

This is mechanism evidence with explicit limits:

- Screen Recording came from the manual-add workaround.
- The artifact predates the activation throttle and observable main-thread
  permission probe.
- `--strict-mcp-config` isolated the direct CLI probe. It does not prove the
  Runtime `SessionHandler` injection path or preservation of native user MCP
  configuration.
- The app may not have been idle. Before/after frontmost-app and pointer
  samples were unchanged, but they do not prove idle focus, pointer, or visible
  cursor behavior.
- The tool actions were AX `set_value` and AX press, not pixel-coordinate
  input.
- The run used a probe prompt. Final acceptance must use the product's injected
  prompt and treat tool errors by observing again before retrying.

## Remaining native acceptance

A separate fixed candidate with a new bundle identity must prove:

1. toggle-on automatically registers the app in the Screen Recording pane;
2. `bootstrap.log` records a main-thread request and bounded capture
   completion;
3. grant, parent-liveness, shell-death cleanup, idle focus/pointer/cursor, and
   Claude operation all use unchanged installed bytes;
4. the normal isolated Runtime session injects the computer server while
   preserving native user MCP configuration;
5. an ad-hoc update that changes the cdhash settles at
   `needs_permission` with a usable re-grant path, without `error`, repeated
   prompts, or retry churn.
