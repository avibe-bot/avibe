# Mobile App onboarding proposal

## Change contract

This proposal defines the App-owned path from first launch to an instance, and
recovery when that path fails. All 42 frames are **proposed**: 21 states in
Chinese and English, each in light and dark themes. Approval authorizes a future
implementation proposal; these frames implement no authentication or navigation.

The acceptance properties are: every entry/recovery state offers a clear next
action; instance identity, ownership and availability stay readable; first choice
and switching an existing selection are distinguishable; account access remains
available before selecting an instance; both languages and themes retain the same
information and action hierarchy.

Scope is confined to `ui/design/`: frames, synthetic fixtures, kit drafts, Apple
button artwork and this handoff. No product/native/backend code, planning files,
new dependencies or new tokens are included.

## Frame inventory

All IDs below use the prefix `mobile-app/`; append `-zh` or `-en` to each state.

| Surface | States |
| --- | --- |
| `welcome` | `default`, `signing-in`, `cancelled`, `network-error`, `denied` |
| `instances` | `own`, `mixed`, `current`, `offline`, `offline-tapped`, `loading`, `error`, `pulling`, `refreshing`, `long-small` |
| `empty` | `default`, `cli` |
| `account` | `default`, `no-instance` |
| `native` | `instance-error`, `window-topbar` |

For example: `mobile-app/welcome/default-zh` and
`mobile-app/instances/long-small-en`. IDs remain stable for implementation links.

## Flow and interaction handoff

- **Welcome:** general sign-in/create-account is first. It opens avibe.bot in the
  system authentication panel; the App contains no credential form. Apple is an
  equally sized second action, using official localized artwork in these frames
  and the native Apple control in implementation. The pending frame depicts the
  App underneath the system panel, with actions disabled; it does not simulate
  operating-system chrome. Cancellation is neutral, while network and denied
  states explain how to retry.
- **Instances:** first selection has no current-instance badge. Switching shows
  the current instance explicitly. Ownership and shared-by information sit next
  to the online/offline label; color is supplementary. Tapping an offline row
  retains the list and explains how to restore availability. It does not enter
  the offline instance. A refresh button supplements pull-to-refresh; refreshing
  retains the existing rows. The account entry is always available.
- **No instances:** desktop installation is primary; CLI steps are a second path.
  Pairing occurs on the computer with the same avibe.bot account. Both paths end
  at the explicit paired/refresh action. The installer command and `vibe remote`
  match the repository README. Download and installation-guide destinations must
  use the project's actual maintained links when implemented.
- **Account:** shows the signed-in account and current selection, with a dedicated
  switch/choose action. Sign-out returns to welcome through native credential
  cleanup. Delete-account opens avibe.bot to review consequences and confirm;
  the final account-deletion destination must be agreed during implementation.
- **Native references:** connection failure names the instance and offers retry
  and switch. The new-window header keeps Done, website address and Open in Safari
  together. Safari is the iOS reference; Android adapts the browser action. The
  example web content underneath is illustrative, not a redesign of the docs.

Sensitive operations (sign-in, credentials, list requests and instance entry)
remain native-owned. Every control here is intentionally static. Implementation
must supply focus, announcements, navigation/back behavior, refresh completion,
safe cancellation and real system-panel transitions.

## Reuse and responsive rules

`kit/mobile-app.tsx` proposes `WelcomeIntro`, `InstancePicker`,
`EmptyInstancesPage`, `AccountPage`, `NativeInstanceError` and `NativeWindowTopBar`.
They reuse `PhoneScreen`, shipped `Button`/`Card` primitives, the current logo and
mascot, and existing tokens. There are no new tokens. Draft bilingual strings
move to product i18n only when approved components ship.

Frames use a 390 × 844 content viewport; `long-small` checks 375 × 667. Native
safe areas live outside that viewport, consistent with existing phone frames.
Content scrolls vertically; instance names, domains and ownership wrap rather
than hiding essential identity. Actions target at least 44 CSS px. The native
implementation must verify actual safe-area and text-scaling behavior on devices.

Apple assets and generator parameters are recorded in
`../../assets/apple-sign-in/README.md`; they preserve Apple's supplied logo and
typography rather than approximating the mark.

## Verification and remaining decisions

Local `typecheck:design` and UI production build pass. The 84 theme/locale captures
pass with zero console errors and zero unfixtured reads. All state contact sheets
were inspected; primary welcome, empty and offline-recovery frames were also
inspected at full 2× size. This verifies layout, wrapping and visible hierarchy,
not real authentication, keyboard behavior, touch interaction or accessibility.
The export in the designer's Show Page is a presentation of these source frames.

Recommended approval choices: keep general sign-in first and Apple second; keep
offline rows tappable for an explanation, entering only after availability returns;
approve or revise the proposed bilingual copy. No further product decision is
needed to review the proposal.

## Read-only avibe.bot mobile assessment

Assessed backend `origin/main` at `88a2592` using existing frames:
`site/login/phone` is the actual sign-in page, and `oidc/consent/phone` is the
subsequent authorization step. Chinese/English and light/dark captures were
produced at 390 × 844. Login and consent content fit at that size. The English
login footer wraps into two rows with a leading separator on the second row;
use an intentional stacked layout without the separator on narrow screens in a
later backend change. Login offers the current web methods;
App-native Apple remains a separate proposed entry.

Login captures report no console errors or unfixtured reads. Consent captures
report no console errors but an existing unfixtured `store.getOAuthConsentGrant`
read; this limits fixture completeness, not the scope of this design. No backend
files are changed. Device verification is still needed for the system panel,
keyboard/email input, magic-link return, consent and native callback round trip.
