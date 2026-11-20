# App Lock (iOS I7-7A, Desktop D7-7A)

Status: approved by the owner on 2026-11-20 with every recommended decision. iOS and Desktop are
separate PRs; this file and `ui/src/lib/appLockContract.ts` are the contract both follow.

## Behavior (both shells)

- One per-device choice: Off (default) / Immediately / After 1 minute / After 5 minutes. The
  native shell stores it; it is not synced to the account or an instance, and signing out does not
  change it. It is shown in Settings › General only when the shell offers App Lock.
- Every change is saved only after one successful `LAContext.evaluatePolicy(.deviceOwnerAuthentication)`.
  Cancel or failure changes nothing. Choosing the stored value again is a no-op success.
- Leaving: iOS, the app enters the background (an inactive glance such as Control Center or a
  system sheet is not leaving). Mac, the app resigns active, the screen locks or sleeps, or the
  session switches user. Elapsed time uses a monotonic clock that includes sleep
  (`CLOCK_MONOTONIC_RAW`); a relaunch is a cold start.
- Locks: on cold start, and when the time away reaches the chosen threshold (Immediately = 0 s).
  The Mac shell locks when the threshold passes while away, so nothing shows on return.
- Unlock: a fresh `LAContext` with `.deviceOwnerAuthentication` each time, so the system falls back
  to the device passcode / Mac password. The shell asks once automatically while it is in front;
  after a cancel it waits for the button. No custom PIN.
- A device without a passcode (Mac user without a password): `available` is false, the lock does
  not engage, and Settings explains why; the stored choice applies again once a passcode exists.
- The lock covers UI only. Background work (sync, push, reminders, the Runtime and Agents, voice
  capture started by the Action Button) keeps running. Notification previews follow system settings.
- The lock screen is native (SwiftUI on iOS, AppKit on Mac). Visual spec: `ui/design: app-lock/lock/*`.
  Its words are `appLock.lock.*` in `ui/src/i18n/{en,zh}.json` (`appLock.lock.iphone.*` for the
  iPhone only); a shell mirrors those keys and never words them itself.
  Settings panel: `ui/design: app-lock/settings/*`.

## Wire contract

Exact types, enums, parsers and example payloads live in `ui/src/lib/appLockContract.ts`; a shell's
tests decode those fixtures.

| | iOS | Desktop |
| --- | --- | --- |
| Discovery | `avibeNative.capabilities.appLock >= 1` | `window.__AVIBE_DESKTOP_APP_LOCK__ === true` |
| Status | `appLock.status` `{}` → `AppLockStatus` | `invoke('app_lock_status')` → `AppLockStatus` |
| Change | `appLock.setPolicy` `{ policy }` → `AppLockSetResult` | `invoke('app_lock_set_policy', { policy })` → `AppLockSetResult` |
| Who may call | instance main frame (existing bridge policy) | main Workbench capability only |
| Unknown policy | bridge error `invalid_argument` | rejected invoke |

`AppLockStatus = { policy, available, biometry, biometryState }`:

- `policy`: `off | immediately | oneMinute | fiveMinutes`.
- `available`: `canEvaluatePolicy(.deviceOwnerAuthentication)` succeeds.
- `biometry`: `faceID | touchID | opticID | none` (`LAContext.biometryType`).
- `biometryState`: `usable | notEnrolled | denied | unavailable | lockout`, from
  `canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics)`: success → `usable`;
  `biometryNotEnrolled` → `notEnrolled`; `biometryLockout` → `lockout`; `biometryNotAvailable` with a
  biometric `biometryType` on iOS → `denied`; anything else (no hardware, a closed lid's
  `systemCancel`) → `unavailable`. Desktop never reports `denied`.

`AppLockSetResult = { ok: true, status } | { ok: false, reason, status }` with `reason`
`cancelled | failed | unavailable | busy`; `status` is always the state after the call.

There is no `unlock` method: the lock screen is native and the page is not visible while locked.
