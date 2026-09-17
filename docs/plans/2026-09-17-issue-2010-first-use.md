# Issue #2010: shared first-use presentation

## Change contract

Welcome and assistant installation use the same React surfaces on Web and Desktop.
The existing logo, platform icons, lifecycle APIs, provider configuration entry,
setup persistence and authorization remain owned by their current components.
No Python, ApiContext, Tauri, Model Hub, Settings shell or Workbench change belongs
in this slice. Issue #2011 owns credential confirmation and the final workspace gate.

Welcome checks the three CLI paths before advancing. A failed check remains on
Welcome with retry/details. The setup rows stay in Claude Code, Codex, OpenCode
order. Each installation and detection updates only that row; failures retain
other results. Runtime updates remain optional through BackendLifecycleChip.
Connection styling is a presentation input only; executable discovery cannot
produce a connected claim.

The existing Configure provider modal stays unchanged. Continue still leads to
platform selection and the existing Summary completion action. Enable controls
and the existing OpenCode permission policy remain available until #2011 replaces
the completion contract. These are intentional intermediate-slice differences
from the final connected setup design, not new readiness rules.

## Design evidence

Read through the installed Pencil MCP on 2026-09-17, using only Get and Export:

- Welcome: `bi8Au`, `J0qaZ`, `P8iLm0`.
- Installation: `i3vw9`, `lm7mN`, `ktRgC`.
- Hover: `mK2JX`, `ZaXoW`, `X7N2Z`.
- Awaiting connection: `ICidc`, `S3mLYc`, `HR3ps`.
- Upgrade: `I9Q8VD`, `pHiuw`, `QPdWU`.
- Connected: `F8RLgR`, `r1IQ0B`, `xNmhX`.

The final document has Chinese Dark/Light and English Dark boards, with no
separate English Light board. Validate English under the same Light tokens.
Native titlebars are omitted on Web. Existing BrandLogo and BackendIcon assets
remain authoritative. Shared semantic palette/glow tokens are reused without
changing the global theme. Card sizes, row sizes, spacing and typography follow
the final frames; small viewports stack cards and wrap row actions.

One 8.9-second clock owns all collaboration states and pulses. Reduced motion
shows all work complete. Pause/replay also pauses access-tile idle emphasis;
hover and keyboard focus suspend idle emphasis without moving the labels.

## Validation plan

- Focused Vitest: handoff boundaries, reduced motion, pause/replay, access tiles,
  detection retry, independent installs, configuration entry and unchanged saves.
- Existing wizard mutation and backend/runtime tests.
- UI build, lint, theme validation and scenario-catalog validation.
- Isolated browser checks for both languages/themes and a narrow viewport.
- Exact-head Codex review, all required CI, zero unresolved review threads.

No real backend login or live credential readiness claim is made in this slice.

## Review circuit-breaker decision (2026-09-17)

PM independently verified the full PR #2015 inventory at `03e1004fb`:
`e1504ffc4` review 5232344841 found premature Back navigation (resolved thread
PRRT_kwDOPbFPYs6jPxPy); `03e1004fb` review 5232430397 found overlapping provider
reconciliation (open PRRT_kwDOPbFPYs6jP88p) and stale installation errors after
successful detection (open PRRT_kwDOPbFPYs6jP88t). Two findings-bearing heads share
the synchronization/navigation class, triggering the breaker. Both lint runs
35192733742 and 35193718025 failed the same dynamic i18n key coverage check.

PM authorized a local repair: admit only one provider configuration/reconciliation
at a time, return the final live rows from both navigation actions without replaying
a pre-detection snapshot, clear only obsolete failed-install evidence after a
current successful detection, and use a literal Claude summary translation key.
Deferred config/probe consumer tests must preserve canonical non-ASCII paths,
provider fields, local enable toggles, other rows and independent installations.
The existing owner remains sufficient; no auth/readiness model rewrite is allowed.
PM must spot-check the concrete diff and tests before the next push.

Keep the original combined Watch/cursor. Owner policy prohibits manual Codex review
triggers: cyhhao triggers automatically after pushes; report any automation gap to
PM. Exact-head review, CI and thread gates remain required. No merge or #2011 work.

PM approved the seven-file repair before push at 15:47 Asia/Shanghai after reading
the working diff, modal/navigation paths, consuming tests and red/green logs.
PM independently ran AssistantSetup and keyCoverage: 2 files / 26 tests passed.
The diagnosed two-head breaker is released for this bounded repair only; any
new repeated-class findings require a fresh independent diagnosis before edits.

## Local evidence

- Breaker repair reproduced Configure admission, Continue replaying a missing
  status/noncanonical path, and obsolete install error projection before fixes.
  Consumer tests cover two deferred configuration cycles through Back and
  Continue, independent installation, unsuccessful probes and stale-token success.
  Focused checks (81 tests including key coverage, wizard mutation, lifecycle and
  authorization consumers), UI build/lint, test typechecks and theme/catalog pass.
- UI build, lint, theme validation, and scenario-catalog validation passed.
- Focused Vitest covers the timeline, Welcome/controller behavior, independent
  row installs, upgrade availability, existing configuration entry and wizard
  config mutations. Existing setup authorization tests are included in the pass.
- Isolated Playwright used the production build, with all API requests fulfilled
  in the browser and all external origins blocked. Eight combinations (en/zh,
  light/dark, 1200/390 pixels) reached Welcome, detected assistants, entered setup
  and returned. No page errors, real writes, or horizontal overflow occurred.
  Desktop rows measured 880 by 88 pixels. Reduced motion completed all three cards.
- Browser screenshots and measurements: `/tmp/issue2010-browser/` in the lane
  environment. Pencil exports: `/tmp/issue2010-design/`. These are local evidence,
  not shipped assets or a claim of completed owner visual acceptance.
- Owner visual acceptance and real isolated runtime installation remain manual.
  PM explicitly authorized API-mocked browser validation for this slice; the
  Incus runner was not used because it writes primary-checkout runtime metadata.
