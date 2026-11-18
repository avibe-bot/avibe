# Shared sign-in for the Desktop and iPhone apps (#3946)

Status: contract draft, 2026-11-18. Owner: #3946 lead. Frozen base: master `f5d8af27`
(not the live tip; master was `c7953082` at 17:08), stacked locally on #3945 head
`a9885d69` (not pushed; rebased once #3945 merges).

## Goal

One sign-in flow in `ui/` that both shells render, with one small adapter per
shell to its native side. Both apps offer email code, password, two-step
(passkey, authenticator code, recovery code), passkey sign-in without an email,
Google, GitHub and Apple. Credentials, ceremonies and cookies stay native.

## Facts this contract rests on (checked 2026-11-18)

| Dependency | State |
| --- | --- |
| #3945 (iOS Google/GitHub/Apple) | OPEN at `a9885d69`, CI green, Codex clean. Blocked by its own list: Apple capability/profile, Product approval, backend exchange. |
| Backend one-time exchange (`/api/mobile/auth/{start,browser,exchange,apple}`, `/mobile/auth/complete`) | Not found in the searched refs (avibe-backend `main` `884a13c`, remote branches, PRs, local worktrees); an anonymous probe of `/api/mobile/auth/start` returned 404. This does not prove nobody has unpushed work: #3945 cites `_tmp/ios-login/contract-backend.md`, which is in no repository. The backend's existing route is Supabase tokens into `POST /auth/callback` or `POST /api/desktop/session`; provider sign-in follows it (`2026-11-18-shared-sign-in-providers.md`). |
| Backend primary passkey (`/api/passkey-primary/{options,verify,cancel}`) | Merged. Cookie-bound flow, RP and origin from deployment configuration (the plan keeps RP `avibe.bot`), no allow-list. Gated by `PRIMARY_PASSKEY_ENABLED` and `PRIMARY_PASSKEY_AUDIENCE` (code default `qa`); whether production enables it is unknown and is checked only through authorized read-only metadata. Native handoff spec: backend `docs/plans/passkey-sign-in.md` §6. |
| Passkey sign-in eligibility | `lib/auth/passkey-session-adapter.ts` admits only `email`, `google`, `github` identities; an account with an `apple` identity is refused (`verification_failed`). |
| Web providers | Production `/api/config/auth` (public fields) reports Google and GitHub on and Apple off; the repo's Supabase config has `[auth.external.apple] enabled=false`. Public fields do not establish secret or provider-side configuration. |
| Identity linking | No Avibe linking policy found in the searched code; `enable_manual_linking = false` in the repo's Supabase config. Which identities reach one account (including Hide My Email and existing Google/GitHub/email accounts) is the backend's existing rule, to be confirmed with the exchange contract. This change does not alter it. |
| Desktop platforms | macOS arm64 and x86_64, Windows x64 (NSIS). No Linux build. Passkey second factor already runs natively on both (AuthenticationServices; `webauthn.dll` with clientData origin `https://avibe.bot`). Scheme `avibe://` is registered with single-instance forwarding. |
| iOS distribution | TestFlight signs only from `master`. |

## Shared module

`ui/src/sign-in/` owns the steps, order, copy (`signIn.*`), error mapping,
cooldowns and operation ownership. Each shell supplies:

- **Adapter** (`SignInAdapter`), promise-based, one method per user action:
  `requestCode`, `verifyCode`, `signInWithPassword`, `signInWithProvider`,
  `signInWithPasskey`, `verifySecondFactor`, `verifySecondFactorPasskey`,
  plus `methods` (what this build can run). Each resolves to the next
  `SignInStep` or rejects with a canonical `SignInError`.
- **Chrome**: the frame a step is drawn in (iPhone: `CloudScreen` + `Brand`;
  Desktop: the account window column) and the shell's own intro and exits
  (iPhone welcome hero; Desktop registration line and "continue locally").
- **Enrollment gate** (`gate: 'enroll'`): the shell's existing screen, unchanged
  (iPhone links to Account security; Desktop enrolls TOTP in-app and protects
  recovery codes).

`SignInStep` = `signedOut` | `codeSent{email}` | `secondFactor{email, gate, methods}` |
`signedIn{newAccount?, weakPassword?}`. The shell owns the authoritative step
(iPhone: session results; Desktop: native state broadcast) and passes it in; a
step change the flow did not start retires its pending operation, so a late
result never reaches the screen.

Canonical errors: `cancelled` and `superseded` (silent), `invalid_email`,
`invalid_code`, `invalid_credentials`, `email_not_confirmed`,
`provider_unavailable`, `provider_email_unverified`, `provider_failed`,
`passkey_unavailable`, `passkey_failed`, `rate_limited{retryAfterSeconds}`,
`network`, `busy`, `unavailable`. Adapters map native codes; the flow maps
canonical code + operation to copy.

## Native work

| Shell | Passkey sign-in | Google / GitHub | Apple |
| --- | --- | --- | --- |
| iPhone | New `auth.signInWithPasskey` per backend §6: fixed transport scripts for options/verify, discoverable assertion (no allow-list), account transition + generation fence, fresh store on failure. | #3945 | #3945 (native sheet) |
| Desktop | New `sign_in_with_passkey` action: fresh `NativeSession`, options → `Passkeys::assert` with an empty allow-list → verify → session discovery; same fencing as `verify_passkey`. macOS and Windows. | Supabase PKCE in the system browser, then the existing `establish()` (`POST /api/desktop/session`). See `2026-11-18-shared-sign-in-providers.md`. | Same route, Supabase's web Apple. |

## Behavior change

- Before: Desktop one card (email field, password toggle, local exit); iPhone
  welcome → email → code/password → two-step. After: both run the iPhone steps
  and copy; Desktop keeps its window chrome, registration line, local exit and
  in-app enrollment.
- Unchanged: accounts, instance selection, sign-out, account deletion,
  recovery-code protection, I3/HealthKit generation ownership, the instance
  WebView interception from #3945.

## Validation

One Vitest matrix run against both adapters with fake natives: every entry →
second step → success, failure, cancel, retry, late result after an account
change, rate limit. Native: Rust coordinator tests for passkey sign-in fencing;
XCTest for the iOS operation. Real providers, Apple's sheet and passkeys on
devices are acceptance steps, not CI claims.
