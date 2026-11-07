# Mobile App onboarding — A / Cloud letter

Status: **approved** by the owner on 2026-11-05 at 23:56 Asia/Shanghai as the
implementation contract. The owner selected A (云间来信 / Cloud letter) at 21:39
and approved the final design and merge at 23:56. This is the complete design-only
handoff for PR #3252. B/C studies and the previous ordinary compositions are
removed. Unchanged shipped component references remain separate. Product
implementation is a subsequent task; approval does not verify runtime behavior.

## Change contract

Welcome → email → six-digit email OTP → instance selection. Preserve the same
account, instance ownership/sharing, availability and recovery semantics. A uses
Avibe's high-resolution cloud mascot, subtle mint/cyan/violet background, raised
rounded surfaces and pill actions. Every approved design exists in Chinese and English,
and light and dark. No Apple/Google/GitHub choices or browser-login instructions.

Acceptance invariants:

- Every state has a clear next action; busy/cooldown states prevent duplicate
  actions and retain the email/code. Timers in the frames are static fixtures.
- Email is always complete: never masked, ellipsized, clipped or truncated in
  recipient/account text. Long text wraps. The account frame renders the actual
  `MoreAccountSection` and uses a design-only descendant style to replace its
  truncation with wrapping; implementation must expose the same wrapping policy.
  An editable email field retains native email-input editing behavior; any long
  value must also be shown as complete wrapped text during implementation.
- OTP is six equal squares in a continuous row, uniform `gap-2`, with no dash or
  grouping. One accessible input underlies the decorative display.
- Current instance is marked only when switching. Offline recovery derives its
  name from the selected fixture, preserves the list and waits for availability.
- Initial load uses matching skeleton rows; refresh retains existing rows.
- Scope is `ui/design/` only. No new tokens, dependencies, production components,
  authentication/native/backend behavior, runtime/config changes or plans edits.

## Frame inventory

Prefix IDs with `mobile-app/`; append `-zh` or `-en` to approved states. Existing
flow IDs remain stable; `welcome/intro` and `welcome/code-partial` complete the A
flow, and `account/long-email` verifies the account wrapping policy.

| File / surface | States |
| --- | --- |
| `welcome` | `intro`, `default` (email entry), `email-invalid`, `sending`, `email-rate-limited`, `email-network-error`, `code-sent` (blank), `code-partial`, `code-ready`, `verifying`, `code-invalid`, `code-expired`, `rate-limited`, `network-error`, `resending`, `resend-success` (60s), `resend-failed` (15s), `success`, `code-small` |
| `instances` | `own`, `mixed`, `current`, `offline`, `offline-tapped`, `loading`, `error`, `pulling`, `refreshing`, `long-small` |
| `empty` | `default` (desktop + CLI choices), `cli` |
| `account` | `default`, `no-instance`, `long-email` |
| `native` | `instance-error`, `window-topbar` |
| `alignment` — shipped references, no locale suffix | `account-reference`, `error-reference` |

**72 approved frames / 144 theme captures**, plus 2 shipped references / 4
captures. `welcome/success` immediately presents the existing picker composition;
there is no invented success detour. Frames normally use 390 × 844. Long email
and long instance names also use 375 × 667. `compact` is a fixture for the small
OTP composition: smaller illustration and tighter header spacing make room for
the full email; implementation adapts to available height and keyboard insets.
Scrolling owns content that exceeds the viewport; it never conceals email text.

## Kit, primitives and token sources

All A drafts live in `ui/design/kit/mobile-app.tsx`; no competing direction kit.

| Composition | Purpose and shipped reuse | Existing values / source |
| --- | --- | --- |
| `CloudScreen`, `Brand` | PhoneScreen without instance-only tabs; brand/back/account actions | `--gradient-wizard-platforms`; `p-6`, `max-w-xl`, `h-11`, logo asset, shipped Button |
| `WelcomePage` | Mascot-led welcome with a friendly invitation | Existing `assets/mascot/cloud-tuanzi-v2.png` at `size-52` inside `size-64`; `shadow-mint-card-lg`, `mint`, `surface`, `rounded-full` |
| `WelcomeIntro`, `CodeInput` | Email form and six-cell OTP state display | Existing `vibey-message.png`; Input/Label; `rounded-3xl`, `p-6`, `shadow-mint-card`; cells `grid-cols-6 gap-2 aspect-square rounded-2xl text-2xl`; ring uses `mint`, invalid uses `destructive` / `destructive-ink` |
| `Primary` | Full-width primary action | Brand Button, `size=lg`, `rounded-full`, `shadow-mint-card-sm` |
| `InstanceRow`, `InstancePicker` | Identity, host, ownership, availability and current selection | Button composition; shipped Badge success/secondary/info/outline; `p-5`, `gap-3`, `size-11`, `rounded-3xl`, `border-border`, `bg-surface/80`, `shadow-mint-card-sm` |
| `InstanceSkeleton` | Same row footprint before data arrives, static here | Shipped Skeleton; text/icon/badge geometry matches the instance card; shipped SkeletonShimmer and handover tokens are the implementation source |
| `EmptyInstancesPage` | Desktop and CLI installation/pairing guidance | Existing icons, raised surface, Button, `surface-2`, `font-mono` only for actual commands |
| `AccountPage` | Actual account/sign-out component, wrapping override, current instance, full-row switch | MoreAccountSection, InstanceRow, outline Button, existing radius/shadow tokens |
| `NativeInstanceError` | Instance failure and retry/switch recovery | Existing AlertTriangle, `gold-ink`, raised identity card, brand/outline Button |
| `NativeWindowTopBar` | Child-window browser actions | Actual MobileAppHeader, compact ghost Buttons; the document underneath is illustrative |

The old-runtime account fallback is an implementation compatibility state, not
a new design-frame variant. It reuses the Account avatar and
`MobileBrandHeader` metrics: a 56px content row plus the top safe-area inset,
the existing background/border tokens, the account email-local-part initial,
and Account/Change Instance actions. In a current native shell,
`MobileBrandHeader` owns the same Account avatar as a 44pt target and opens the
bundled Account/Change Instance surface. That reachable header action registers
with native code, so current Web UI shows no native fallback. The mobile chat
header uses the same 56px content height and safe-area ownership; built-in app
headers keep their separate compact 48px chrome.

All spacing, type, radius, semantic colors, shadows, gradients and motion values
come from `ui/src/index.css` or existing component variants. No new tokens. The
round/surface/gradient layers are static, with no animated filter or backdrop.
Typography retains Avibe's installed product font. Draft bilingual kit copy moves
to the product's locale catalogs when implemented.

## Interaction and accessibility handoff

- The email field uses `type=email`, email autocomplete/input mode, no automatic
  capitalization or spellcheck, and an associated Label. Invalid format displays
  an inline alert linked through `aria-describedby`; sending disables repeats.
- Code uses one labeled Input with `type=text`, `inputMode=numeric`,
  `autocomplete=one-time-code`, `pattern=[0-9]{6}` and `maxLength=6`. The six cells
  are `aria-hidden`; no six-focus-stop implementation. Blank/partial frames show
  the current cell's mint border/ring/caret; full codes enable verification.
  Errors use destructive cells and a text alert, never color alone.
- Production must synchronize typing, paste and autofill to the display, normalize
  digits, focus the underlying field when any cell is tapped, expose a visible
  focus state on the current cell without an outer ring around all six cells, keep
  the cells and Verify action reachable above the keyboard accessory bar, and
  announce busy/error/success changes. The static fixture input does not implement
  those behaviors. No screen-reader or iOS autofill claim is made.
- Change email is centered with the recipient text. It returns to editable email
  entry, clears the code, error and resend timer. Busy actions cannot duplicate.
- Initial send success enters blank code entry with resend available. Resending
  disables repeat actions. Completed resend success waits 60s; failure waits 15s.
  At zero the text link becomes Resend again. A 15s UI delay is **not** a promise
  that server throttling has ended. Rate-limit errors still ask to try later.
- Error and expired fixtures intentionally share the existing invalid-or-expired
  wording. Distinct frames do not imply the service can distinguish these causes.
- Refresh is a single permanent header action; pulling/release/refreshing states
  are transient. Cached rows remain visible during refresh. No cached data means
  skeleton, and a failed load gives retry in that region.
- No instances: desktop first, CLI second. Commands preserve the repository's
  install + `vibe remote` flow. Pair the same account, return and refresh.
- Account retains actual MoreAccountSection/sign-out. The full-width switch or
  choose-instance action is **owner-approved**.
  Delete account leads to avibe.bot to review consequences and confirm; final
  maintained destination and native credential cleanup belong to implementation.
- Instance failure is a packaged shell-page proposal; its stable `native/*` ID
  does not require a Swift/Kotlin renderer. Retry/switch remain native-integrated.
- Child-window back navigates history, Done closes, Open in Safari hands off on
  iOS. Android uses its corresponding browser copy. This toolbar is not an auth
  instruction or system-browser login path.

## Motion specification — approved standard strength

`docs/plans/web-ui-motion.md` (owner-approved PR #3263) and its existing tokens
supersede the earlier spinner suggestion and exploratory decorative effects.
**These frames are static.** The table specifies future implementation only.

| Moment | Existing tokens, duration and curve | Reduced motion |
| --- | --- | --- |
| Mascot arrival / floated resting composition | One small rise using `--motion-offset-sm` (4px) and `--motion-spring-smooth` / duration (440ms); opacity on `--motion-duration-base` (240ms), enter. No perpetual idle bobbing, in line with the approved purposeful-motion rule. | No translation; fast 160ms crossfade or immediate appearance. |
| OTP focus / new digit | Border/fill of the one active cell on `--motion-duration-fast` (160ms), `--motion-ease-enter`; input/focus updates immediately. No six staggered entrances. | Color/focus remains, no travel or scale. |
| OTP error | Inline alert fades on fast 160ms / enter; cells change destructive color. No repeated shake or animated shadow. The optional exploratory shake is omitted under the approved standard. | Static destructive state and text alert; no shake. |
| Press / release | Controls scale to `--motion-scale-press-control` (0.94), cards to `--motion-scale-press` (0.97), instant 100ms; release uses snappy 290ms. Respect the approved 60ms scroller touch delay. | Color step only. |
| First instance load | Shipped Skeleton blocks, one SkeletonShimmer sweep per card on `--motion-shimmer-duration` (1600ms). Use its existing reveal delay; don't flash skeleton for faster reads. No spinner. | Still blocks, no shimmer or reveal fade. |
| Skeleton → rows / refresh | `--motion-handover-duration` / ease: 240ms / enter. Match row geometry; cached identities remain still. Failure/retry reuses the same region. | Content replaces skeleton in one frame. |
| Forward / back between entry steps | Follow approved M5: View Transition on the `--motion-nav-*` pace (push 260ms, pop 220ms, one decelerating curve) on supported iOS 18+, prior page drift `--motion-drift` (30%). Unsupported iOS 17 uses a cut. Finish on new input; only the app's own Back animates a pop, and a history step the system took (an edge swipe) is a cut. | Fast 160ms crossfade, no push. |

WKWebView budget: compositor transform/opacity only for travelling elements;
never animate layout dimensions, filters or list shadows. Animate only the
visible/changed region; pause hidden work. Native edge swipe owns the gesture,
so no simultaneous web back animation. Follow approved section 5 measurements
(no motion-caused main-thread task over 16ms, zero per-frame style/layout work,
no dropped compositor frames; Chromium 4× is only a proxy);
real iPhone/WKWebView frame-time verification belongs to implementation. This
proposal adds no runtime animation, duration or parallel motion token system.

## Owner approval and verification boundary

The owner approved these four decisions on 2026-11-05 at 23:56 Asia/Shanghai:
shared error/expired wording; offline-tap recovery explanation; centered Change
email; full-row switch-instance action.
The old spinner question is settled by #3263's skeleton policy.

Focused checks: design typecheck, UI lint baseline, production build, whitespace,
all `mobile-app` screenshots and a bounded light/dark visual inspection. Capture
results and exact-head Codex/CI gates are recorded in the PR. A screenshot proves
layout, not functioning authentication or accessibility.

Unverified implementation items: real mail delivery, live Supabase template or
OTP configuration, native session/token handling, keyboard insets, paste/autofill,
focus/announcements, touch targets/text scaling on device, refresh/recovery
completion, maintained install/delete destinations and motion performance. No
production services, accounts, credentials or auth configuration were changed.
The prior read-only backend source assessment was pinned to `88a2592`; it is
historical flow evidence, not a live authentication test.

## Two-step verification — proposed

Status: **proposed**. Plan: `docs/plans/2026-11-07-mobile-two-factor.md`.
After the email code, an account with avibe.bot two-step verification confirms
it is the account owner. The page uses the same A composition (`CloudScreen`,
`Brand`, `Primary`, the six-cell `CodeInput`) and adds no tokens or assets.
`SecondFactorPage` lives in `ui/design/kit/mobile-app.tsx`.

| File / surface | States |
| --- | --- |
| `two-factor` | `passkey`, `passkey-waiting`, `passkey-failed`, `totp`, `totp-partial`, `totp-verifying`, `totp-invalid`, `totp-rate-limited`, `totp-network`, `totp-only`, `recovery`, `recovery-invalid`, `setup-required`, `totp-small` (375 × 667, long email) |

The page offers a passkey first when the account has one. The authenticator
code and a recovery code are alternatives, and "Sign in with a different
account" signs out. The authenticator code reuses the OTP input contract. The
recovery code is one text field. An account that must turn on two-step
verification opens avibe.bot and returns to "I've turned it on · Continue".
The system passkey sheet is native and is not drawn; `passkey-waiting` is the
page behind it.
