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

export type MobileSessionState =
  | {
      status: 'signedOut';
    }
  | {
      status: 'signedIn';
      account: MobileAccount;
      instances: MobileInstance[];
      selected_instance_id: string | null;
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

export const selectMobileInstance = (
  id: string,
): Promise<{ instance: MobileInstance }> =>
  call('session.selectInstance', { id });

export const logoutMobileSession = (): Promise<MobileSessionState> =>
  call('session.logout');

export const openMobileExternal = (url: string): Promise<{ opened: boolean }> =>
  call('app.openExternal', { url });
