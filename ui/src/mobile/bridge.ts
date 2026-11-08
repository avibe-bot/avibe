export type MobileAccount = {
  id: string;
  email: string;
};

export type MobileInstance = {
  id: string;
  name: string;
  host: string;
  url: string;
  relationship: 'owner' | 'shared';
  owner_email: string | null;
  available: boolean;
};

export type MobileSecondFactorMethod = 'passkey' | 'totp';

export type MobileSessionState =
  | {
      status: 'signedOut';
    }
  | {
      status: 'signedIn';
      account: MobileAccount;
      instances: MobileInstance[];
      selected_instance_id: string | null;
    }
  | {
      /** The email code passed; this session still owes avibe.bot's second factor. */
      status: 'twoFactorRequired';
      /** The account's email when native knows it; the page then omits that line. */
      email: string | null;
      /** `enroll`: the account must turn two-step verification on at avibe.bot first. */
      gate: 'challenge' | 'enroll';
      /** The account's method kinds, passkey first; empty for `enroll`. */
      methods: MobileSecondFactorMethod[];
    };

export type MobileBridgeError = {
  code: string;
  message: string;
  retry_after_seconds?: number;
};

interface AvibeMobile {
  readonly contract: 1;
  call<T>(method: string, params?: Record<string, unknown>): Promise<T>;
}

declare global {
  interface Window {
    /** Installed only in the app-bundled onboarding WebView. */
    readonly avibeMobile?: AvibeMobile;
  }
}

const bridge = (): AvibeMobile | null => {
  if (typeof window === 'undefined') return null;
  const candidate = window.avibeMobile;
  if (candidate?.contract !== 1 || typeof candidate.call !== 'function') return null;
  return candidate;
};

const unavailable = (): MobileBridgeError => ({
  code: 'unavailable',
  message: 'The mobile account bridge is unavailable.',
});

export const normalizeMobileBridgeError = (error: unknown): MobileBridgeError => {
  if (
    typeof error === 'object'
    && error !== null
    && typeof Reflect.get(error, 'code') === 'string'
    && typeof Reflect.get(error, 'message') === 'string'
  ) {
    const retryAfter = Reflect.get(error, 'retry_after_seconds');
    return {
      code: Reflect.get(error, 'code') as string,
      message: Reflect.get(error, 'message') as string,
      ...(typeof retryAfter === 'number' ? { retry_after_seconds: retryAfter } : {}),
    };
  }
  return { code: 'failed', message: 'The mobile account operation failed.' };
};

const call = async <T>(
  method: string,
  params: Record<string, unknown> = {},
): Promise<T> => {
  const mobile = bridge();
  if (!mobile) throw unavailable();
  return mobile.call<T>(method, params);
};

export const bootstrapMobileSession = (): Promise<MobileSessionState> =>
  call('session.bootstrap');

export const requestMobileOtp = (
  email: string,
): Promise<{ cooldown_seconds: number }> =>
  call('auth.requestOtp', { email });

export const verifyMobileOtp = (
  email: string,
  code: string,
): Promise<MobileSessionState> =>
  call('auth.verifyOtp', { email, code });

export const refreshMobileSession = (): Promise<MobileSessionState> =>
  call('session.refresh');

/** Errors: `invalid`, `rate_limited` (with `retry_after_seconds`), `network`. */
export const verifyMobileSecondFactor = (
  method: 'totp' | 'recovery',
  code: string,
): Promise<MobileSessionState> =>
  call('auth.verifySecondFactor', { method, code });

/**
 * Runs the system passkey sheet natively. Errors: `passkey_cancelled`,
 * `passkey_unavailable`, `invalid`, `rate_limited`, `network`.
 */
export const verifyMobilePasskey = (): Promise<MobileSessionState> =>
  call('auth.verifyPasskey');

export const selectMobileInstance = (
  id: string,
): Promise<{ instance: MobileInstance }> =>
  call('session.selectInstance', { id });

export const logoutMobileSession = (): Promise<MobileSessionState> =>
  call('session.logout');

export const openMobileExternal = (url: string): Promise<{ opened: boolean }> =>
  call('app.openExternal', { url });

/**
 * Closes an account page that native opened over a live instance
 * (`#runtime-account`) and shows that instance again, unchanged. Rejects with
 * `runtime_unavailable` once a switch, refresh, or sign-out has closed it.
 */
export const returnToMobileInstance = (): Promise<null> =>
  call('app.returnToInstance');

/** Where an account that must turn on two-step verification sets it up. */
export const ACCOUNT_SECURITY_URL = 'https://avibe.bot/app/account/security';
