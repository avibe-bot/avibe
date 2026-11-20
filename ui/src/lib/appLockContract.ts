// App Lock wire contract (iOS I7-7A, Desktop D7-7A). Both shells answer exactly these
// shapes; the Web UI reads them only through `parseAppLockStatus` / `parseAppLockSetResult`.
// Narrative and transport details: docs/plans/app-lock.md.
//
// Transport
// - iOS: capability `appLock` version 1 on `window.avibeNative`; methods `appLock.status`
//   (params `{}`) and `appLock.setPolicy` (params `{ policy }`). Instance main frame only.
// - Desktop: `window.__AVIBE_DESKTOP_APP_LOCK__ === true`; Tauri commands `app_lock_status`
//   (no args) and `app_lock_set_policy` (args `{ policy }`), granted to the main Workbench only.
// Both return the JSON below (camelCase keys, string enums).

/** The one stored choice. `off` is the default. */
export const APP_LOCK_POLICIES = ['off', 'immediately', 'oneMinute', 'fiveMinutes'] as const;
export type AppLockPolicy = (typeof APP_LOCK_POLICIES)[number];

/** Biometric hardware the device has (LocalAuthentication `biometryType`). */
export const APP_LOCK_BIOMETRY_KINDS = ['faceID', 'touchID', 'opticID', 'none'] as const;
export type AppLockBiometry = (typeof APP_LOCK_BIOMETRY_KINDS)[number];

/**
 * Whether that biometric can be used right now. Anything but `usable` still unlocks with the
 * device passcode / Mac password through `.deviceOwnerAuthentication`.
 * - `usable`: biometric evaluation is possible.
 * - `notEnrolled`: no face / fingerprint enrolled (`LAError.biometryNotEnrolled`).
 * - `denied`: iOS only; the user turned Face ID off for Avibe (`biometryNotAvailable` with a
 *   biometric `biometryType`).
 * - `unavailable`: no biometric hardware, or none reachable now (a Mac with the lid closed).
 * - `lockout`: too many failed attempts (`LAError.biometryLockout`); the system asks for the passcode.
 */
export const APP_LOCK_BIOMETRY_STATES = ['usable', 'notEnrolled', 'denied', 'unavailable', 'lockout'] as const;
export type AppLockBiometryState = (typeof APP_LOCK_BIOMETRY_STATES)[number];

export type AppLockStatus = {
  /** The stored choice, even while `available` is false. */
  policy: AppLockPolicy;
  /**
   * The device has a passcode (iPhone) or the user has a login password (Mac):
   * `canEvaluatePolicy(.deviceOwnerAuthentication)` succeeds. When false the lock does not
   * engage whatever `policy` says, Settings shows the choice as Off and explains why, and the
   * stored policy takes effect again once a passcode exists.
   */
  available: boolean;
  biometry: AppLockBiometry;
  biometryState: AppLockBiometryState;
};

/**
 * Why `setPolicy` changed nothing.
 * - `cancelled`: the user or the system dismissed the verification.
 * - `failed`: verification did not succeed, or the shell could not save.
 * - `unavailable`: `available` is false; no verification was attempted.
 * - `busy`: another verification (an unlock or another change) is already showing.
 */
export const APP_LOCK_SET_FAILURES = ['cancelled', 'failed', 'unavailable', 'busy'] as const;
export type AppLockSetFailure = (typeof APP_LOCK_SET_FAILURES)[number];

/**
 * `setPolicy({ policy })`: every change is saved only after one successful
 * `.deviceOwnerAuthentication`. Asking for the policy already stored succeeds without verifying.
 * `status` is always the state after the call, so the page never has to re-read it.
 * An unknown `policy` is a transport error (iOS `invalid_argument`; Desktop a rejected invoke),
 * not a result.
 */
export type AppLockSetResult =
  | { ok: true; status: AppLockStatus }
  | { ok: false; reason: AppLockSetFailure; status: AppLockStatus };

const isOneOf = <T extends string>(values: readonly T[], value: unknown): value is T =>
  typeof value === 'string' && (values as readonly string[]).includes(value);

export function isAppLockPolicy(value: unknown): value is AppLockPolicy {
  return isOneOf(APP_LOCK_POLICIES, value);
}

/** The status a shell sent, or null when it is not this contract's shape. */
export function parseAppLockStatus(value: unknown): AppLockStatus | null {
  if (!value || typeof value !== 'object') return null;
  const { policy, available, biometry, biometryState } = value as Record<string, unknown>;
  if (!isAppLockPolicy(policy) || typeof available !== 'boolean'
    || !isOneOf(APP_LOCK_BIOMETRY_KINDS, biometry) || !isOneOf(APP_LOCK_BIOMETRY_STATES, biometryState)) {
    return null;
  }
  return { policy, available, biometry, biometryState };
}

/** The result a shell sent, or null when it is not this contract's shape. */
export function parseAppLockSetResult(value: unknown): AppLockSetResult | null {
  if (!value || typeof value !== 'object') return null;
  const { ok, reason } = value as Record<string, unknown>;
  const status = parseAppLockStatus((value as Record<string, unknown>).status);
  if (!status) return null;
  if (ok === true) return { ok: true, status };
  if (ok === false && isOneOf(APP_LOCK_SET_FAILURES, reason)) return { ok: false, reason, status };
  return null;
}

/** Example payloads both shells' tests decode; keep them in step with the types above. */
export const APP_LOCK_FIXTURES = {
  iphoneOff: { policy: 'off', available: true, biometry: 'faceID', biometryState: 'usable' },
  iphoneFiveMinutes: { policy: 'fiveMinutes', available: true, biometry: 'faceID', biometryState: 'usable' },
  iphoneFaceIdDenied: { policy: 'oneMinute', available: true, biometry: 'faceID', biometryState: 'denied' },
  iphoneNoPasscode: { policy: 'fiveMinutes', available: false, biometry: 'faceID', biometryState: 'notEnrolled' },
  macOneMinute: { policy: 'oneMinute', available: true, biometry: 'touchID', biometryState: 'usable' },
  macLidClosed: { policy: 'fiveMinutes', available: true, biometry: 'touchID', biometryState: 'unavailable' },
  macNoTouchId: { policy: 'immediately', available: true, biometry: 'none', biometryState: 'unavailable' },
  setChanged: { ok: true, status: { policy: 'fiveMinutes', available: true, biometry: 'faceID', biometryState: 'usable' } },
  setCancelled: { ok: false, reason: 'cancelled', status: { policy: 'off', available: true, biometry: 'touchID', biometryState: 'usable' } },
  setUnavailable: { ok: false, reason: 'unavailable', status: { policy: 'off', available: false, biometry: 'none', biometryState: 'unavailable' } },
} as const satisfies Record<string, AppLockStatus | AppLockSetResult>;
