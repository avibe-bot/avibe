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

Product decides separately whether a credentialed LLM Turn is required. The
activation RC must be explicitly selected only after its activation PR has
landed and its installer/Stage 2 provenance are published.
