# Computer Use: foreground input, window activation, and observable readiness

Issue: [#3050](https://github.com/avibe-bot/avibe/issues/3050). Status:
implemented by the PR that references this record.

## Background

The Computer Use Skill named QQ, WeChat, and Electron in a universal startup
rule with a fixed wait of about 30 seconds. The Controller refused every
`delivery_mode: "foreground"` request and a set of shortcuts (`⌘L`, `⌘⇧G`,
`⌘1`–`⌘9`, `⌘[`, `⌘]`) solely because they change focus, and the managed tool
surface had no explicit window activation. Several documents described IM
`require_bind` as part of Computer Use enforcement, although no Computer Use
code path consults it.

The bounded foreground design of #2607 (consent, leases, verified restoration)
was reverted by #2674. The owner decision on #3050 replaces it: foreground
input and activation are ordinary operations that need no per-action consent.

## Change contract

Behavior:

- Readiness is observable state, not elapsed time: the target window exists,
  its state is stable across observations, and its screenshot or accessibility
  tree is usable. Operation timeouts are unchanged.
- The Controller forwards `delivery_mode: "foreground"` and focus-changing key
  chords like any other input. The pinned driver performs the bounded
  front-act-restore rung and serializes it on its process-wide desktop lane.
- `vibe computer activate` and `WindowHandle.activate()` call the pinned
  driver's `bring_to_front` for one exact `pid` + `window_id`. Activation is
  persistent: Avibe does not restore the previous frontmost window, because the
  caller asked for this window to stay in front.
- The Skill states restrictions and obligations only. It does not list ordinary
  permitted operations; API examples show how to call the SDK.
- `require_bind` is described only as the generic IM ingress policy it is.

Unchanged safeguards:

- Exact window targets, observe-first for input, stale-handle refusal, and
  target-scoped locks. Activation takes the same exact-window lock as other
  window mutations; it is not input and needs no prior observation.
- Irreversible external effects: verify, `avibe.confirm`, verify again, act;
  an unavailable or refused confirmation stops the step.
- Driver managed policy, snapshot digest checks, native permission and
  lifecycle checks, and truthful effect reporting.
- Generic IM `require_bind` behavior in the shared auth pipeline.

Boundaries touched:

| Boundary | Change |
| --- | --- |
| `desktop/cua-driver/policy.yaml`, `tools-v0.31.0.json` | Add the pinned driver's `bring_to_front`; new snapshot digest in `sources.json`, `core/computer_use.py`, and the Runtime host |
| `core/computer_use.py` | Accept the previous approved snapshot so a newer Runtime keeps Computer Use available, without activation, beside an older desktop shell |
| `core/computer_server.py` | Remove the foreground and focus-shortcut refusals and the dead consent code; treat `bring_to_front` as an exact-window mutation |
| `core/computer_cli.py`, `vibe/cli.py` | `activate` command; drop error keys that can no longer occur |
| `core/computer_script_handles.py`, `vibe/computer_sdk/` | Window handles may call `bring_to_front`; `activate()`; keep the released `ForegroundConsentRequired` export as a deprecated, never-thrown class |
| Skill and docs | Generic readiness; foreground and activation API; no Computer Use `require_bind` guidance |

Non-goals:

- No consent prompt, lease, restoration verification, or owner setting for
  foreground use.
- No automatic escalation from background to foreground delivery; background
  remains the default.
- No change to generic IM binding, the driver patch, or the driver's
  permission mode.
- These controls are cooperative. They do not sandbox same-user local
  processes, which can reach the driver socket directly.

## Compatibility

Product builds ship the shell and its embedded Runtime together. An adopted
Runtime can differ from the shell:

- Newer Runtime, older shell: the Runtime accepts the previous 28-tool
  snapshot. Computer Use stays available; `activate` reports `unknown_tool`.
- Older Runtime, newer shell: the released Runtime pins only its own digest
  and reports `snapshot_invalid` until it is updated or restarted from the
  newer bundle. This matches every earlier snapshot change.

## Validation

- Controller tests: foreground input and focus-changing chords reach the
  upstream call unchanged; activation forwards with an exact-window lock and
  without observation; missing targets and stale handles are refused before
  dispatch; raw-click validation and background defaults are unchanged.
- CLI and SDK tests: `activate` and `WindowHandle.activate()` reach the
  Controller with the exact descriptor; foreground options pass through.
- Snapshot tests: policy equals snapshot; both approved digests load with their
  exact tool sets; any other digest is refused.
- Skill test: readiness is observable-state based, with no fixed delay,
  application-specific startup rule, or `require_bind` guidance.
- Generic `require_bind` coverage in `tests/test_auth_pipeline.py` is
  unchanged.
- Native evidence: the snapshot entry is generated by the pinned, patched
  driver binary's own `tools/list`. Live macOS GUI acceptance of foreground
  delivery and activation is outside this change's authorization.
