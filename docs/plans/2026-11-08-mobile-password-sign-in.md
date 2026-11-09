# Mobile password sign-in

Status: approved by the owner on 2026-11-08 at 12:46 Asia/Shanghai, with the
four decisions below, and implemented as specified; `ios/README.md` (Password
sign-in) records the shipped contract. On 2026-11-09 at 01:51 Asia/Shanghai
the owner replaced decision 2 (identifier-first) with one form, after using
TestFlight build 0.1.0 (3); the flow below describes the one form. The design
frames are `mobile-app/password/*`.

## Background

The iOS app signs in with an email code only. The code is code-only (no link)
and auto-submits on its last digit. avibe.bot's web login also offers a
password (`app/login/login-form.tsx` in avibe-backend). Its modes are email
link (default), password, and password sign-up. Password sign-in calls
Supabase `signInWithPassword`, posts the session to `/auth/callback`, and maps
errors this way: an unreachable service shows `authServiceUnreachable`,
"Invalid login" shows "Invalid email or password.", and anything else shows
the provider message.

avibe.bot treats the password as optional. A signed-in account sets or changes
it on Account security (`/app/account/security`, "Set or change password").
The rules there are at least 8 characters and no password found in a data
breach (Supabase leaked-password protection). avibe.bot has no
forgot-password or reset-by-email page.

Supabase answers `invalid_credentials` for a wrong email, a wrong password,
and an account that never set a password. It answers `email_not_confirmed`
when the password is right but the email was never confirmed. When the
password is right but no longer meets the rules, it signs the person in and
adds `weak_password` with reasons (`length`, `characters`, `pwned`).

## Goal

An account with an avibe.bot password can sign in to the app with email and
password, and iCloud Keychain can fill it. The email code stays the primary
path and the only way to create an account in the app. Two-step verification,
instance selection, and instance entry are unchanged.

Not in scope: password sign-up in the app, setting or changing a password in
the app, and a reset-by-email page on avibe.bot.

## Flow

1. **Email step** (`password/email`). Send verification code stays primary.
   "Sign in with password" is a link under it. It switches the step to
   password mode at once and never asks for the email first. Send verification
   code validates the email; the invalid copy becomes "Enter a valid email
   address."
2. **Password mode** (`password/switched-empty` … `network`). One form: the
   same email field, now `autocomplete="username"` (typed text carried over,
   focused when empty), above the `current-password` field with a show/hide
   toggle, then Sign in and "Sign in with an email code", which returns to code
   mode with the email kept. Return in the email field moves to the password;
   Return in the password submits. Sign in checks the email
   (`password/password-email-invalid`) and needs a non-empty password, then
   calls `auth.signInWithPassword({email, password})`.
3. **Result.** The result is the same as `auth.verifyOtp`. `signedIn` goes to
   the picker. `twoFactorRequired` goes to `two-factor/*`, whose existing
   contract is unchanged.
4. **Weak password** (`password/weak`). When the result carries
   `weak_password`, the page keeps it through any second factor. Once signed
   in, it shows the notice once before the picker. Continue never blocks.
   "Change it in Safari" opens Account security. The `pwned` copy is drawn.
   Other reasons use "This password no longer meets avibe.bot's rules (at least
   8 characters). Set a new one in Account security." / "这个密码已不符合
   avibe.bot 的密码要求（至少 8 位）。请在「账号安全」里设置新密码。"
5. **Use an email code instead.** This calls the existing `auth.requestOtp` for
   the same email and enters the approved code step. The password is cleared.
6. **Forgot password** (`password/forgot`). Send verification code works as in
   step 5. Open in Safari calls `app.openExternal` with
   `https://avibe.bot/app/account/security`. That URL is already on the closed
   allowlist (`ACCOUNT_SECURITY_URL`). Signed out, avibe.bot's middleware
   redirects to `/login?next=/app/account/security`. The person signs in there
   with email and chooses Set or change password.

Returning to code mode (the link or Back) and leaving the step clear the
password and its error. The email is kept, and editing it keeps the password.
Forgot password and "Use an email code instead" check the email first.

## Bridge contract

- New `auth.signInWithPassword({email, password})` returns `MobileSessionState`,
  like `auth.verifyOtp`. When Supabase reported a weak password, the result
  adds `weak_password: {reasons: ('length' | 'characters' | 'pwned')[]}`.
- Errors:
  - `invalid_credentials`: a token-stage `400` with code `invalid_credentials`,
    and any other token-stage `4xx`.
  - `email_not_confirmed`: a token-stage `400` with that code.
  - `rate_limited`: a `429`, with `retry_after_seconds` from `Retry-After` when
    present. The page then waits 60 seconds by default.
  - `network`: timeouts, aborts, malformed responses, `5xx`, and callback or
    session failures. This matches the OTP path.
  - A session `401` stays the explicit signed-out answer.
- `app.openExternal` needs no new entry.
- The page's error mapping adds the three password copies drawn in the frames.
  Unknown codes keep the existing network presentation.

## Transport

The hidden account transport adds one fixed script, modeled on the OTP verify
script, with the same deadline, origin, and redirect rules:

1. `POST {supabase_url}/auth/v1/token?grant_type=password`, with the
   configured anon key as `apikey` and bearer, `credentials: "omit"`, and the
   JSON body `{email, password}`. The Supabase origin is the one that already
   serves `/auth/v1/otp` and `/auth/v1/verify`.
2. On success, post `access_token` and `refresh_token` to the existing
   `/auth/callback`, then run the shared `/api/mobile/session` discovery. That
   discovery already reports `403 two_factor_required`. Return `weak_password`
   reasons with the result.
3. The password is a transient script argument, like the tokens today. It is
   never logged or persisted, never echoed in an error or message, and never
   returned to the page. Native bridge logging must redact the `password`
   parameter. The page holds it only in field state, which clears on success
   and when the step is left.
4. A failed password sign-in rotates to a fresh store before another attempt,
   as a failed OTP verify does.

## Backend (avibe-backend)

Nothing is required:

- The Supabase email provider already allows password grants, because the web
  uses them.
- `/auth/callback` already accepts posted tokens.
- `/api/mobile/session` already reports the second factor.
- `/app/account/security` already sets passwords.
- The `apple-app-site-association` `webcredentials` entry already associates
  `avibe.bot` passwords with the app.

A dedicated reset-by-email page would be new backend work. It is not part of
this plan (decision 3).

## Owner decisions (approved)

1. Entry: a "Sign in with password" link under Send verification code, rather
   than a segmented control or a second button.
2. ~~Identifier-first: the password page shows the email from the email step
   with Change email, rather than a second editable email field.~~ Replaced on
   2026-11-09 at 01:51 Asia/Shanghai: "Sign in with password" switches the
   email step to one form with the email and the password, without asking for
   the email first.
3. Forgot password: an email code now, and a new password on avibe.bot's
   Account security page in Safari. No new reset page and no new allowlist
   entry.
4. Breached password: a one-time, non-blocking notice after sign-in.

## Verification boundary

- Design PR: design typecheck, `design:shot` for `mobile-app`, production
  build, and a light/dark capture inspection.
- Implementation:
  - Hermetic transport tests cover the fixed script, redirect rejection, the
    error mapping above, `Retry-After`, `weak_password` pass-through, and the
    absence of the password from results, errors, and logs.
  - Bridge and page tests cover each `password/*` state. They also cover the
    password clearing on returning to code mode and leaving the step; the email
    code fallback reusing `auth.requestOtp`; and the weak notice following a
    second factor.
- Manual on a signed device:
  - The iCloud Keychain suggestion and fill on the password field.
  - Whether iOS offers to save a newly typed password after the in-page sign-in.
  - The `keyboard` composition on a 375 × 667 device.
  - A real `invalid_credentials` for an account without a password.
