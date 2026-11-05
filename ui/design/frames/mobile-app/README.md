# Mobile App onboarding proposal

## Change contract

Define the App-owned path from first launch to an instance, including recovery.
The **66 proposed frames** cover 33 states in Chinese and English, each in light
and dark themes. Two additional `alignment` frames capture unchanged shipped
components for comparison and retain `shipped` status. Approval of this proposal
precedes a separate implementation; no authentication or navigation is built here.

Acceptance properties: every entry/recovery state has a clear next action;
instance identity, ownership and availability remain readable; first selection is
distinguishable from switching; account access works before selecting an instance;
both locales/themes preserve the same hierarchy; each proposed element maps to an
existing shipped component or an unchanged composition from a shipped page.

Scope stays in `ui/design/`: frames, synthetic fixtures, kit and
this handoff. No product/native/backend code, planning changes, tokens or new
dependencies. All source references below were inspected at base `84ae467725`.

## Frame inventory

Prefix every entry with `mobile-app/`; append `-zh` or `-en` to proposal states.

| Surface | States |
| --- | --- |
| `welcome` | `default`, `email-invalid`, `sending`, `email-rate-limited`, `email-network-error`, `code-sent`, `code-ready`, `resending`, `resend-success`, `resend-failed`, `verifying`, `code-invalid`, `code-expired`, `rate-limited`, `network-error`, `success`, `code-small` |
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
| Email and verification fields | Shipped `Input` and `Label`, `flex flex-col gap-2` composition; `Input` owns `fieldBaseClass`, h-9 and focus ring | `system/fields/all` |
| Submit and resend actions | Unmodified default-size `Button`: default primary and outline secondary; text/sm change-email action | `system/buttons/variants` |
| Field errors | `OAuthCallbackCodeInput` inline alert composition, with aria-invalid and aria-describedby | `system/fields/all`; source `settings/oauth/OAuthFlowParts.tsx` |
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

- **Login / registration:** App-owned email input, then one six-digit code field.
  Both use avibe.bot; new email addresses register through the existing
  `signInWithOtp` behavior. The heading is “登录 / 注册” / “Sign in / Create account”.
  Avibe/Agent OS and Vibey as the built-in AI partner remain on the first step.
  No password, third-party login, browser authentication panel or Apple artwork.
  Native owns the auth calls and credentials; the packaged React page collects
  email/code and presents state. Success opens the existing instance picker,
  represented by `welcome/success`, without an extra success screen.
- **Code entry:** one shipped Input, `type="text"`, `inputMode="numeric"`,
  `autoComplete="one-time-code"`, `pattern="[0-9]{6}"`, `maxLength={6}`. It accepts
  normal paste; do not split the field into six boxes. Implementation mirrors
  backend digit normalization and enables verification only for six digits.
  The explicit instruction is “输入邮件里的 6 位验证码”; supporting copy explains
  that the email link opens Safari and will not return to the App. Android uses
  its browser name. The recipient stays visible; change-email returns to an
  editable email step and clears code, errors and resend timer.
- **Send / resend:** initial success enters `code-sent`, with resend available,
  matching the pinned web implementation. Only a completed **resend** starts
  its cooldown: success **60 seconds**, failure **15 seconds**. `resend-success`
  and `resend-failed` show the starting values; at zero, restore Resend email.
  These are retry delays, not code expiry or a promise that a server rate limit
  ends in 15 seconds. Sending/resending/verifying states disable duplicate actions.
  Rate-limit and network states retain the email/code so the user can retry.
- **Error copy:** invalid and expired code have separate fixtures but deliberately
  share backend `otpInvalid`: “验证码无效或已过期，请重试或重新发送邮件。” The existing
  web client does not distinguish these causes. Rate-limit and network feedback
  are App proposals; future implementation must map real error outcomes rather
  than claim the pinned web UI already exposes them. Inline alerts reuse the
  shipped OAuth callback form treatment. No terms/privacy agreement is added:
  neither `app/login/page.tsx` nor `login-form.tsx` renders one at the pinned SHA.
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

Normal frames are 390 × 844; long-copy selection and long-email code entry are
375 × 667. PhoneScreen owns
scrolling and native safe-area handling follows the existing shell. Names, domains
and shared-by text wrap. Existing compact controls keep the shipped geometry;
actual native hit targets and text scaling require device checks rather than a
blanket 44px claim.

Local design typecheck, UI build, lint and explicit design captures gate the PR.
All 132 proposal captures and the four alignment-reference captures are inspected
for layout and overflow; the Show Page also exports the existing reference pages.
Rendering proves neither real authentication nor keyboard/touch accessibility.
Owner review can focus on the email/code sequence, retry feedback and bilingual copy.
Input editing/paste uses the native HTML field, but fixture-driven buttons and
countdown values are static; this is not an interactive authentication prototype.
iOS email-code autofill, the software keyboard and a real paste-to-submit flow
must be verified in the implemented App.

## Read-only avibe.bot assessment

Behavior and copy are pinned to backend `88a2592bc8261d563baf5b9620c4dd1e45dea6c2`,
`app/login/login-form.tsx` and `lib/i18n/messages.ts`. The existing magic-link
path already supports `verifyOtp({ email, token, type: 'email' })`; this proposal
makes that code path the primary App flow. Email validation, six-digit validation,
resend success/failure timing and the combined invalid/expired message follow
that source. Login/register share `signInWithOtp` with automatic user creation.
No backend change is proposed or implemented. Native session establishment and
instance-list access still need end-to-end verification during implementation.

The earlier read-only mobile assessment remains available as historical web
context: actual sign-in is `site/login/phone`, consent is `oidc/consent/phone`.
Eight Chinese/English light/dark captures at 390 × 844 fit their content. English
login footer wraps into two rows with a leading separator on the second; prefer
an intentional stacked narrow layout. This is the website's existing navigation,
not a terms/privacy agreement. Existing website OAuth choices and consent are
not part of the proposed App login.

Login captures have no console errors or unfixtured reads. Consent has no console
errors but its existing fixture has an unfixtured `store.getOAuthConsentGrant`
read. No backend edits. Real-device OTP autofill, keyboard, paste, send/resend,
verification and native credential/session transitions remain deferred.
