# Signed Windows Computer Use acceptance (draft-only)

This harness belongs to one **draft PR that must never be merged**. It consumes
an already-published, signed Windows x64 installer, never builds or signs a
release, and never receives release/model credentials.

## Change contract

- Run only from this same-repository draft PR on GitHub-hosted `windows-latest`
  (Windows Server 2025 x64), with a 45-minute total runner budget.
- Download the installer and its release provenance. Verify SHA256SUMS and
  Windows Authenticode trust with a Certum issuer before executing it. Compare
  the installed `computer-use/cua-driver.exe` to both Stage 2 provenance hashes.
- Install, launch and uninstall under a newly created Users-only local account;
  never treat a filtered administrator token as a standard user.
- Preparation uses `gh-v3.2.7rc3` and asserts Computer Use remains unavailable.
  Preparation passing is not enabled-feature acceptance.
- Enabled acceptance must use the shipped CLI/JS path and the actual Settings
  enable action, without an LLM Turn. No model credentials are available.
- Keep only value-free JSON and per-case Markdown for seven days. No screenshots,
  clipboard capture, user text, credentials, raw process output or state trees.
- Yield to RC/stable/acceptance release workflows. A blocked or unexecuted case
  is NOT RUN, never PASS.

## Required enabled cases

1. Launch the pinned official portable Notepad++ Win32 editor by
   `computer.app({path})`, attach, type synthetic
   unsaved text, then independently exercise CU off, Desktop quit and Desktop
   crash. In each case the original app and its unsaved text survive while the
   driver and owned helpers exit.
2. Refuse bounded control of a verifiably elevated app with `protected_window`
   (metadata/attach alone is not prohibited), and refuse service from an elevated
   driver. Unrelated errors such as `unverified_window` are not a PASS.
3. A second local user cannot open the first user's pipe or acquire its lock.
4. Locked desktop and a true second interactive session: report NOT RUN if the
   hosted environment cannot demonstrate these honestly. A second account or
   noninteractive logon is not a second interactive session.

Product requires the real LLM Agent Turn to remain **NOT RUN** on hosted
runners, deferred to the owner's ten-minute real-machine check before Preview
can be removed. The activation RC must be explicitly selected only after its
activation PR has landed and its installer/Stage 2 provenance are published.

## Evidence and current preparation

The first full rc3 preparation passed in run `43670974577` on head `a08ef4cdb`
(Windows Server 2025 x64, about 2 minutes 35 seconds). The installed driver hash
was `7d64933b79355e336b915461a501ebf187b5c975869f99e9d57f45a5faf3f8a7`,
matching both Stage 2 prepared and packaged hashes. This is **not** enabled
Computer Use acceptance. Its optional WebView2 loopback CDP preflight was then
NOT RUN/unproven.

Expanded preparation passed in run `43677783571` on head `3a28c6407`. In addition
to those checks, the actual installed WebView2 accepted loopback CDP, a visible
Settings rail link was clicked, and the resulting Computer Use DOM was read.
The real native Quit menu and Keep Runtime running dialog also completed. This
is not a Settings **enable** test: rc3 still has the feature disabled.

Run `43684652439` on `0dc490105` repeated all preparation/editor/query checks
and the actual Settings DOM click successfully. Its unified-panel preflight
clicked VersionBadge and Check and required a distinct visible checked-at
receipt, a successful settled state, and Current version `3.2.7rc3`.
Auto-update remained false and prereleases true. No Update was clicked; this
proves the shared UI route, not an installation transition.

The supported credential-free tool-context fixture is now proven through the
default-off rejection: the real
installed Model Hub points to a tiny loopback OpenAI-compatible deterministic
server, and an ordinary `vibe agent run` starts the real shipped Vibey Turn.
The server asks the actually advertised Windows `powershell` tool to run the
shipped `vibe computer js`. It does not fabricate Session/Turn IDs, write the
runtime database, replace the Hub engine, or modify installed code. This is
reported as **deterministic model stub, not an LLM**. Only the external observer
judges case results; the stub never declares acceptance PASS. The nonempty
source-key field is a fixed fixture placeholder, not a model credential.
The prep probe requires the exact shipped CLI `computer_use_unavailable` error
with status `off`, reached after live-Turn admission; JS is **not executed**
while off. Enabled action execution remains NOT RUN until the activation RC.

Security fixture reports preserve each completed case independently of later
fixture failure. Lifecycle observations retain original process handles and
also scan for owned replacement driver/helper generations across the native
reprobe interval. Cleanup independently attempts process, profile, account,
port and work-root steps and verifies account/profile removal.

The elevated-target fixture additionally needs a positive control from the
actual Users-only caller, reopening the target process/token by PID. A narrow
candidate grants that SID only process-query and token-query object rights on
the freshly created elevated editor. Existing ACEs, integrity, identity and
privileges stay unchanged; shared target/runner token objects are refused.
Native run `43680355580` proved this narrow positive control on the pinned
editor: the actual Users-only caller reopened the elevated target, and the
parent's retained-target postcheck passed. This is query feasibility only,
not a protected-window control PASS. Failures remain
`elevated_target_query_access_unproven`.

Run `43678884845` found that Server 2025's System32 Notepad exited/rebrokered;
the query-ACL positive control was therefore **NOT RUN**, not disproved or
passed. Additional real-editor fixture preparation started at 13:06 UTC+08 on
November 15, independently of the already-completed model-stub timebox. The
replacement fixture pins official portable Notepad++ **8.9.9.1** (archive SHA256
`cbcefa34677e10fe6dd19802f7d6b3b9860cd6de62afeaae7680e09c51b47779`). Native runs
record its public source, actual executable hash/version, Authenticode signer
identity and validation. The executable directory is read/execute-only to the
ordinary account; each launch uses separate test-owned settings with plugins
and session reuse disabled. No name/descendant fallback may replace the exact
created PID. A prep-only ordinary-process/blank-document readback and an
elevated query positive control are separate from actual CU typing/refusal.
The observer reads one visible Edit/RichEdit/Scintilla document control with
`WM_GETTEXT`; it never writes document contents. Run `43680355580` also proved
the standard-user exact-created-PID/window/blank-document preflight and
recorded executable SHA256
`5518ad65f576b993c48da1878bda83a62f7a101d246aefab472379ca8800390c`, with a valid
timestamped `NOTEPAD++` signature. Additional fixture preparation took about
17 minutes (13:06–13:23 UTC+08). Nonempty CU typing and elevated control remain
NOT RUN on rc3. That run's Settings DOM readiness timed out; the earlier CDP
proof is not substituted for a fresh per-run prerequisite.

Settings is an on-only **Turn on button**, not a switch. The native menu owns
Off and Quit. Settings automation must click the shipped button in the actual
installed WebView; the harness may not substitute a direct native invoke or a
configuration write. Native menu automation discovers visible menu items and
uses their normal Windows menu-command route. No screenshots are written.

## Authorized follow-on: unified update panel (not yet executed)

Only after the shared real DOM click prerequisite, and strictly behind CU work,
this same harness may add one rc3 → rc4 case with the same CDP client. Click the
real VersionBadge → `[data-update-panel]` → Check → Update, require that rc4 is
actually offered by the public updater feed, observe the installer and real
relaunch, then verify Desktop/runtime versions and installed helper hash against
rc4 provenance. Preserve the existing auto-update setting; use the actual
pre-release switch if necessary. Reacquire the CDP target after relaunch and
check **Current version**, not the offered-version link. No direct update API,
patched feed, or second GUI automation stack.

The explicit `update` mode is scaffolded separately from `preparation` and
`enabled`, restricted to the authorized rc3 → rc4 pair. The sole writer selects
it only after CU work has priority and the public candidate exists. It does not
interrupt another run; workflow concurrency has cancellation disabled. It skips
CU/model/editor fixtures rather than reporting them as update acceptance.
Both releases pass the same checksum/Certum/provenance verifier; the downloaded
target is reference-only and is **never executed by the harness**.

The observer runs concurrently with the same CDP Update client. It retains an
actual standard-user `avibe-update-*/update.exe` process handle, verifies its
bytes against the target reference and its exit status, and retains its real
PowerShell transaction supervisor until a successful exit. Failure cleanup
stops producers and the supervisor before its installer; if quiescence cannot
be proved, ordinary worker Quit/uninstall is suppressed and the parent owns
account-wide teardown. It requires the original
Desktop to exit and a distinct exact-path owned Desktop window to appear, and
resolves the new installed runtime manifest. It then checks Desktop and runtime
versions, the installed driver hash, and the relaunched panel's Current version
and unchanged settings. Debugger listeners must belong to that retained live
replacement's process lineage, with pinned listener handles across the proof;
stale/ambiguous generations and an exited replacement are rejected. A click
or an offered-version link alone cannot pass.
Target not discoverable/shared CDP missing is a named NOT RUN blocker; required
update cases that are NOT RUN or FAIL fail the run. No transition is claimed
until this mode has run against the published candidate.

Downloads recheck release admission and have at most 180-second timeouts. A
40-minute operation deadline and a bounded worker leave room under the
45-minute job limit for owned-process/account cleanup and seven-day artifacts.
