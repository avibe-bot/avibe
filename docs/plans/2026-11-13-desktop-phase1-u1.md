# Desktop instance client: bundled UI (U1)

## Change contract

Phase 1 adds a capability-gated Desktop account footer and a separately bundled,
full-window account surface. Ordinary Web and older Desktop shells retain their
existing footer and Setup behavior. The native coordinator owns authentication,
enrollment, instance authorization, cancellation and committed connection state.
No account cookies, provider tokens, pairing keys, enrollment tokens or OAuth
URLs enter this UI. TOTP setup/recovery material exists only in direct replies
and transient account-page memory; it is never written to browser storage.

The bridge uses `desktop_client_request` with `{ request: envelope }`. All
requests use version 1, a fresh correlation ID and the latest native generation.
The runtime view only consumes the nonsecret projection. Native presentation
requests and footer navigation share the existing unsaved-changes guard.

The bundled entry is `account/index.html?view=account|instances|setup-complete|sign-out`.
The query selects presentation, never a mutation. The build output is
`desktop/dist/account/index.html` plus local assets. Its CSP admits no cloud
scripts, frames, form submission or network fetch; the only connection target
is Tauri IPC. The bundle imports the existing React components, theme and catalogs.

## Real shell slot and design handoff

`AppShellFooter({ tools: ReactNode, children: ReactNode })` is exported from
`ui/src/components/AppShell.tsx`. It owns the existing two-row geometry.
`AppShell` accepts `footer?: ({ tools, defaultView }) => ReactNode` so design
consumers can inject a footer view while retaining actual Apps/Settings behavior.
The default view chooses the account row only with native bridge version 1.

The footer avatar and Apps container start at x=16. Instance label, status and
chevron use gap-1.5 (6px); the local Desktop version is independently right
aligned. Account and update popovers are exclusive. The update popover anchors
to the entire row, preserving x=16 rather than its right-hand trigger.

Source authority: `ui/design/kit/desktop-instances.tsx`, placement kit and
`ui/design/frames/desktop/instances*` in design PR #3649. Observed design head:
`d3c5393b4577ca433605be8e4c224f38f3150a99` for reference captures; later source
diff through `bd47d067692dd1d2fe64f27faf11b0cd00b73dda` was inspected before
submission (separate connection/update state and shell metrics). This PR changes
no design sources.
The design owner will consume the real slot after separately authorized merge;
frame/product equivalence is not claimed before that follow-up.

## Invariants and evidence

- AUTH-SETUP-170: both Wizard completion exits read back durable completion,
  navigate out of Setup, then present the same optional account flow, once.
  Remote Runtime completion never registers that Runtime as this Mac.
- AUTH-SETUP-171/177: only native state confirms login, second factor and consent;
  direct TOTP material is not broadcast or persisted. Account UI has no HTTP client.
- AUTH-SETUP-173/174: conflict is read-only and offers other instances; failed
  tunnel retry is distinct from enrollment retry; status-only remote access.
- AUTH-SETUP-176: stale-generation replies cannot replace newer projection;
  Escape/Back sends cancel; explicit local return sends return_local.
- AUTH-SETUP-178: actual AppShell geometry and local updater scope at 1200x800
  and 880x600, both languages and themes. Account email is menu-only in the footer.
- AUTH-SETUP-179: the remote auxiliary limitation is visible in account UI/menu.

Focused tests extend the existing Wizard completion suite and shell slot tests.
`ui/e2e/desktop-client` runs the actual product shell and the built account bundle
against an explicitly labeled native peer fixture. Every browser context owns
its storage; requests outside fixture origins are blocked. This is UI evidence,
not proof of the native/backend producer-consumer chain.

Build: `npm ci` in `ui` and `desktop`, `npm run build` in each. Browser evidence:
`cd ui && npx playwright test --config playwright.desktop-client.config.ts`.
Artifacts live under `ui/e2e/.artifacts/desktop-client`; failed runs are retained
separately for diagnosis. Native integration consumes the exact built bundle
and the exported bridge types; the integration owner must join actual producers.

## Remaining acceptance tiers

Exact-head review/CI and Lead verification of the actual producer-consumer chain
are required before Merge-ready. Signed package checks remain a later authorized
stage: native credential storage, passkey association/system sheet, WebView cookie
isolation, native hit testing, update decision and local task continuity.
No merge, deployment, release, install or service restart is part of this lane.

The existing `avibe://updates` is the only updater CTA. Lead confirmed that S1
will route this intent to the existing native updater decision (not back to the
same Workbench popover) once the native-owned Phase 1 capability is enabled.
The footer handles native-menu `avibe:desktop-open-updates` locally. Neither a
presentation URL nor any Runtime action installs an update by itself.

Local browser verification passed 25 account/shell cases and both real Wizard
completion exits (27 total). Focused unit verification passed 141 tests in four
files; UI/account builds, UI lint and focused TypeScript checks passed.
The browser suite covers passkey/TOTP/recovery and explicit sign-out confirmation.
Wizard fixtures serve a usable Vibey model and confirm the durable setup write
before intercepting OS protocol presentation. That interception prevents launching
the user's installed Avibe; native integration must exercise the real protocol.
The first failed browser run exposed a fixture startup ordering error and a
custom Popover anchor registration ordering issue. Both failures are retained;
the final actual-shell measurements are avatar/tools x=16, status/chevron gaps
at most 6px and update popover x=16 within both viewports.
Reference frames were re-rendered from D0 at 1200x800 in light/dark and inspected
against product captures (account menu, updates, email). Product captures also
cover 880x600. The menu adds the required Phase 1 auxiliary explanation and
reflects fixture remote-access Off; the account page adds real empty/disabled
inputs and generation-safe actions. The new required states are implementation
coverage, not fixtures claimed to exist in D0.

## Native integration handoff

From this worktree, run `npm --prefix desktop run build` after installing the
locked UI and Desktop dependencies. Use `desktop/dist/account/index.html` and
its adjacent `assets` directory together; the same Desktop dist also retains the
bootstrap entry. `ui/e2e/desktop-client/bridge-fixture.ts` documents a runnable
peer fixture and `client.spec.ts` drives the actual bundle. Replace the fixture
invoke with the native coordinator for producer-consumer acceptance. Fixture
names include `账号-fixture`, `小麦的 Mac`, `工作室 Studio` and `共享团队`.

Preserved local evidence: `/tmp/desktop-u1-sesuzge7suz9p/browser-evidence`,
`setup-rerun`, `reference`, and build/test logs in the same directory. Initial
failed runs remain available in `browser-first-failures` and `setup-final`.
