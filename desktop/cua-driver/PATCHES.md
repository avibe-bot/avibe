# Pinned Cua Driver Patches

Avibe consumes the MIT-licensed `cua-driver-rs-v0.31.0` source at commit
`5272e492d61b96caf08e3bf434d91126c1f3dccc`. The release binary is not used
for this variant. The package workflow downloads the pinned source archive,
verifies its SHA-256, applies the patch recorded in `sources.json`, builds the
requested macOS target, and records the resulting provenance.

## `avibe-raw-single-click-v1`

Patch: `patches/0001-macos-raw-single-click.patch`

This patch adds the opt-in `click.click_mode` selector:

- omitted or `"auto"` preserves the existing AX-first `count=1` behavior;
- `"raw"` is accepted only for an exact `pid` and `window_id`, window pixel
  coordinates, background left-click delivery, `action` `press` or `click`,
  `count=1`, no modifiers, and no element or portable target;
- `"raw"` bypasses the macOS pixel-path AX hit-test/`AXPress` optimization and
  uses the existing routed CGEvent path;
- unknown values and invalid combinations return structured
  `invalid_arguments`.

The patch also adds the test-owned AppKit hollow-AX target and its ignored
background acceptance harness. The target reports AX presses separately from
native `mouseDown`/`mouseUp` delivery, so the acceptance can prove one raw
mouse pair and unchanged default behavior without relying on the tool result
alone.

For embedded serve mode, the patch also makes parent-liveness shutdown
terminal: stdin EOF removes only the socket bound by that process, removes its
PID file, aborts accepted connection tasks, and drops the Tokio runtime without
waiting for non-cancellable platform work. After that cleanup,
`std::process::exit(0)` terminates the helper even when the macOS cursor
overlay owns the main AppKit run loop. Its shutdown diagnostic is best-effort
because parent death also closes the shell-owned stderr pipe. The terminal path
leaves process globals intact because abandoned tasks may still reference them
until process exit. Explicit daemon shutdown keeps the existing graceful drain
and global teardown. The default process test keeps the cursor overlay disabled
so ordinary `cargo test` runs remain hermetic; the overlay-enabled macOS variant
is ignored and reserved for an explicit logged-in GUI acceptance run.

The upstream investigation found no consumable maintainer-accepted contract
with these semantics. The relevant discussions are:

- [PR #3922](https://github.com/trycua/cua/pull/3922)
- [PR #3539](https://github.com/trycua/cua/pull/3539)
- [PR #4632](https://github.com/trycua/cua/pull/4632)
- [PR #4208](https://github.com/trycua/cua/pull/4208)
- [Issue #4350](https://github.com/trycua/cua/issues/4350)
- [Issue #2874](https://github.com/trycua/cua/issues/2874)

The exact patch SHA-256, source archive SHA-256, tool snapshot SHA-256,
contract, and upgrade plan are duplicated in `sources.json`. The release
provenance uses schema 2 and includes those hashes plus the prepared binary
hash and post-signing packaged hash.

When upstream exposes both an opt-in raw `count=1` macOS click selector with
the same default-compatible contract and terminal embedded parent-liveness
cleanup, remove this patch and pin the upstream release. Otherwise, rebase the
patch onto the next pinned source commit, rerun the fixture and lifecycle
acceptance, refresh the source and snapshot hashes, and review the Avibe
forwarding admission again.

Foreground delivery is the unpatched driver's own `delivery_mode:
"foreground"` rung, and window activation is its `bring_to_front` tool. Both
are forwarded unchanged since #3050; neither is an implicit fallback for this
patch, whose raw selector stays background-only.
