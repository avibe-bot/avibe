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

1. Launch real system Notepad by `computer.app({path})`, attach, type synthetic
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
the freshly created elevated Notepad. Existing ACEs, integrity, identity and
privileges stay unchanged; shared target/runner token objects are refused.
Native sufficiency is not yet proven. Until it is, report
`elevated_target_query_access_unproven`, not a protected-window PASS.

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
patched feed, or second GUI automation stack. This case remains NOT RUN until
implemented and executed against the published candidate.
