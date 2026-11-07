# Mobile two-step verification

Status: proposed. The design frames are `mobile-app/two-factor/*`. The
implementation follows the owner's approval of this plan and those frames.

## Background

avibe.bot owns a second factor: passkeys, an authenticator app (TOTP), and
recovery codes (avibe-backend #315, `docs/plans/2026-11-06-two-step-verification.md`
there). A proof is bound to one Supabase session. Session-backed APIs refuse a
session that still owes it.

The iOS app signs in with an email code through the hidden account transport,
then calls `GET /api/mobile/session`. For an account with two-step
verification, that call now returns `401`, and the app reads `401` as signed
out. Such accounts can't use the app today.

## Goal

After the email code, an account with two-step verification passes its second
factor in the app, using a passkey, an authenticator code, or a recovery code.
An account that must turn it on (platform administrators) is sent to avibe.bot
to set it up. As on the web, the proof belongs to this app's Supabase session,
so the app asks once per sign-in. The proof stays valid while that session
lives: 14 days idle, 90 days at most.

## Flow

1. `auth.verifyOtp` runs the existing OTP verify, then `/auth/callback`, then
   `GET /api/mobile/session`.
2. When the first factor is valid and the second is owed, the backend answers
   `403 {"error":"two_factor_required","gate":"challenge"|"enroll","methods":["passkey","totp"]}`.
   A `401` still means signed out. Nothing rotates the store on `403`.
3. The bundled page shows `two-factor/*`:
   - **Passkey** (first when the account has one). The transport fetches
     options from `POST /api/two-factor/passkey-challenge`. Native code runs
     `ASAuthorizationPlatformPublicKeyCredentialProvider` for relying party
     `avibe.bot`, and the transport posts the assertion to
     `POST /api/two-factor/verify`. This needs the `webcredentials:avibe.bot`
     associated domain and the site association file below. Cancelling, or
     having no passkey on this device, offers the code paths.
   - **Authenticator code / recovery code.** The transport posts to
     `POST /api/two-factor/verify`.
   - **Success.** The backend re-issues `avibe_session` with the proof into
     the account's WebKit store. The transport reads `/api/mobile/session`
     again, and the instance picker follows.
   - **Setup required (`gate: enroll`).** `app.openExternal` opens
     `https://avibe.bot/app/account/security` in Safari. "I've turned it on"
     runs `session.refresh`. That turns the gate into `challenge` for this
     session, because a method set up elsewhere is not a proof here.
4. A cold launch's `session.bootstrap` and `session.refresh` treat
   `two_factor_required` as in step 2.
5. Instance entry is unchanged. The instance's OIDC authorize runs on
   avibe.bot in the runtime WebView, which shares the account store, so the
   proof is present.

Tokens stay where they are today: in the isolated transport world. The
passkey options and assertion are not secret. They pass between the transport
and native code only.

## Bridge contract

- `auth.verifyOtp`, `session.bootstrap`, and `session.refresh` may return
  `{status: 'twoFactorRequired', email, gate: 'challenge' | 'enroll', methods: ('passkey' | 'totp')[]}`.
- New `auth.verifySecondFactor({method: 'totp' | 'recovery', code})` returns
  the signed-in state. Its errors are `invalid`, `rate_limited` (with
  `retry_after_seconds`), and `network`.
- New `auth.verifyPasskey()` returns the signed-in state. Its errors are
  `passkey_cancelled`, `passkey_unavailable`, `invalid`, `rate_limited`, and
  `network`.
- `app.openExternal` adds `https://avibe.bot/app/account/security` to its
  closed allowlist.
- "Sign in with a different account" is the existing `session.logout`.

## Backend changes (avibe-backend)

- `GET /api/mobile/session` returns the `403 two_factor_required` body above.
  Other session-backed APIs keep `401`.
- `/.well-known/apple-app-site-association`:
  `{"webcredentials":{"apps":["68B2SQ6YK3.bot.avibe.ios"]}}`, served as
  `application/json` with no redirect. The Team ID is the one the desktop app
  and avault are signed with.

## Owner steps

- Enable Associated Domains for the `bot.avibe.ios` App ID in the Apple
  Developer account. The app adds the
  `com.apple.developer.associated-domains` entitlement with
  `webcredentials:avibe.bot`.

## Decisions for the owner

1. Accounts that must turn on two-step verification set it up on avibe.bot
   (Safari), not in the app.
2. When the account has a passkey, the passkey comes first, and the
   authenticator code and recovery code are alternatives.
3. The app asks once per app sign-in, not per launch.

## Verification boundary

- Backend tests cover the `403` contract and the site association response.
- Hermetic transport tests cover the new fixed scripts, redirect rejection,
  and error mapping. Bridge and page tests cover the new states.
- A native passkey needs a real device, or a simulator with the associated
  domain in developer mode, signed in to an account with a passkey. That check
  is manual and is recorded in the implementation PR.
