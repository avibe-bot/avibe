# Pinned Cua Driver Patches

Avibe consumes the MIT-licensed `cua-driver-rs-v0.31.0` source at commit
`5272e492d61b96caf08e3bf434d91126c1f3dccc`. The release binary is not used
for this variant. The package workflow downloads the pinned source archive,
verifies its SHA-256, applies the patch recorded in `sources.json`, builds the
requested macOS target, and records the resulting provenance.

## `avibe-raw-single-click-reopen-v2`

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

The patch also changes macOS `launch_app` for an already-running application: it
sends `kAEReopenApplication` (`aevt/rapp`) while preserving `activates=false`;
cold launches and explicit new-instance launches continue to send
`kAEOpenApplication` (`aevt/oapp`). Bundle-ID and name lookup share this event
selection. The URL handoff path remains unchanged.

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
contract, and upgrade plan are duplicated in `sources.json`. The reopen hunk is
tracked by upstream PR #6198 and is removed after a release containing that
contract; the raw-click and parent-liveness hunks have independent upgrade
conditions. The release
provenance uses schema 2 and includes those hashes plus the prepared binary
hash and post-signing packaged hash.

When an upstream release includes the running-app `rapp` launch contract,
remove only that hunk. Keep the raw-click and parent-liveness hunks until their
corresponding upstream contracts are released. Otherwise, rebase the single
patch onto the next pinned source commit, rerun the fixture and lifecycle
acceptance, refresh the source and snapshot hashes, and review the Avibe
forwarding admission again.

Foreground delivery is the unpatched driver's own `delivery_mode:
"foreground"` rung, and window activation is its `bring_to_front` tool. Both
are forwarded unchanged since #3050; neither is an implicit fallback for this
patch, whose raw selector stays background-only.

## Windows security and literal-launch supplement (not yet packaged)

Patch: `patches/0002-windows-security-boundary.patch`

Apply this supplement only to the pinned source **after the complete, unchanged
0001 patch**. Production `sources.json`, the Windows tool snapshot, and
preparation now select the ordered 0001 → 0002 chain; macOS preparation still
selects 0001 alone. The hosted Windows source-test step explicitly applies both
patches, records source/patch/test-binary hashes, and requires all 13 selected
native tests with JUnit no-skip/count evidence. Passing those fixtures is not
signed-installer or interactive GUI acceptance.

The supplement refuses elevated, above-Medium, UIAccess, or unreadable driver
tokens at startup/readmission, removes elevation workarounds, and makes protected
or unverifiable target refusals explicit. Windows `launch_app` accepts only an
absolute local fixed-drive `.exe` path, optional string-array arguments, and
`start_minimized` (besides existing internal session metadata). It creates a new
process directly, never attaches to an existing process or follows a shell,
association, name, AUMID, or derived-token fallback.

A single ordinary file open resolves the final OS path spelling, checks disk,
non-directory, `.exe`, and fixed-drive properties, then closes the file. The
exact final spelling is launched. An OS-final component ending in an ASCII
period or space is refused rather than stripping its verbatim prefix and
potentially launching a different existing path. This is a check of the final
string only, not an input dot/dotdot restriction or ancestor walk. Same-user
executable replacement is not a security boundary: there are no file-ID locks,
ancestor handles, reparse bans, lowercase identity transforms, or
case-sensitive-directory restrictions.

The target receives `CreateEnvironmentBlock` for the ordinary token with caller
inheritance disabled, an executable-parent cwd, no inherited handles, and
explicit target-only Job breakaway. Driver/helpers remain contained by their
owning shell. Application-internal handoff is not adopted: the response retains
the original created PID and reports whether it is still running. Ordinary app
metadata remains the existing `list_apps` responsibility; launch performs no
additional installed-app scan.

### Windows refusal transport

The Windows action wrapper normalizes the platform's known security refusals
before the common daemon output-schema gate. Text-only actuator errors previously
reached that gate without an error identity and received `tool_invocation_failed`.
The supplement now carries `protected_window`, `unverified_window`,
`desktop_unavailable`, or `administrator_runtime_refused` as structured codes,
retaining existing parameters and supplying a message. An elevated target and a
non-elevated higher-integrity target share `protected_window`, with distinct
`reason` values; an unavailable desktop does not acquire an invented cause.
Existing explicit error identities and successful results are not replaced.
This is diagnostic normalization, not a new admission check, retry, foreground
fallback, or claim about whether earlier action preparation ran.

The Python proxy retains code/parameters and emits the same bounded, sanitized
text in `message` and the existing `detail` field. The latter preserves existing
JavaScript SDK behavior without changing its error classes. Observation and
attachment permissions, target admission, launch ownership and default-off
behavior are unchanged.

The source-build PR runner additionally creates real elevated and ordinary EDIT
fixtures under its existing disposable account setup. It exercises the built
driver daemon, embedded MCP proxy, Python server and JavaScript SDK, checks the
exact protected-window code/parameters/message, and independently reads both
editors: the protected target must receive no input while the ordinary control
must contain the requested text. The shell state and loopback HTTP adapter are
test-owned; this is not shell readiness or signed-installer acceptance. Locked
desktop behavior remains outside that hosted proof. Packaging with an externally
provided binary keeps its existing metadata-only runner path.

This changes the Windows driver build input: the supplement SHA, both manifest
patch entries, and the snapshot provenance must move together. Tool definitions
and output schemas are unchanged; native MCP `tools/list` capture remains the
verification boundary. The source archive and complete macOS 0001 patch remain
pinned unchanged. A released Windows binary must be rebuilt from this chain;
Python-only updating does not repair a text-only refusal from an older driver.

### Known-by-design shared-source scope

The supplement changes three core source files: `protocol.rs`, `tool.rs`, and
new `windows_launch_ownership_tests.rs`. Their ownership additions are Windows
cfg-only. `ToolResult` carries a serde-skipped internal original process handle;
there is no JSON/wire/caller evidence field. Registration reads both PID and
creation FILETIME from that original handle, requires fresh fingerprint equality,
and retains the handle across the await and ownership mark. Missing, failed,
exited, or mismatched evidence cannot grant ownership. Existing fingerprint
reproof is retained; Windows termination additionally checks PID and creation
time on the same handle used for termination. There is no PID-only fallback.
Mac/Linux registration paths and the ownership ledger shape are unchanged.

Expected Windows tool-metadata differences from 0001 are `hotkey.description`,
`list_apps.description`, and `launch_app` description/schema descriptions plus
`idempotent=false`. Packaging must derive its snapshot from actual patched MCP
`tools/list`; `dump-docs` alone is insufficient. Refresh packaging provenance and
perform native/signed acceptance in the separate integration before activation.
