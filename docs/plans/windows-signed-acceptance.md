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
2. Refuse an elevated app attach and refuse service from an elevated driver.
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
Computer Use acceptance. An optional WebView2 loopback CDP preflight remained
NOT RUN/unproven; it is not counted as a Settings enable test.

The draft is adding a supported credential-free tool-context fixture: the real
installed Model Hub points to a tiny loopback OpenAI-compatible deterministic
server, and an ordinary `vibe agent run` starts the real shipped Vibey Turn.
The server asks the actually advertised Windows `powershell` tool to run the
shipped `vibe computer js`. It does not fabricate Session/Turn IDs, write the
runtime database, replace the Hub engine, or modify installed code. This is
reported as **deterministic model stub, not an LLM**. Only the external observer
judges case results; the stub never declares acceptance PASS. The nonempty
source-key field is a fixed fixture placeholder, not a model credential.

Settings is an on-only **Turn on button**, not a switch. The native menu owns
Off and Quit. Settings automation must click the shipped button in the actual
installed WebView; the harness may not substitute a direct native invoke or a
configuration write. Native menu automation discovers visible menu items and
uses their normal Windows menu-command route. No screenshots are written.
