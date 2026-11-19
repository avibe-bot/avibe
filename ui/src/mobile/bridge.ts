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
      /**
       * The sign-ins avibe.bot offers the App now (`mobile_providers` in its auth config). Native
       * adds it to every signed-out session answer; absent means the page keeps what it had.
       */
      providers?: ('google' | 'github' | 'apple')[];
    }
  | {
      status: 'signedIn';
      account: MobileAccount;
      instances: MobileInstance[];
      selected_instance_id: string | null;
      /**
       * This account was just created by a Google, GitHub or Apple sign-in in the App, as the backend
       * attested; native keeps it for this account generation only. A hint for the empty state.
       */
      new_account?: true;
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

/** Why avibe.bot still accepted a password that no longer meets its rules. */
export type MobileWeakPasswordReason = 'length' | 'characters' | 'pwned';

/** `auth.verifyOtp`'s state, plus `weak_password` when Supabase reported one. */
export type MobilePasswordSignInResult = MobileSessionState & {
  weak_password?: { reasons: MobileWeakPasswordReason[] };
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

/**
 * Signs in with the account's avibe.bot password. Errors:
 * `invalid_credentials`, `email_not_confirmed`, `rate_limited` (with
 * `retry_after_seconds` when avibe.bot sent one), and `network`.
 */
export const signInMobileWithPassword = (
  email: string,
  password: string,
): Promise<MobilePasswordSignInResult> =>
  call('auth.signInWithPassword', { email, password });

/**
 * Google or GitHub in the system sign-in window, or Sign in with Apple's sheet, then the backend's
 * one-time exchange, natively.
 * Resolves like `verifyMobileOtp`. Errors: `provider_cancelled` (show nothing),
 * `provider_unavailable`, `provider_email_unverified`, `invalid_or_expired`, `rate_limited`
 * (with `retry_after_seconds`), and `network`.
 */
/**
 * The sign-ins avibe.bot offers the App: read now when that answers within a few seconds,
 * otherwise the last list it advertised. Without `providers`, none was ever read.
 */
export const readMobileProviders = (): Promise<{ providers?: ('google' | 'github' | 'apple')[] }> =>
  call('auth.providers');

export const signInMobileWithProvider = (
  provider: 'google' | 'github' | 'apple',
): Promise<MobileSessionState> =>
  call('auth.signInWithProvider', { provider });

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

/** The avibe.bot pages the App shows over this page. Each has one fixed address, set natively. */
export type MobilePage = 'accountDeletion' | 'privacyPolicy';

/**
 * Shows one of the App's avibe.bot pages and resolves once it closes. The account deletion page
 * uses the App's sign-in; `completed` is true when it reported the account deleted. Whether the
 * account is still there is the session's answer, which the caller reads next.
 */
export const openMobilePage = (page: MobilePage): Promise<{ completed: boolean }> =>
  call('app.openPage', { page });

/**
 * Closes an account page that native opened over a live instance
 * (`#runtime-account`) and shows that instance again, unchanged. Rejects with
 * `runtime_unavailable` once a switch, refresh, or sign-out has closed it.
 */
export const returnToMobileInstance = (): Promise<null> =>
  call('app.returnToInstance');

/**
 * Closes the instance sheet native opened over the selected instance (`#sheet`), which is shown
 * again unchanged. Rejects with `runtime_unavailable` when no such sheet is presented.
 */
export const dismissMobileSheet = (): Promise<null> =>
  call('app.dismiss');

/** The page native opens as a sheet over the selected instance (the avatar). */
export const SHEET_HASH = '#sheet';
/** The same sheet opened from the instance's name: the instance list without the account. */
export const SHEET_INSTANCES_HASH = '#sheet-instances';
/** The same sheet opened from the avatar where the header names the instance: the Account page as its root. */
export const SHEET_ACCOUNT_HASH = '#sheet-account';

export const isSheetHash = (hash: string): boolean =>
  hash === SHEET_HASH || hash === SHEET_INSTANCES_HASH || hash === SHEET_ACCOUNT_HASH;

/**
 * avibe.bot's Account security page: where an account turns on two-step
 * verification and sets or changes its password.
 */
export const ACCOUNT_SECURITY_URL = 'https://avibe.bot/app/account/security';
