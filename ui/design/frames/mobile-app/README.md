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
`MobileBrandHeader` metrics: a 44px content row plus the top safe-area inset,
the existing background/border tokens, the account email-local-part initial,
and Account/Change Instance actions. In a current native shell,
`MobileBrandHeader` owns the same Account avatar as a 44pt target and opens the
bundled Account/Change Instance surface. That reachable header action registers
with native code, so current Web UI shows no native fallback. The mobile chat
header uses the same 44px content height and safe-area ownership; built-in app
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

## Two-step verification — approved

Status: **approved** by the owner on 2026-11-07 at 21:39 Asia/Shanghai, with the
three decisions in the plan. Plan: `docs/plans/2026-11-07-mobile-two-factor.md`.
After the email code, an account with avibe.bot two-step verification confirms
it is the account owner. The page uses the same A composition (`CloudScreen`,
`Brand`, `Primary`, the six-cell `CodeInput`) and adds no tokens or assets.
`SecondFactorPage` lives in `ui/design/kit/mobile-app.tsx`.

| File / surface | States |
| --- | --- |
| `two-factor` | `passkey`, `passkey-waiting`, `passkey-failed`, `totp`, `totp-partial`, `totp-verifying`, `totp-invalid`, `totp-rate-limited`, `totp-network`, `totp-only`, `recovery`, `recovery-invalid`, `setup-required`, `totp-small` (375 × 667, long email) |

**28 approved frames / 56 theme captures** (14 states × zh/en × light/dark).

The page offers a passkey first when the account has one. The authenticator
code and a recovery code are alternatives, and "Sign in with a different
account" signs out. The authenticator code reuses the OTP input contract. The
recovery code is one text field. An account that must turn on two-step
verification opens avibe.bot and returns to "I've turned it on · Continue".
The system passkey sheet is native and is not drawn; `passkey-waiting` is the
page behind it.

## Password sign-in — approved

Status: **approved** by the owner on 2026-11-08 at 12:46 Asia/Shanghai, with
the four decisions below. On 2026-11-09 at 01:51 Asia/Shanghai the owner
replaced decision 2 (identifier-first) with one form: "Sign in with password"
turns the email step into email + password at once. Plan:
`docs/plans/2026-11-08-mobile-password-sign-in.md`. An account that set a
password on avibe.bot (Account security → Set or change password) can sign in
with it instead of the email code. The email code stays the primary path and
the only way to create an account in the app. After either first factor, an
account with two-step verification continues to `two-factor/*`; everyone else
reaches the instance picker. The pages reuse the A composition (`CloudScreen`,
`Brand`, `Primary`, the raised tile and card, the email card) and add no
tokens or assets. `PasswordPage` (the email step in password mode),
`ForgotPasswordPage` and `WeakPasswordNotice` live in
`ui/design/kit/mobile-app.tsx`; the email step in code mode is `WelcomeIntro`
with the `password` fixture flag, so the approved `welcome/*` frames are
unchanged.

| File / surface | States |
| --- | --- |
| `password` | `email` (both ways offered), `email-invalid` (Send verification code), `switched-empty` (password mode, no email typed yet), `default` (password mode, the typed email carried over), `filled`, `shown`, `signing-in`, `password-email-invalid` (Sign in with an invalid email), `invalid`, `unconfirmed`, `rate-limited`, `network`, `forgot`, `weak`, `small` (375 × 667, long email + error), `keyboard` (375 × 407) |

**32 approved frames / 64 theme captures** (16 states × zh/en × light/dark).
`password/email` and `password/email-invalid` supersede the email entry of
`welcome/default` and `welcome/email-invalid`.

### Why a link under the primary action

The email step keeps one primary action, Send verification code, and offers
"Sign in with password" as a link under it, inside the same card. The code
works for every account, new or existing, and avibe.bot calls the password
optional, so the password is the secondary path. A link matches avibe.bot's
own "Sign in with password" wording and the approved alternatives under the
two-step verification action. A segmented control would present the two as
equals and imply that a new email can create an account with a password, which
the app does not offer. A second full-width button would compete with the
primary action. Both actions validate the email first, so the invalid-email
copy no longer names the code.

### Interaction and accessibility handoff

- One form: "Sign in with password" switches the email step to password mode at
  once and never asks for the email first. The header stays; the card holds
  the same email field, now `autocomplete="username"` with
  `enterkeyhint="next"` (typed text carried over; focused when empty, otherwise
  the password is), then the password (`type=password`,
  `autocomplete="current-password"`, no autocapitalize or spellcheck,
  `enterkeyhint="go"`) with Forgot password? on its label row, then Sign in and
  the link "Sign in with an email code". The visible email field is the saved
  login's username, so iCloud Keychain can match the avibe.bot login through
  the app's existing `webcredentials:avibe.bot` association; there is no
  hidden field and no "Signing in as" step. The email hint (new emails create
  an account) is code-mode copy and is left out here; the footer names how to
  get a password.
- "Sign in with an email code" and Back return to code mode with the email kept
  and the password cleared. Editing the email keeps the password.
- Return in the email field moves to the password; Return in the password field
  submits. Nothing auto-submits.
- Sign in checks the email (`password-email-invalid`: the existing "Enter a
  valid email address." under the email field, which is marked) and needs a
  non-empty password; there is no client minimum length, because older
  passwords may predate avibe.bot's 8-character rule. Forgot password? and "Use
  an email code instead" check the email the same way. The page shows its own
  copy, not WebKit's validation bubble.
- Show password is one toggle button with a stable label and `aria-pressed`,
  a 44pt target inside the field. Its icon changes. The field returns to hidden
  when the page is left.
- Busy (`signing-in`) disables the field and every action, and sets `aria-busy`.
- Sign-in errors render in the slot under the password field (`role=alert`,
  linked with `aria-describedby`). Email and password are kept. `invalid` also
  marks the password field. Supabase answers `invalid_credentials` for a wrong
  email, a wrong password and an account with no password, so the copy covers
  all three. For `invalid` and `unconfirmed`, where the email code is the fix,
  an outline "Use an email code instead" button appears under the card.
- `rate-limited` disables Sign in for the provider's `Retry-After`, or 60s when
  none is given, and the label counts down. Editing stays allowed. As with the
  approved resend timer, the countdown does not promise that the server limit
  has ended. `network` keeps everything and leaves Sign in enabled.
- "Use an email code instead" and the forgot page's Send verification code
  request a code for the same email and enter the approved code step (code-only,
  auto-submit on the last digit). While that request runs, the tapped control
  shows Sending…; a failure uses the approved email-step copy in the same slot.
- Forgot password opens `forgot`. Email sign-in always works, so the code comes
  first. Open in Safari opens `https://avibe.bot/app/account/security`, already
  on the `app.openExternal` allowlist. Signed out, avibe.bot sends Safari to
  `/login?next=…`, the person signs in with email, and lands on Account security
  to choose Set or change password. avibe.bot has no separate reset page.
- `weak`: when Supabase signs the person in but reports the password as weak
  (`weak_password`, reason `pwned`), the app shows this notice once, after any
  second factor and before the picker. Continue never blocks. Change it in
  Safari opens Account security. Other reasons use the plan's copy.
- Motion: navigation between these steps follows the approved M5 rule above.
  These frames add no new moment.

### Owner decisions (approved)

1. Entry: a "Sign in with password" link under Send verification code, rather
   than a segmented control or a second button.
2. ~~Identifier-first: the password page shows the email from the email step
   with Change email, rather than a second editable email field.~~ Replaced by
   the owner on 2026-11-09 at 01:51 Asia/Shanghai, after using TestFlight build
   0.1.0 (3): "Sign in with password" must not require the email first, and the
   email and password belong on one screen. Password mode is one form with the
   email and the password.
3. Forgot password: an email code now, and a new password on avibe.bot's
   Account security page in Safari. No new avibe.bot reset page and no new
   allowlist entry.
4. Breached password: the one-time, non-blocking `weak` notice after sign-in.

### Verification boundary

Checked: design typecheck, `design:shot` for `mobile-app` (no unfixtured reads,
no console errors), production build, and a light/dark inspection of every
password capture. A screenshot proves layout only. Keychain suggestions and
saving, keyboard insets, focus and announcements, and real Supabase answers
belong to the implementation PR and a device check.

## Open-source licenses — approved

Status: **approved** by 产品伙伴 on 2026-11-11 at 19:38 Asia/Shanghai, as the
implementation contract (the owner delegated this page's review; no further
owner review). A build of the App that contains iroh ships several hundred
open-source components, and the MPL-2.0 ones (attohttpc and the uniffi family)
need a visible offer of their source code (`packaging/iroh-apple`, #3551). The
App's own surface is Account, so its new About section opens 开源许可 /
Open-source licenses. The page reuses the A composition (`CloudScreen`,
`Brand`, `PageTitle`, the raised card) and adds no tokens or assets.
`LicensesPage` and Account's `licenses` flag live in
`ui/design/kit/mobile-app.tsx`.

| File / surface | States |
| --- | --- |
| `licenses` | `entry` (Account with the About row), `default` (first screen), `group-open` (390 × 2400 whole page, MIT group expanded) |

6 frames / 12 theme captures (3 states × zh/en × light/dark).

Approved decisions (revised after 产品伙伴's review on 2026-11-11):

- Entry stays Account → About → Open-source licenses.
- The MPL-2.0 source offer stays at the top. A crate family sharing one version
  and upstream commit is one line: "uniffi and 8 related crates, 0.31.2", the
  crate names in small type (each linking its crates.io archive), and one
  Source code link.
- Links are labels (crates.io archive / Source code) with an external-link
  icon; raw URLs are never shown and the targets are unchanged.
- Each distinct license text appears once, with the components it covers
  ("MIT · 214 components"), collapsed until opened.
- License texts are reflowed: hard wraps become spaces and a blank line ends a
  paragraph. No monospace.

Fixture names and counts are illustrative; the App renders the published
notices. Implementation keeps two invariants the frames cannot show: texts
group only when substantively identical (two MIT texts with different
copyright lines are two groups, both titled MIT, each keeping its components,
versions and attribution), and reflowing never merges a heading underline
(`----`, `====`) into the text or alters license terms or copyright lines.
