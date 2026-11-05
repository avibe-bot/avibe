# Mobile App onboarding proposal

## Change contract

Define the App-owned path from first launch to an instance, including recovery.
The **42 proposed frames** cover 21 states in Chinese and English, each in light
and dark themes. Two additional `alignment` frames capture unchanged shipped
components for comparison and retain `shipped` status. Approval of this proposal
precedes a separate implementation; no authentication or navigation is built here.

Acceptance properties: every entry/recovery state has a clear next action;
instance identity, ownership and availability remain readable; first selection is
distinguishable from switching; account access works before selecting an instance;
both locales/themes preserve the same hierarchy; each proposed element maps to an
existing shipped component or an unchanged composition from a shipped page.

Scope stays in `ui/design/`: frames, synthetic fixtures, kit, Apple artwork and
this handoff. No product/native/backend code, planning changes, tokens or new
dependencies. All source references below were inspected at base `84ae467725`.

## Frame inventory

Prefix every entry with `mobile-app/`; append `-zh` or `-en` to proposal states.

| Surface | States |
| --- | --- |
| `welcome` | `default`, `signing-in`, `cancelled`, `network-error`, `denied` |
| `instances` | `own`, `mixed`, `current`, `offline`, `offline-tapped`, `loading`, `error`, `pulling`, `refreshing`, `long-small` |
| `empty` | `default`, `cli` |
| `account` | `default`, `no-instance` |
| `native` | `instance-error`, `window-topbar` |
| `alignment` (shipped reference, no locale suffix) | `account-reference`, `error-reference` |

Stable implementation examples: `mobile-app/welcome/default-zh` and
`mobile-app/instances/long-small-en`. The `native/instance-error` ID is retained
for continuity; the recommended implementation is a packaged web shell page.

## Alignment inventory

The Show Page places a corresponding existing-page screenshot beside **every**
proposal, with locale/theme controls and a separate element mapping section.
References are captures of actual components, not redrawn approximations.

| Proposed element | Shipped component or exact existing composition | Reference frame |
| --- | --- | --- |
| Brand bar, scrolling content, page insets and background | `PhoneScreen` uses `MobileBrandHeader`, as `AppShell` does; only instance-only tabs are absent | `mobile/workbench-tab/team` |
| Page title, icon well, subtitle, account action | `WorkbenchPageHeader` with its existing icon/title sizes and `Button` toolbar action | `agents/agents-page/phone` |
| Section label and refresh action | `PageSectionTitle`, matching ProjectsPage's title/action row | `mobile/projects/collapsed` |
| Instance row | `MobileProjectRow` composition: `rounded-xl` border/surface, `px-4 py-3.5`, 16px icon, 14px semibold name; instance metadata adds wrapping | `mobile/projects/collapsed` |
| Online/offline/shared/current labels | `Badge` success/secondary/info/outline variants; no custom status dots | `system/badges/all` |
| AI partner identity | `BotAvatar` with the existing mascot; mobile identity-row spacing | `mobile/workbench-tab/team` |
| Main and Apple actions | Shipped `Button size="lg"`: 48px high, 14px text, `rounded-lg`; Apple mark/palette are its brand exception | `system/buttons/variants` |
| Signed-in account and sign-out | Actual `MoreAccountSection`, using `/api/session` fixture; no copied account markup | `mobile-app/alignment/account-reference` (real mobile Dock account sheet) |
| Initial load and retry | Exact ProjectsPage spinner/label and retry-panel composition | `mobile/projects/collapsed` (list geometry); source `ProjectsPage` loading/error branches |
| Empty state | Exact ProjectsPage `px-4 py-10 text-center text-sm text-muted` surface | `mobile/projects/collapsed` (list geometry); source empty branch |
| Installation choices | Existing More-page row composition, shipped Button variants, pairing language consistent with Remote Access | `settings/remote-access/phone` |
| Instance failure | ErrorFallback's icon, 15px title, 12.5px body, diagnostic well and sm brand/outline buttons, with instance-specific actions | `mobile-app/alignment/error-reference` |
| New-window toolbar | Actual `MobileAppHeader`, plus existing compact ghost actions | `apps/file-browser/phone` |

Other inspected patterns: `AccountMenu` is the desktop account dropdown, so the
mobile `MoreAccountSection` is the correct reuse. `InboxSectionHeader` binds its
labels to bots/sessions; `PageSectionTitle` provides the equivalent neutral mobile
section. `sessionRowLayout` owns sidebar indentation and running-session dots;
instance rows follow the mobile project row instead. `SortableProjectList` adds
drag/reorder behavior, which instance selection does not need. `SegmentedRadio`
is available but this flow has no mutually exclusive display modes. `InstallHint`
is for adding the browser app to Home Screen; it is not an install-the-computer
flow. `setup/intro/phone` informed first-use tone and brand ordering; its complete
assistant-relay story does not belong in account sign-in. There is no generic
shipped skeleton-row component: initial loading uses the actual ProjectsPage
spinner panel (also the existing provider screen's initial-loading pattern),
rather than inventing skeleton geometry.

## Flow and interaction handoff

- **Welcome:** Avibe/Agent OS leads, then Vibey as the built-in AI partner in both
  locales. General sign-in/create-account comes first and opens avibe.bot in the
  system panel; no credential form is drawn in the App. Apple is equally sized.
  The pending frame is the App underneath that panel, not simulated OS chrome.
  Cancellation is neutral; network/denied failures follow setup's inline alert
  treatment. Apple's official logo provenance is in the assets README.
- **Instances:** first selection has no current badge. Switching marks the current
  instance. Offline row selection preserves the list and uses the selected
  fixture's identity in its recovery message. Entry waits for availability.
  There is one permanent refresh action in the section header; no permanent
  pull-to-refresh hint. Pull/release and refreshing are transient gesture states,
  and refresh preserves the previous rows. Account stays reachable.
- **No instances:** desktop is the first installation row, CLI the second. Pair
  the computer with the same avibe.bot account, then use paired/refresh. Commands
  match the repository README. The final download/guide destinations must be
  the project's maintained links, confirmed during implementation.
- **Account:** the real mobile account/sign-out section appears above current
  instance and switch/choose. Native credential cleanup handles sign-out in the
  implementation. Delete account opens avibe.bot for consequences and confirmation;
  implementation must confirm that destination and complete the return flow.
- **Connection failure:** recommend the same packaged React shell as the other
  entry pages; native code reports the error and handles retry/switch. Its existing
  `native/instance-error` design ID does not mandate Swift/Kotlin rendering.
- **New window:** directly reuse MobileAppHeader's back, website title and compact
  action placement. Back navigates window history; Done closes it; Open in Safari
  hands it off. This is the iOS reference, with corresponding browser copy on
  Android. The short document below is illustrative content.

Sensitive operations remain native-owned. Proposed actions are static; shipped
components retain their existing behavior in the canvas, which is not a working
mobile onboarding implementation. Real focus, announcements, safe cancellation,
refresh completion and native transitions remain implementation requirements.

## Responsive and validation

Normal frames are 390 × 844; long-copy selection is 375 × 667. PhoneScreen owns
scrolling and native safe-area handling follows the existing shell. Names, domains
and shared-by text wrap. Existing compact controls keep the shipped geometry;
actual native hit targets and text scaling require device checks rather than a
blanket 44px claim.

Local design typecheck, UI build, lint and explicit design captures gate the PR.
All 84 proposal captures and the four alignment-reference captures are inspected
for layout and overflow; the Show Page also exports the existing reference pages.
Rendering proves neither real authentication nor keyboard/touch accessibility.
Owner review can focus on action order, offline recovery and bilingual copy.

## Read-only avibe.bot assessment

Backend `origin/main` at `88a2592`: actual sign-in is `site/login/phone`, consent is
`oidc/consent/phone`. Eight Chinese/English light/dark screenshots at 390 × 844
fit their content. English login footer wraps into two rows with a leading
separator on the second; prefer an intentional stacked narrow layout. App-native
Apple remains a separate entry from current web login methods.

Login captures have no console errors or unfixtured reads. Consent has no console
errors but its existing fixture has an unfixtured `store.getOAuthConsentGrant`
read. No backend changes. Real-device system panel, keyboard/email entry,
magic-link return, consent and native callback checks remain deferred.
