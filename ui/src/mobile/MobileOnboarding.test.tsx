/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  MobileBridgeError,
  MobilePasswordSignInResult,
  MobileSessionState,
} from './bridge';

const mobile = vi.hoisted(() => ({
  bootstrap: vi.fn<() => Promise<MobileSessionState>>(),
  dismiss: vi.fn<() => Promise<null>>(),
  logout: vi.fn<() => Promise<MobileSessionState>>(),
  openExternal: vi.fn(),
  refresh: vi.fn<() => Promise<MobileSessionState>>(),
  requestOtp: vi.fn(),
  returnToInstance: vi.fn<() => Promise<null>>(),
  selectInstance: vi.fn(),
  signInWithPassword: vi.fn<
    (email: string, password: string) => Promise<MobilePasswordSignInResult>
  >(),
  verifyOtp: vi.fn<() => Promise<MobileSessionState>>(),
  verifyPasskey: vi.fn<() => Promise<MobileSessionState>>(),
  verifySecondFactor: vi.fn<(method: string, code: string) => Promise<MobileSessionState>>(),
}));

vi.mock('./bridge', () => ({
  ACCOUNT_SECURITY_URL: 'https://avibe.bot/app/account/security',
  bootstrapMobileSession: mobile.bootstrap,
  dismissMobileSheet: mobile.dismiss,
  SHEET_HASH: '#sheet',
  logoutMobileSession: mobile.logout,
  openMobileExternal: mobile.openExternal,
  refreshMobileSession: mobile.refresh,
  requestMobileOtp: mobile.requestOtp,
  returnToMobileInstance: mobile.returnToInstance,
  selectMobileInstance: mobile.selectInstance,
  signInMobileWithPassword: mobile.signInWithPassword,
  verifyMobileOtp: mobile.verifyOtp,
  verifyMobilePasskey: mobile.verifyPasskey,
  verifyMobileSecondFactor: mobile.verifySecondFactor,
  normalizeMobileBridgeError: (error: unknown): MobileBridgeError => {
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
    return { code: 'failed', message: 'failed' };
  },
}));

const appLicenses = vi.hoisted(() => ({ load: vi.fn() }));

vi.mock('./appLicenses', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./appLicenses')>()),
  loadAppLicenses: appLicenses.load,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    i18n: { language: 'en' },
    t: (key: string, options?: Record<string, unknown>) => {
      if (typeof options?.count === 'number') return `${key}:${options.count}`;
      if (typeof options?.name === 'string') return `${key}:${options.name}`;
      if (typeof options?.owner === 'string') return `${key}:${options.owner}`;
      return key;
    },
  }),
}));

import { MobileOnboarding } from './MobileOnboarding';

const signedIn = (): Extract<MobileSessionState, { status: 'signedIn' }> => ({
  status: 'signedIn',
  account: {
    id: 'account-1',
    email: 'alexander.with.a.very.long.address@example.com',
  },
  selected_instance_id: null,
  instances: [
    {
      id: 'owned',
      name: 'Personal Avibe',
      host: 'alex-app.avibe.bot',
      url: 'https://alex-app.avibe.bot',
      relationship: 'owner',
      owner_email: null,
      available: true,
    },
    {
      id: 'shared',
      name: 'Shared Research Avibe',
      host: 'research.example.com',
      url: 'https://research.example.com',
      relationship: 'shared',
      owner_email: 'owner@example.com',
      available: false,
    },
  ],
});

type SecondFactorState = Extract<MobileSessionState, { status: 'twoFactorRequired' }>;

const owed = (overrides: Partial<SecondFactorState> = {}): SecondFactorState => ({
  status: 'twoFactorRequired',
  email: 'alex@example.com',
  gate: 'challenge',
  methods: ['passkey', 'totp'],
  ...overrides,
});

const resetMobileMocks = () => {
  appLicenses.load.mockReset();
  appLicenses.load.mockResolvedValue(null);
  mobile.bootstrap.mockReset();
  mobile.dismiss.mockReset();
  mobile.dismiss.mockResolvedValue(null);
  mobile.logout.mockReset();
  mobile.openExternal.mockReset();
  mobile.refresh.mockReset();
  mobile.requestOtp.mockReset();
  mobile.returnToInstance.mockReset();
  mobile.selectInstance.mockReset();
  mobile.signInWithPassword.mockReset();
  mobile.verifyOtp.mockReset();
  mobile.verifyPasskey.mockReset();
  mobile.verifySecondFactor.mockReset();
  mobile.bootstrap.mockResolvedValue({ status: 'signedOut' });
  mobile.logout.mockResolvedValue({ status: 'signedOut' });
  mobile.refresh.mockResolvedValue(signedIn());
  mobile.requestOtp.mockResolvedValue({ cooldown_seconds: 0 });
  mobile.returnToInstance.mockResolvedValue(null);
  mobile.selectInstance.mockResolvedValue({ instance: signedIn().instances[0] });
  mobile.signInWithPassword.mockResolvedValue(signedIn());
  mobile.verifyOtp.mockResolvedValue(signedIn());
  mobile.verifyPasskey.mockResolvedValue(signedIn());
  mobile.verifySecondFactor.mockResolvedValue(signedIn());
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
};

const enabledInteractiveControls = (): HTMLElement[] => [
  ...document.querySelectorAll<HTMLElement>(
    'button:not(:disabled), input:not(:disabled):not([readonly]), select:not(:disabled), textarea:not(:disabled), a[href]',
  ),
];

const expectScreenLocked = () => {
  expect(enabledInteractiveControls().map((element) => (
    element.getAttribute('aria-label') ?? element.textContent?.trim() ?? element.tagName
  ))).toEqual([]);
};

type FailureOperation =
  | 'bootstrap'
  | 'accountEntry'
  | 'request'
  | 'resend'
  | 'verify'
  | 'password'
  | 'passwordCode'
  | 'passkey'
  | 'totp'
  | 'recovery'
  | 'setupDone'
  | 'refresh'
  | 'select'
  | 'logout';

const bridgeErrorCodes = [
  'network',
  'rate_limited',
  'invalid_email',
  'email_address_invalid',
  'invalid_or_expired',
  'invalid_credentials',
  'email_not_confirmed',
  'invalid',
  'passkey_cancelled',
  'passkey_unavailable',
  'busy',
  'stale_session',
  'inactive',
  'unavailable',
  'forbidden',
  'invalid_request',
  'signed_out',
  'unauthorized_instance',
  'instance_offline',
  'invalid_instance',
  'invalid_argument',
  'failed',
  'unexpected_provider_code',
] as const;

const expectedErrorKey = (
  operation: FailureOperation,
  code: typeof bridgeErrorCodes[number],
): string => {
  if (operation === 'select') {
    // An instance that did not open says so on its own row, by name.
    if (code === 'instance_offline') return 'mobileApp.sheet.openFailedOffline:Personal Avibe';
    if (code === 'unauthorized_instance') return 'mobileApp.sheet.openFailedGone:Personal Avibe';
    return 'mobileApp.sheet.openFailed:Personal Avibe';
  }
  if (operation === 'password') {
    if (code === 'invalid_credentials') return 'mobileApp.password.invalid';
    if (code === 'email_not_confirmed') return 'mobileApp.password.unconfirmed';
    if (code === 'rate_limited') return 'mobileApp.password.rateLimited';
    return 'mobileApp.errors.network';
  }
  if (
    (operation === 'request' || operation === 'resend' || operation === 'passwordCode')
    && (code === 'invalid_email' || code === 'email_address_invalid')
  ) {
    return 'mobileApp.email.invalid';
  }
  if (operation === 'verify' && code === 'invalid_or_expired') {
    return 'mobileApp.code.invalid';
  }
  if (operation === 'passkey' || operation === 'totp' || operation === 'recovery') {
    if (code === 'rate_limited') return 'mobileApp.twoFactor.rateLimited:1';
    if (
      operation === 'passkey'
      && (code === 'passkey_cancelled' || code === 'passkey_unavailable' || code === 'invalid')
    ) {
      return 'mobileApp.twoFactor.passkeyFailed';
    }
    if (code === 'invalid') {
      return operation === 'totp'
        ? 'mobileApp.twoFactor.codeInvalid'
        : 'mobileApp.twoFactor.recoveryInvalid';
    }
  }
  if (code === 'rate_limited') {
    return 'mobileApp.code.rateLimited';
  }
  return 'mobileApp.errors.network';
};

/** Sends the email code and returns its input. */
const reachCode = async (): Promise<HTMLInputElement> => {
  render(<MobileOnboarding />);
  fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
  fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
    target: { value: 'alex@example.com' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
  return await screen.findByLabelText('mobileApp.code.label') as HTMLInputElement;
};

/** Opens the password step for a valid email and returns its password field. */
const reachPassword = async (email = 'alex@example.com'): Promise<HTMLInputElement> => {
  render(<MobileOnboarding />);
  fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
  fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
    target: { value: email },
  });
  fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.entry' }));
  return await screen.findByLabelText('mobileApp.password.label') as HTMLInputElement;
};

/** Lets pending promise callbacks and re-renders run. */
const settle = () => new Promise((resolve) => {
  window.setTimeout(resolve, 20);
});

/** Opens the bundled account page the way native starts it. */
const openAccount = async (hash: '#account' | '#runtime-account') => {
  window.history.replaceState({}, '', `/${hash}`);
  mobile.bootstrap.mockResolvedValueOnce({
    ...signedIn(),
    selected_instance_id: 'owned',
  });
  render(<MobileOnboarding />);
  await screen.findByRole('heading', { name: 'mobileApp.account.title' });
};

/** Signs in with the email code into an account that owes the second factor. */
const reachSecondFactor = async (state: SecondFactorState = owed()) => {
  mobile.verifyOtp.mockResolvedValueOnce(state);
  render(<MobileOnboarding />);
  fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
  fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
    target: { value: 'alex@example.com' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
  fireEvent.change(await screen.findByLabelText('mobileApp.code.label'), {
    target: { value: '123456' },
  });
  await screen.findByRole('heading', {
    name: state.gate === 'enroll' ? 'mobileApp.twoFactor.setupTitle' : 'mobileApp.twoFactor.title',
  });
};

beforeEach(() => {
  window.history.replaceState({}, '', '/');
  resetMobileMocks();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('mobile onboarding', () => {
  it('AUTH-SETUP-148 completes email OTP discovery and authorized instance selection', async () => {
    render(<MobileOnboarding />);

    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
      target: { value: 'alex@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));

    const code = await screen.findByLabelText('mobileApp.code.label');
    fireEvent.change(code, { target: { value: '123456' } });

    const instance = await screen.findByRole('button', { name: /Personal Avibe/ });
    fireEvent.click(instance);
    await waitFor(() => expect(mobile.selectInstance).toHaveBeenCalledWith('owned'));

    expect(mobile.requestOtp).toHaveBeenCalledWith('alex@example.com');
    expect(mobile.verifyOtp).toHaveBeenCalledWith('alex@example.com', '123456');
  });

  it('AUTH-SETUP-149 keeps every bridge failure on its owning screen without reauthentication', async () => {
    const operations: FailureOperation[] = [
      'bootstrap',
      'accountEntry',
      'request',
      'resend',
      'verify',
      'password',
      'passwordCode',
      'passkey',
      'totp',
      'recovery',
      'setupDone',
      'refresh',
      'select',
      'logout',
    ];

    for (const operation of operations) {
      for (const code of bridgeErrorCodes) {
        cleanup();
        resetMobileMocks();
        window.history.replaceState({}, '', operation === 'accountEntry' ? '/#account' : '/');
        const error = {
          code,
          message: code,
          ...(code === 'rate_limited' ? { retry_after_seconds: 9 } : {}),
        };

        switch (operation) {
        case 'bootstrap':
        case 'accountEntry':
          mobile.bootstrap.mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          break;
        case 'request':
          mobile.requestOtp.mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          fireEvent.click(await screen.findByRole('button', {
            name: 'mobileApp.welcome.action',
          }));
          fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
            target: { value: 'alex@example.com' },
          });
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
          break;
        case 'resend':
          mobile.requestOtp
            .mockResolvedValueOnce({ cooldown_seconds: 0 })
            .mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          fireEvent.click(await screen.findByRole('button', {
            name: 'mobileApp.welcome.action',
          }));
          fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
            target: { value: 'alex@example.com' },
          });
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
          fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.code.resend' }));
          break;
        case 'verify':
          mobile.verifyOtp.mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          fireEvent.click(await screen.findByRole('button', {
            name: 'mobileApp.welcome.action',
          }));
          fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
            target: { value: 'alex@example.com' },
          });
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
          fireEvent.change(await screen.findByLabelText('mobileApp.code.label'), {
            target: { value: '123456' },
          });
          break;
        case 'password':
          mobile.signInWithPassword.mockRejectedValueOnce(error);
          fireEvent.change(await reachPassword(), { target: { value: 'correct-horse' } });
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));
          break;
        case 'passwordCode':
          // "Use an email code instead" appears for the errors a code fixes.
          mobile.signInWithPassword.mockRejectedValueOnce({
            code: 'invalid_credentials',
            message: 'wrong',
          });
          mobile.requestOtp.mockRejectedValueOnce(error);
          fireEvent.change(await reachPassword(), { target: { value: 'correct-horse' } });
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));
          fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.password.useCode' }));
          break;
        case 'passkey':
          mobile.bootstrap.mockResolvedValueOnce(owed());
          mobile.verifyPasskey.mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          fireEvent.click(await screen.findByRole('button', {
            name: 'mobileApp.twoFactor.usePasskey',
          }));
          break;
        case 'totp':
        case 'recovery':
          mobile.bootstrap.mockResolvedValueOnce(owed({ methods: ['totp'] }));
          mobile.verifySecondFactor.mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          if (operation === 'recovery') {
            fireEvent.click(await screen.findByRole('button', {
              name: 'mobileApp.twoFactor.useRecovery',
            }));
            fireEvent.change(screen.getByLabelText('mobileApp.twoFactor.recoveryLabel'), {
              target: { value: 'k7m2p-9xq4r' },
            });
            fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.verify' }));
          } else {
            fireEvent.change(await screen.findByLabelText('mobileApp.twoFactor.totpLabel'), {
              target: { value: '287082' },
            });
          }
          break;
        case 'setupDone':
          mobile.bootstrap.mockResolvedValueOnce(owed({ gate: 'enroll', methods: [] }));
          mobile.refresh.mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          fireEvent.click(await screen.findByRole('button', {
            name: 'mobileApp.twoFactor.setupDone',
          }));
          break;
        case 'refresh':
          mobile.bootstrap.mockResolvedValueOnce(signedIn());
          mobile.refresh.mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          fireEvent.click(await screen.findByRole('button', {
            name: 'mobileApp.instances.refresh',
          }));
          break;
        case 'select':
          mobile.bootstrap.mockResolvedValueOnce(signedIn());
          mobile.selectInstance.mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          fireEvent.click(await screen.findByRole('button', { name: /Personal Avibe/ }));
          break;
        case 'logout':
          window.history.replaceState({}, '', '/#account');
          mobile.bootstrap.mockResolvedValueOnce({
            ...signedIn(),
            selected_instance_id: 'owned',
          });
          mobile.logout.mockRejectedValueOnce(error);
          render(<MobileOnboarding />);
          fireEvent.click(await screen.findByRole('button', {
            name: 'mobileApp.account.signOut',
          }));
          break;
        }

        const alert = await screen.findByRole('alert');
        expect(alert.textContent, `${operation} ${code}`).toContain(
          expectedErrorKey(operation, code),
        );
        expect(
          screen.queryByRole('button', { name: 'mobileApp.welcome.action' }),
          `${operation} ${code}`,
        ).toBeNull();

        switch (operation) {
        case 'bootstrap':
        case 'accountEntry':
          expect(screen.getByRole('button', { name: 'common.retry' })).toBeTruthy();
          break;
        case 'request':
          expect(screen.getByLabelText('mobileApp.email.label')).toBeTruthy();
          break;
        case 'resend':
        case 'verify':
          expect(screen.getByLabelText('mobileApp.code.label')).toBeTruthy();
          break;
        case 'password':
        case 'passwordCode':
          expect(
            (screen.getByLabelText('mobileApp.password.label') as HTMLInputElement).value,
          ).toBe('correct-horse');
          expect(
            (screen.getByLabelText('mobileApp.email.label') as HTMLInputElement).value,
          ).toBe('alex@example.com');
          break;
        case 'passkey':
          expect(screen.getByRole('button', { name: 'mobileApp.twoFactor.usePasskey' })).toBeTruthy();
          break;
        case 'totp':
          expect(
            (screen.getByLabelText('mobileApp.twoFactor.totpLabel') as HTMLInputElement).value,
          ).toBe('287082');
          break;
        case 'recovery':
          expect(
            (screen.getByLabelText('mobileApp.twoFactor.recoveryLabel') as HTMLInputElement).value,
          ).toBe('k7m2p-9xq4r');
          break;
        case 'setupDone':
          expect(screen.getByRole('heading', {
            name: 'mobileApp.twoFactor.setupTitle',
          })).toBeTruthy();
          break;
        case 'refresh':
        case 'select':
          expect(screen.getByRole('heading', {
            name: 'mobileApp.instances.title',
          })).toBeTruthy();
          break;
        case 'logout':
          expect(screen.getByRole('heading', {
            name: 'mobileApp.account.title',
          })).toBeTruthy();
          break;
        }

        const expectedRequestCalls = operation === 'request' || operation === 'passwordCode'
          ? 1
          : operation === 'resend'
            ? 2
            : operation === 'verify'
              ? 1
              : 0;
        expect(mobile.requestOtp, `${operation} ${code}`).toHaveBeenCalledTimes(
          expectedRequestCalls,
        );
        expect(mobile.logout, `${operation} ${code}`).toHaveBeenCalledTimes(
          operation === 'logout' ? 1 : 0,
        );
        await Promise.resolve();
        expect(mobile.requestOtp, `${operation} ${code} late`).toHaveBeenCalledTimes(
          expectedRequestCalls,
        );
        expect(mobile.logout, `${operation} ${code} late`).toHaveBeenCalledTimes(
          operation === 'logout' ? 1 : 0,
        );
      }
    }
  }, 30_000);

  it('applies provider cooldowns to every provider-calling action', async () => {
    const actions = ['request', 'resend', 'verify'] as const;

    for (const action of actions) {
      for (const retryAfter of [9, undefined]) {
        cleanup();
        resetMobileMocks();
        const error = {
          code: 'rate_limited',
          message: 'slow down',
          ...(retryAfter === undefined ? {} : { retry_after_seconds: retryAfter }),
        };
        const expectedCooldown = retryAfter ?? 15;

        if (action === 'request') {
          mobile.requestOtp.mockRejectedValueOnce(error);
        } else if (action === 'resend') {
          mobile.requestOtp
            .mockResolvedValueOnce({ cooldown_seconds: 0 })
            .mockRejectedValueOnce(error);
        } else {
          mobile.requestOtp.mockResolvedValueOnce({ cooldown_seconds: 60 });
          mobile.verifyOtp.mockRejectedValueOnce(error);
        }

        render(<MobileOnboarding />);
        fireEvent.click(await screen.findByRole('button', {
          name: 'mobileApp.welcome.action',
        }));
        fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
          target: { value: 'alex@example.com' },
        });
        fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));

        if (action === 'request') {
          const send = await screen.findByRole('button', {
            name: `mobileApp.email.sendIn:${expectedCooldown}`,
          });
          expect((send as HTMLButtonElement).disabled).toBe(true);
          fireEvent.click(send);
          expect(mobile.requestOtp).toHaveBeenCalledTimes(1);
          expect(mobile.verifyOtp).not.toHaveBeenCalled();
          continue;
        }

        const code = await screen.findByLabelText('mobileApp.code.label');
        if (action === 'resend') {
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.resend' }));
        } else {
          // The sixth digit starts the verification.
          fireEvent.change(code, { target: { value: '123456' } });
        }

        const verify = await screen.findByRole('button', {
          name: `mobileApp.email.sendIn:${expectedCooldown}`,
        });
        const resend = screen.getByRole('button', {
          name: `mobileApp.code.resendIn:${expectedCooldown}`,
        });
        expect((verify as HTMLButtonElement).disabled).toBe(true);
        expect((resend as HTMLButtonElement).disabled).toBe(true);

        fireEvent.click(verify);
        fireEvent.click(resend);
        // A code completed during the cooldown waits for the button.
        fireEvent.change(code, { target: { value: '65432' } });
        fireEvent.change(code, { target: { value: '654321' } });
        expect((code as HTMLInputElement).value).toBe('654321');
        expect(mobile.requestOtp).toHaveBeenCalledTimes(action === 'resend' ? 2 : 1);
        expect(mobile.verifyOtp).toHaveBeenCalledTimes(action === 'verify' ? 1 : 0);
      }
    }
  });

  it('keeps successful send cooldowns scoped to another send', async () => {
    mobile.requestOtp.mockResolvedValueOnce({ cooldown_seconds: 9 });
    mobile.verifyOtp.mockReturnValueOnce(deferred<MobileSessionState>().promise);

    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', {
      name: 'mobileApp.welcome.action',
    }));
    fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
      target: { value: 'alex@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));

    const code = await screen.findByLabelText('mobileApp.code.label');
    expect(
      (screen.getByRole('button', { name: 'mobileApp.code.verify' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    const resend = screen.getByRole('button', {
      name: 'mobileApp.code.resendIn:9',
    });
    expect((resend as HTMLButtonElement).disabled).toBe(true);

    // The send cooldown does not hold back a completed code.
    fireEvent.change(code, { target: { value: '123456' } });
    expect(screen.getByRole('button', { name: 'mobileApp.code.verifying' })).toBeTruthy();
    fireEvent.click(resend);
    expect(mobile.verifyOtp).toHaveBeenCalledWith('alex@example.com', '123456');
    expect(mobile.verifyOtp).toHaveBeenCalledTimes(1);
    expect(mobile.requestOtp).toHaveBeenCalledTimes(1);
  });

  it('retries bootstrap in place and never starts OTP after a discovery failure', async () => {
    mobile.bootstrap
      .mockRejectedValueOnce({ code: 'network', message: 'offline' })
      .mockResolvedValueOnce(signedIn());

    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', { name: 'common.retry' }));

    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.bootstrap).toHaveBeenCalledTimes(2);
    expect(mobile.requestOtp).not.toHaveBeenCalled();
    expect(mobile.logout).not.toHaveBeenCalled();
  });

  it('enters Welcome only from an explicit signed-out result', async () => {
    render(<MobileOnboarding />);
    expect(await screen.findByRole('button', {
      name: 'mobileApp.welcome.action',
    })).toBeTruthy();
    cleanup();

    resetMobileMocks();
    mobile.bootstrap.mockResolvedValueOnce(signedIn());
    mobile.refresh.mockResolvedValueOnce({ status: 'signedOut' });
    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', {
      name: 'mobileApp.instances.refresh',
    }));
    expect(await screen.findByRole('button', {
      name: 'mobileApp.welcome.action',
    })).toBeTruthy();
    cleanup();

    resetMobileMocks();
    window.history.replaceState({}, '', '/#account');
    mobile.bootstrap.mockResolvedValueOnce(signedIn());
    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', {
      name: 'mobileApp.account.signOut',
    }));
    expect(await screen.findByRole('button', {
      name: 'mobileApp.welcome.action',
    })).toBeTruthy();
    cleanup();

    resetMobileMocks();
    window.history.replaceState({}, '', '/');
    mobile.verifyOtp.mockResolvedValueOnce({ status: 'signedOut' });
    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', {
      name: 'mobileApp.welcome.action',
    }));
    fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
      target: { value: 'alex@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    fireEvent.change(await screen.findByLabelText('mobileApp.code.label'), {
      target: { value: '123456' },
    });

    expect((await screen.findByRole('alert')).textContent).toContain(
      'mobileApp.errors.network',
    );
    expect(screen.getByLabelText('mobileApp.code.label')).toBeTruthy();
    expect(screen.queryByRole('button', {
      name: 'mobileApp.welcome.action',
    })).toBeNull();
  });

  it('owns one fixed viewport that scrolls every bundled screen inside the document lock', async () => {
    render(<MobileOnboarding />);

    const viewport = await screen.findByRole('main');
    expect(viewport.className).toContain('h-dvh');
    expect(viewport.className).toContain('overflow-y-auto');
    expect(viewport.className).not.toContain('min-h-[100dvh]');

    const content = viewport.firstElementChild as HTMLElement | null;
    expect(content).not.toBeNull();
    expect(content?.className).toContain('min-h-full');
    expect(content?.className).toContain('shrink-0');
    expect(content?.className).toContain('env(safe-area-inset-top)');
    expect(content?.className).toContain('env(safe-area-inset-bottom)');
  });

  it('uses one OTP input, normalizes paste, and prevents duplicate sends', async () => {
    const user = userEvent.setup();
    mobile.verifyOtp.mockReturnValueOnce(deferred<MobileSessionState>().promise);
    let finishSend: ((value: { cooldown_seconds: number }) => void) | undefined;
    mobile.requestOtp.mockReturnValueOnce(
      new Promise((resolve) => {
        finishSend = resolve;
      }),
    );

    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    await user.type(screen.getByLabelText('mobileApp.email.label'), 'alex@example.com');

    const send = screen.getByRole('button', { name: 'mobileApp.email.send' });
    await user.click(send);
    fireEvent.click(send);
    expect(mobile.requestOtp).toHaveBeenCalledTimes(1);
    expect((send as HTMLButtonElement).disabled).toBe(true);

    finishSend?.({ cooldown_seconds: 0 });
    const code = await screen.findByLabelText('mobileApp.code.label');
    fireEvent.focus(code);
    expect(code.parentElement?.className).not.toContain('focus-within:ring');
    fireEvent.change(code, { target: { value: '12 3a456' } });

    expect((code as HTMLInputElement).value).toBe('123456');
    expect(screen.getAllByText(/^[1-6]$/)).toHaveLength(6);
    expect(
      code.parentElement?.querySelectorAll('.ring-2.ring-mint\\/30'),
    ).toHaveLength(0);
    expect(mobile.verifyOtp).toHaveBeenCalledWith('alex@example.com', '123456');
  });

  it('keeps the code and applies provider retry timing after an error', async () => {
    const user = userEvent.setup();
    mobile.verifyOtp.mockRejectedValueOnce({
      code: 'invalid_or_expired',
      message: 'expired',
    });

    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    await user.type(screen.getByLabelText('mobileApp.email.label'), 'alex@example.com');
    await user.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));

    const code = await screen.findByLabelText('mobileApp.code.label');
    await user.type(code, '123456');

    expect((await screen.findByRole('alert')).textContent).toContain('mobileApp.code.invalid');
    expect((code as HTMLInputElement).value).toBe('123456');

    mobile.requestOtp.mockRejectedValueOnce({
      code: 'rate_limited',
      message: 'slow down',
      retry_after_seconds: 9,
    });
    await user.click(screen.getByRole('button', { name: 'mobileApp.code.resend' }));

    expect((await screen.findByRole('alert')).textContent).toContain('mobileApp.code.rateLimited');
    expect(
      (screen.getByRole('button', {
        name: 'mobileApp.code.resendIn:9',
      }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it('applies the client resend timer only after a successful resend', async () => {
    const user = userEvent.setup();

    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    await user.type(screen.getByLabelText('mobileApp.email.label'), 'alex@example.com');
    await user.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));

    await user.click(await screen.findByRole('button', { name: 'mobileApp.code.resend' }));
    expect(
      (screen.getByRole('button', {
        name: 'mobileApp.code.resendIn:60',
      }) as HTMLButtonElement).disabled,
    ).toBe(true);

    await user.click(screen.getByRole('button', { name: 'mobileApp.code.changeEmail' }));
    expect(
      (screen.getByRole('button', { name: 'mobileApp.email.send' }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it('applies provider retry timing to the first email request', async () => {
    const user = userEvent.setup();
    mobile.requestOtp.mockRejectedValueOnce({
      code: 'rate_limited',
      message: 'slow down',
      retry_after_seconds: 11,
    });

    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    await user.type(screen.getByLabelText('mobileApp.email.label'), 'alex@example.com');
    await user.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));

    expect((await screen.findByRole('alert')).textContent).toContain(
      'mobileApp.code.rateLimited',
    );
    expect(
      (screen.getByRole('button', {
        name: 'mobileApp.email.sendIn:11',
      }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it('shows the complete account and preserves owned, shared, and offline semantics', async () => {
    const user = userEvent.setup();
    mobile.bootstrap.mockResolvedValueOnce(signedIn());

    render(<MobileOnboarding />);

    expect(await screen.findByText('mobileApp.sheet.offlineLine')).toBeTruthy();

    const offlineRow = screen.getByRole('button', { name: /Shared Research Avibe/ });
    expect(offlineRow.getAttribute('aria-expanded')).toBe('false');
    await user.click(offlineRow);
    expect(mobile.selectInstance).not.toHaveBeenCalled();
    // Explained in place, on its own row.
    expect(offlineRow.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('mobileApp.sheet.offlineBody:Shared Research Avibe')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /Personal Avibe/ }));
    await waitFor(() => expect(mobile.selectInstance).toHaveBeenCalledWith('owned'));

    await user.click(screen.getByRole('button', { name: 'mobileApp.account.title' }));
    expect(screen.getByText('alexander.with.a.very.long.address@example.com')).toBeTruthy();
  });

  it('opens the bundled account surface when native starts with the account target', async () => {
    window.history.replaceState({}, '', '/#account');
    mobile.bootstrap.mockResolvedValueOnce({
      ...signedIn(),
      selected_instance_id: 'owned',
    });

    render(<MobileOnboarding />);

    expect(await screen.findByRole('heading', { name: 'mobileApp.account.title' })).toBeTruthy();
    expect(screen.getByText('alexander.with.a.very.long.address@example.com')).toBeTruthy();
  });

  it('disables account navigation while logout is pending', async () => {
    window.history.replaceState({}, '', '/#account');
    mobile.bootstrap.mockResolvedValueOnce({
      ...signedIn(),
      selected_instance_id: 'owned',
    });
    let finishLogout: ((state: MobileSessionState) => void) | undefined;
    mobile.logout.mockReturnValueOnce(
      new Promise((resolve) => {
        finishLogout = resolve;
      }),
    );

    render(<MobileOnboarding />);
    await screen.findByRole('heading', { name: 'mobileApp.account.title' });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.account.signOut' }));

    await waitFor(() => {
      expect(
        (screen.getByRole('button', { name: 'common.back' }) as HTMLButtonElement).disabled,
      ).toBe(true);
      expect(
        (screen.getByRole('button', { name: /Personal Avibe/ }) as HTMLButtonElement).disabled,
      ).toBe(true);
      expect(
        (screen.getByRole('button', { name: 'mobileApp.account.switch' }) as HTMLButtonElement)
          .disabled,
      ).toBe(true);
    });

    finishLogout?.({ status: 'signedOut' });
    await screen.findByRole('button', { name: 'mobileApp.welcome.action' });
  });

  it('locks every interactive control on each screen that owns an account operation', async () => {
    const user = userEvent.setup();

    const boot = deferred<MobileSessionState>();
    mobile.bootstrap.mockReturnValueOnce(boot.promise);
    render(<MobileOnboarding />);
    expectScreenLocked();
    cleanup();

    resetMobileMocks();
    const send = deferred<{ cooldown_seconds: number }>();
    mobile.requestOtp.mockReturnValueOnce(send.promise);
    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    await user.type(screen.getByLabelText('mobileApp.email.label'), 'alex@example.com');
    await user.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    expectScreenLocked();
    cleanup();

    resetMobileMocks();
    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    await user.type(screen.getByLabelText('mobileApp.email.label'), 'alex@example.com');
    await user.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    mobile.verifyOtp.mockReturnValueOnce(deferred<MobileSessionState>().promise);
    await user.type(await screen.findByLabelText('mobileApp.code.label'), '123456');
    expectScreenLocked();
    cleanup();

    resetMobileMocks();
    mobile.bootstrap.mockResolvedValueOnce(signedIn());
    mobile.refresh.mockReturnValueOnce(deferred<MobileSessionState>().promise);
    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.instances.refresh' }));
    expectScreenLocked();
    cleanup();

    resetMobileMocks();
    mobile.bootstrap.mockResolvedValueOnce({ ...signedIn(), instances: [] });
    mobile.refresh.mockReturnValueOnce(deferred<MobileSessionState>().promise);
    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.empty.refresh' }));
    expectScreenLocked();
    cleanup();

    resetMobileMocks();
    mobile.bootstrap.mockResolvedValueOnce({ ...signedIn(), instances: [] });
    mobile.refresh.mockReturnValueOnce(deferred<MobileSessionState>().promise);
    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: /mobileApp\.empty\.commandTitle/ }));
    await user.click(screen.getByRole('button', { name: 'mobileApp.empty.refresh' }));
    expectScreenLocked();
    cleanup();

    resetMobileMocks();
    window.history.replaceState({}, '', '/#account');
    mobile.bootstrap.mockResolvedValueOnce({
      ...signedIn(),
      selected_instance_id: 'owned',
    });
    mobile.logout.mockReturnValueOnce(deferred<MobileSessionState>().promise);
    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.account.signOut' }));
    expectScreenLocked();
  });

  it('keeps a slow verification as the sole owner through navigation and resend attempts', async () => {
    const user = userEvent.setup();
    const verification = deferred<MobileSessionState>();
    mobile.verifyOtp.mockReturnValueOnce(verification.promise);
    mobile.requestOtp.mockResolvedValueOnce({ cooldown_seconds: 0 }).mockRejectedValueOnce({
      code: 'busy',
      message: 'Another account operation is already running.',
    });

    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    await user.type(screen.getByLabelText('mobileApp.email.label'), 'alex@example.com');
    await user.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    const code = await screen.findByLabelText('mobileApp.code.label');
    await user.type(code, '123456');

    const back = screen.getByRole('button', { name: 'common.back' });
    const changeEmail = screen.getByRole('button', { name: 'mobileApp.code.changeEmail' });
    const resend = screen.getByRole('button', { name: 'mobileApp.code.resend' });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.verifying' }));
    fireEvent.click(resend);
    fireEvent.click(back);
    fireEvent.click(changeEmail);
    fireEvent.change(code, { target: { value: '654321' } });

    expectScreenLocked();
    expect(mobile.verifyOtp).toHaveBeenCalledTimes(1);
    expect(mobile.requestOtp).toHaveBeenCalledTimes(1);
    expect((code as HTMLInputElement).value).toBe('123456');

    verification.resolve(signedIn());
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
  });

  it('applies a slow verification failure only to its owning code screen', async () => {
    const user = userEvent.setup();
    const verification = deferred<MobileSessionState>();
    mobile.verifyOtp.mockReturnValueOnce(verification.promise);

    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    await user.type(screen.getByLabelText('mobileApp.email.label'), 'alex@example.com');
    await user.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    await user.type(await screen.findByLabelText('mobileApp.code.label'), '123456');

    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.changeEmail' }));
    verification.reject({ code: 'invalid_or_expired', message: 'expired' });

    expect((await screen.findByRole('alert')).textContent).toContain('mobileApp.code.invalid');
    expect(screen.getByLabelText('mobileApp.code.label')).toBeTruthy();
    expect(enabledInteractiveControls().length).toBeGreaterThan(0);
  });

  it('unlocks after a timed-out verification and requires a fresh code', async () => {
    const user = userEvent.setup();
    const verification = deferred<MobileSessionState>();
    mobile.verifyOtp.mockReturnValueOnce(verification.promise);

    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    await user.type(screen.getByLabelText('mobileApp.email.label'), 'alex@example.com');
    await user.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    const code = await screen.findByLabelText('mobileApp.code.label');
    await user.type(code, '123456');

    verification.reject({ code: 'network', message: 'Request timed out.' });

    expect((await screen.findByRole('alert')).textContent).toContain('mobileApp.errors.network');
    expect((code as HTMLInputElement).value).toBe('');
    expect(
      (screen.getByRole('button', { name: 'mobileApp.code.verify' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole('button', { name: 'mobileApp.code.resend' }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    expect(enabledInteractiveControls().length).toBeGreaterThan(0);
  });

  it('AUTH-SETUP-156 passes the second factor after the email code and reaches the instances', async () => {
    mobile.verifyPasskey.mockRejectedValueOnce({
      code: 'passkey_cancelled',
      message: 'cancelled',
    });
    await reachSecondFactor();

    expect(screen.getByText('mobileApp.twoFactor.onFor')).toBeTruthy();
    expect(screen.getByText('alex@example.com')).toBeTruthy();
    expect(screen.queryByLabelText('mobileApp.twoFactor.totpLabel')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.usePasskey' }));
    expect((await screen.findByRole('alert')).textContent).toContain(
      'mobileApp.twoFactor.passkeyFailed',
    );

    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.useTotp' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('button', { name: 'mobileApp.twoFactor.usePasskeyInstead' })).toBeTruthy();
    const code = screen.getByLabelText('mobileApp.twoFactor.totpLabel');
    fireEvent.change(code, { target: { value: '28 70a82' } });
    expect((code as HTMLInputElement).value).toBe('287082');

    fireEvent.click(await screen.findByRole('button', { name: /Personal Avibe/ }));
    await waitFor(() => expect(mobile.selectInstance).toHaveBeenCalledWith('owned'));
    expect(mobile.verifyPasskey).toHaveBeenCalledTimes(1);
    expect(mobile.verifySecondFactor).toHaveBeenCalledWith('totp', '287082');
    expect(mobile.logout).not.toHaveBeenCalled();
  });

  it('offers each account only the second factors it has', async () => {
    mobile.bootstrap.mockResolvedValueOnce(owed({ methods: ['totp'], email: null }));
    render(<MobileOnboarding />);

    expect(await screen.findByLabelText('mobileApp.twoFactor.totpLabel')).toBeTruthy();
    expect(screen.getByText('mobileApp.twoFactor.onThisAccount')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'mobileApp.twoFactor.usePasskeyInstead' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.useRecovery' }));
    expect(screen.queryByRole('button', { name: 'mobileApp.twoFactor.useRecovery' })).toBeNull();
    expect(screen.getByRole('button', { name: 'mobileApp.twoFactor.useTotp' })).toBeTruthy();
    cleanup();

    resetMobileMocks();
    mobile.bootstrap.mockResolvedValueOnce(owed({ methods: ['passkey'] }));
    render(<MobileOnboarding />);
    expect(await screen.findByRole('button', { name: 'mobileApp.twoFactor.usePasskey' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'mobileApp.twoFactor.useTotp' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.useRecovery' }));
    expect(screen.getByRole('button', { name: 'mobileApp.twoFactor.usePasskeyInstead' })).toBeTruthy();
  });

  it('keeps a rejected or unsent code and shows avibe.bot retry timing', async () => {
    mobile.bootstrap.mockResolvedValueOnce(owed({ methods: ['totp'] }));
    mobile.verifySecondFactor
      .mockRejectedValueOnce({ code: 'invalid', message: 'invalid' })
      .mockRejectedValueOnce({ code: 'rate_limited', message: 'slow', retry_after_seconds: 900 })
      .mockRejectedValueOnce({ code: 'rate_limited', message: 'slow' })
      .mockRejectedValueOnce({ code: 'network', message: 'offline' });
    render(<MobileOnboarding />);
    const code = await screen.findByLabelText('mobileApp.twoFactor.totpLabel');
    fireEvent.change(code, { target: { value: '287083' } });

    for (const [attempt, expected] of [
      'mobileApp.twoFactor.codeInvalid',
      'mobileApp.twoFactor.rateLimited:15',
      'mobileApp.twoFactor.rateLimited:15',
      'mobileApp.errors.network',
    ].entries()) {
      // The sixth digit sent the first attempt; the button retries the same code.
      if (attempt > 0) {
        fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.verify' }));
      }
      expect((await screen.findByRole('alert')).textContent).toBe(expected);
      expect((code as HTMLInputElement).value).toBe('287083');
      expect(code.getAttribute('aria-invalid')).toBe(
        expected === 'mobileApp.twoFactor.codeInvalid' ? 'true' : null,
      );
    }
    expect(mobile.verifySecondFactor).toHaveBeenCalledTimes(4);
  });

  it('verifies a recovery code and reports one that is wrong or used', async () => {
    mobile.bootstrap.mockResolvedValueOnce(owed());
    mobile.verifySecondFactor.mockRejectedValueOnce({ code: 'invalid', message: 'used' });
    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.twoFactor.useRecovery' }));

    const verify = screen.getByRole('button', { name: 'mobileApp.twoFactor.verify' });
    expect((verify as HTMLButtonElement).disabled).toBe(true);
    const input = screen.getByLabelText('mobileApp.twoFactor.recoveryLabel');
    fireEvent.change(input, { target: { value: 'k7m2p-9xq4r' } });
    fireEvent.click(verify);
    expect((await screen.findByRole('alert')).textContent).toBe(
      'mobileApp.twoFactor.recoveryInvalid',
    );
    expect(input.getAttribute('aria-invalid')).toBe('true');

    fireEvent.submit(input);
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.verifySecondFactor).toHaveBeenNthCalledWith(1, 'recovery', 'k7m2p-9xq4r');
    expect(mobile.verifySecondFactor).toHaveBeenNthCalledWith(2, 'recovery', 'k7m2p-9xq4r');
  });

  it('sends an account that must turn it on to avibe.bot and continues with the new gate', async () => {
    await reachSecondFactor(owed({ gate: 'enroll', methods: [] }));

    expect(screen.getByText('mobileApp.twoFactor.requiredFor')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'mobileApp.twoFactor.useRecovery' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.openSecurity' }));
    expect(mobile.openExternal).toHaveBeenCalledWith('https://avibe.bot/app/account/security');

    mobile.refresh.mockResolvedValueOnce(owed({ methods: ['passkey'] }));
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.setupDone' }));
    expect(await screen.findByRole('button', { name: 'mobileApp.twoFactor.usePasskey' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'mobileApp.twoFactor.title' })).toBeTruthy();
  });

  it('locks the second-factor screen while the passkey sheet or a code check runs', async () => {
    const passkey = deferred<MobileSessionState>();
    mobile.verifyPasskey.mockReturnValueOnce(passkey.promise);
    mobile.bootstrap.mockResolvedValueOnce(owed());
    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.twoFactor.usePasskey' }));

    expect(screen.getByRole('button', { name: 'mobileApp.twoFactor.waitingForPasskey' })).toBeTruthy();
    expectScreenLocked();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.differentAccount' }));
    expect(mobile.logout).not.toHaveBeenCalled();
    passkey.reject({ code: 'passkey_unavailable', message: 'none' });
    expect((await screen.findByRole('alert')).textContent).toBe('mobileApp.twoFactor.passkeyFailed');

    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.useTotp' }));
    mobile.verifySecondFactor.mockReturnValueOnce(deferred<MobileSessionState>().promise);
    fireEvent.change(screen.getByLabelText('mobileApp.twoFactor.totpLabel'), {
      target: { value: '287082' },
    });
    expect(screen.getByRole('button', { name: 'mobileApp.twoFactor.verifying' })).toBeTruthy();
    expectScreenLocked();
  });

  it('returns to Welcome when the email sign-in ended or the person switches account', async () => {
    mobile.bootstrap.mockResolvedValueOnce(owed());
    mobile.verifyPasskey.mockResolvedValueOnce({ status: 'signedOut' });
    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.twoFactor.usePasskey' }));
    expect(await screen.findByRole('button', { name: 'mobileApp.welcome.action' })).toBeTruthy();
    expect(mobile.logout).not.toHaveBeenCalled();
    cleanup();

    resetMobileMocks();
    mobile.bootstrap.mockResolvedValueOnce(owed());
    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', {
      name: 'mobileApp.twoFactor.differentAccount',
    }));
    expect(await screen.findByRole('button', { name: 'mobileApp.welcome.action' })).toBeTruthy();
    expect(mobile.logout).toHaveBeenCalledTimes(1);
    expect(mobile.verifyPasskey).not.toHaveBeenCalled();
  });

  it('starts verification once when the last code digit is typed, pasted, or autofilled', async () => {
    const user = userEvent.setup();
    const verification = deferred<MobileSessionState>();
    mobile.verifyOtp.mockReturnValueOnce(verification.promise);
    const typed = await reachCode();

    await user.type(typed, '12345');
    expect(mobile.verifyOtp).not.toHaveBeenCalled();
    await user.type(typed, '6');
    expect(mobile.verifyOtp).toHaveBeenCalledTimes(1);
    expect(mobile.verifyOtp).toHaveBeenCalledWith('alex@example.com', '123456');
    verification.resolve(signedIn());
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    cleanup();

    // Paste and one-time-code autofill deliver the whole code in one edit,
    // which can arrive as both an input and a change event.
    resetMobileMocks();
    const pasted = await reachCode();
    fireEvent.input(pasted, { target: { value: '654321' } });
    fireEvent.change(pasted, { target: { value: '654321' } });
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.verifyOtp).toHaveBeenCalledTimes(1);
    expect(mobile.verifyOtp).toHaveBeenCalledWith('alex@example.com', '654321');
  });

  it('sends a failed code again only when it is edited or the button is pressed', async () => {
    const user = userEvent.setup();
    const expired = { code: 'invalid_or_expired', message: 'expired' };
    mobile.verifyOtp.mockRejectedValueOnce(expired).mockRejectedValueOnce(expired);
    const code = await reachCode();

    await user.type(code, '123456');
    expect((await screen.findByRole('alert')).textContent).toContain('mobileApp.code.invalid');
    await settle();
    expect(mobile.verifyOtp).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));
    await waitFor(() => expect(mobile.verifyOtp).toHaveBeenCalledTimes(2));
    expect((await screen.findByRole('alert')).textContent).toContain('mobileApp.code.invalid');
    await settle();
    expect(mobile.verifyOtp).toHaveBeenCalledTimes(2);

    await user.type(code, '{Backspace}');
    expect(mobile.verifyOtp).toHaveBeenCalledTimes(2);
    await user.type(code, '7');
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.verifyOtp).toHaveBeenCalledTimes(3);
    expect(mobile.verifyOtp).toHaveBeenLastCalledWith('alex@example.com', '123457');
  });

  it('starts the authenticator check on its sixth digit but leaves a recovery code to the button', async () => {
    const user = userEvent.setup();
    mobile.bootstrap.mockResolvedValueOnce(owed({ methods: ['totp'] }));
    mobile.verifySecondFactor.mockRejectedValueOnce({ code: 'invalid', message: 'invalid' });
    render(<MobileOnboarding />);
    const code = await screen.findByLabelText('mobileApp.twoFactor.totpLabel');

    await user.type(code, '28708');
    expect(mobile.verifySecondFactor).not.toHaveBeenCalled();
    await user.type(code, '2');
    expect((await screen.findByRole('alert')).textContent).toBe(
      'mobileApp.twoFactor.codeInvalid',
    );
    await settle();
    expect(mobile.verifySecondFactor).toHaveBeenCalledTimes(1);
    expect(mobile.verifySecondFactor).toHaveBeenCalledWith('totp', '287082');

    await user.type(code, '{Backspace}3');
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.verifySecondFactor).toHaveBeenCalledTimes(2);
    expect(mobile.verifySecondFactor).toHaveBeenLastCalledWith('totp', '287083');
    cleanup();

    resetMobileMocks();
    mobile.bootstrap.mockResolvedValueOnce(owed({ methods: ['totp'] }));
    render(<MobileOnboarding />);
    await user.click(await screen.findByRole('button', {
      name: 'mobileApp.twoFactor.useRecovery',
    }));
    await user.type(screen.getByLabelText('mobileApp.twoFactor.recoveryLabel'), '123456');
    await settle();
    expect(mobile.verifySecondFactor).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.verify' }));
    expect(mobile.verifySecondFactor).toHaveBeenCalledWith('recovery', '123456');
  });

  it('holds a completed authenticator code while avibe.bot rate-limits the second factor', async () => {
    const user = userEvent.setup();
    mobile.bootstrap.mockResolvedValueOnce(owed({ methods: ['totp'] }));
    mobile.verifySecondFactor.mockRejectedValueOnce({
      code: 'rate_limited',
      message: 'slow down',
      retry_after_seconds: 900,
    });
    render(<MobileOnboarding />);
    const code = await screen.findByLabelText('mobileApp.twoFactor.totpLabel');

    await user.type(code, '287082');
    expect((await screen.findByRole('alert')).textContent).toBe(
      'mobileApp.twoFactor.rateLimited:15',
    );
    await user.type(code, '{Backspace}3');
    await settle();
    expect((code as HTMLInputElement).value).toBe('287083');
    expect(mobile.verifySecondFactor).toHaveBeenCalledTimes(1);

    // The button still sends it.
    await user.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.verify' }));
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.verifySecondFactor).toHaveBeenCalledTimes(2);
    expect(mobile.verifySecondFactor).toHaveBeenLastCalledWith('totp', '287083');
  });

  it('returns Account Back to the instance it covers, and to the list when none is behind it', async () => {
    await openAccount('#account');
    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
    expect(await screen.findByRole('heading', {
      name: 'mobileApp.instances.switchTitle',
    })).toBeTruthy();
    expect(mobile.returnToInstance).not.toHaveBeenCalled();
    cleanup();

    resetMobileMocks();
    await openAccount('#runtime-account');
    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
    await waitFor(() => expect(mobile.returnToInstance).toHaveBeenCalledTimes(1));
    // Native removes this page; the list is not drawn underneath it.
    expect(screen.getByRole('heading', { name: 'mobileApp.account.title' })).toBeTruthy();

    // Looking at the list first still leaves the same instance behind Account.
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.account.switch' }));
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.account.title' }));
    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
    await waitFor(() => expect(mobile.returnToInstance).toHaveBeenCalledTimes(2));
    expect(mobile.selectInstance).not.toHaveBeenCalled();
  });

  it('returns to the covered instance from Account after a failed first read and a Retry', async () => {
    window.history.replaceState({}, '', '/#runtime-account');
    mobile.bootstrap
      .mockRejectedValueOnce({ code: 'busy', message: 'Another account operation is running.' })
      .mockResolvedValueOnce({ ...signedIn(), selected_instance_id: 'owned' });
    render(<MobileOnboarding />);

    // The error page over an instance has its own way back to it.
    const errorBack = await screen.findByRole('button', { name: 'common.back' });
    expect(errorBack.hasAttribute('data-mobile-back')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'common.retry' }));
    await screen.findByRole('heading', { name: 'mobileApp.account.title' });

    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
    await waitFor(() => expect(mobile.returnToInstance).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('heading', { name: 'mobileApp.instances.switchTitle' })).toBeNull();
  });

  it('returns from the instance list to the instance it covers, and offers no Back without one', async () => {
    await openAccount('#runtime-account');
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.account.switch' }));
    await screen.findByRole('heading', { name: 'mobileApp.instances.switchTitle' });
    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
    await waitFor(() => expect(mobile.returnToInstance).toHaveBeenCalledTimes(1));
    cleanup();

    resetMobileMocks();
    await openAccount('#account');
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.account.switch' }));
    await screen.findByRole('heading', { name: 'mobileApp.instances.switchTitle' });
    // Nothing to swipe back to, either.
    expect(screen.queryByRole('button', { name: 'common.back' })).toBeNull();
    expect(document.querySelector('[data-mobile-back]')).toBeNull();
  });

  it('locks Account while returning and shows the list once the instance behind it closed', async () => {
    const returning = deferred<null>();
    mobile.returnToInstance.mockReturnValueOnce(returning.promise);
    await openAccount('#runtime-account');

    const back = screen.getByRole('button', { name: 'common.back' });
    fireEvent.click(back);
    expectScreenLocked();
    fireEvent.click(back);
    expect(mobile.returnToInstance).toHaveBeenCalledTimes(1);

    returning.reject({
      code: 'runtime_unavailable',
      message: 'No instance is open behind the account page.',
    });
    expect(await screen.findByRole('heading', {
      name: 'mobileApp.instances.switchTitle',
    })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.account.title' }));
    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
    expect(await screen.findByRole('heading', {
      name: 'mobileApp.instances.switchTitle',
    })).toBeTruthy();
    expect(mobile.returnToInstance).toHaveBeenCalledTimes(1);
  });
});

describe('mobile password sign-in', () => {
  const emailField = () => screen.getByLabelText('mobileApp.email.label') as HTMLInputElement;
  const passwordField = () => screen.queryByLabelText('mobileApp.password.label') as HTMLInputElement | null;
  const codeWayControl = () => screen.queryByRole('button', { name: 'mobileApp.password.useCode' });

  /** Opens the email step and types `email` in code mode. */
  const reachEmail = async (email: string) => {
    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    if (email) fireEvent.change(emailField(), { target: { value: email } });
  };

  it('AUTH-SETUP-158 signs in with the avibe.bot password and reaches the instances', async () => {
    const user = userEvent.setup();
    await reachEmail(' alex@example.com ');
    // The email step keeps the code as its primary action and offers the password under it.
    expect(screen.getByRole('button', { name: 'mobileApp.email.send' })).toBeTruthy();
    const email = emailField();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.entry' }));

    // One form: the same email field, now the saved login's username, above the password.
    const password = passwordField() as HTMLInputElement;
    expect(emailField()).toBe(email);
    // An email input drops the surrounding spaces from its value.
    expect(email.value).toBe('alex@example.com');
    expect(email.form).toBe(password.form);
    expect(screen.getByRole('heading', { name: 'mobileApp.email.title' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'mobileApp.email.send' })).toBeNull();
    expect(screen.getByRole('button', { name: 'mobileApp.password.codeEntry' })).toBeTruthy();
    expect(document.querySelectorAll('input[readonly], input[aria-hidden="true"]')).toHaveLength(0);
    expect(document.activeElement).toBe(password);
    expect(email.type).toBe('email');
    expect(email.getAttribute('autocomplete')).toBe('username');
    expect(email.getAttribute('name')).toBe('username');
    expect(email.getAttribute('enterkeyhint')).toBe('next');
    expect(password.type).toBe('password');
    expect(password.getAttribute('name')).toBe('password');
    expect(password.getAttribute('autocomplete')).toBe('current-password');
    expect(password.getAttribute('autocapitalize')).toBe('none');
    expect(password.getAttribute('spellcheck')).toBe('false');
    expect(password.getAttribute('enterkeyhint')).toBe('go');
    expect(password.getAttribute('aria-describedby')).toBe('mobile-login-password-feedback');

    // The only client check before Sign in is a non-empty password; nothing submits on its own.
    const signIn = screen.getByRole('button', { name: 'mobileApp.password.signIn' }) as HTMLButtonElement;
    expect(signIn.disabled).toBe(true);
    fireEvent.submit(password);
    await user.type(password, 'correct horse');
    expect(signIn.disabled).toBe(false);
    expect(mobile.signInWithPassword).not.toHaveBeenCalled();

    const toggle = screen.getByRole('button', { name: 'mobileApp.password.show' });
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(toggle);
    expect(password.type).toBe('text');
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: 'mobileApp.password.show' })).toBe(toggle);
    fireEvent.click(toggle);
    expect(password.type).toBe('password');

    // Return in the password field submits.
    await user.type(password, '{Enter}');
    fireEvent.click(await screen.findByRole('button', { name: /Personal Avibe/ }));
    await waitFor(() => expect(mobile.selectInstance).toHaveBeenCalledWith('owned'));
    expect(mobile.signInWithPassword).toHaveBeenCalledTimes(1);
    expect(mobile.signInWithPassword).toHaveBeenCalledWith('alex@example.com', 'correct horse');
    expect(mobile.requestOtp).not.toHaveBeenCalled();
    expect(mobile.verifyOtp).not.toHaveBeenCalled();
  });

  it('switches to password sign-in at once, whatever the email holds', async () => {
    for (const [typed, focus] of [['', 'email'], ['max@', 'password'], ['alex@example.com', 'password']] as const) {
      cleanup();
      resetMobileMocks();
      await reachEmail(typed);
      fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.entry' }));

      expect(passwordField(), typed).not.toBeNull();
      expect(emailField().value, typed).toBe(typed);
      expect(screen.queryByRole('alert'), typed).toBeNull();
      expect(emailField().getAttribute('aria-invalid'), typed).toBeNull();
      // An empty email is where the person starts; a typed one moves them on to the password.
      expect(document.activeElement, typed).toBe(focus === 'email' ? emailField() : passwordField());
    }
    expect(mobile.requestOtp).not.toHaveBeenCalled();
    expect(mobile.signInWithPassword).not.toHaveBeenCalled();
  });

  it('checks the email on Sign in and Forgot password, and sends nothing for an invalid one', async () => {
    for (const typed of ['', 'max@']) {
      cleanup();
      resetMobileMocks();
      await reachEmail(typed);
      fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.entry' }));
      fireEvent.change(passwordField() as HTMLInputElement, { target: { value: 'correct-horse' } });

      for (const action of ['mobileApp.password.signIn', 'mobileApp.password.forgot']) {
        fireEvent.click(screen.getByRole('button', { name: action }));
        const alert = await screen.findByRole('alert');
        expect(alert.textContent, `${typed} ${action}`).toBe('mobileApp.email.invalid');
        expect(alert.parentElement?.id).toBe('mobile-login-email-feedback');
        expect(emailField().getAttribute('aria-invalid'), typed).toBe('true');
        expect(passwordField()?.getAttribute('aria-invalid'), typed).toBeNull();
        expect(passwordField()?.value, typed).toBe('correct-horse');
        // Editing the email clears its error and keeps the password.
        fireEvent.change(emailField(), { target: { value: `${typed}x` } });
        expect(screen.queryByRole('alert'), typed).toBeNull();
        expect(passwordField()?.value, typed).toBe('correct-horse');
        fireEvent.change(emailField(), { target: { value: typed } });
      }
      expect(screen.queryByRole('heading', { name: 'mobileApp.password.forgotTitle' })).toBeNull();
    }
    // In code mode, Send verification code keeps the same check and copy.
    cleanup();
    resetMobileMocks();
    await reachEmail('max@');
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    expect((await screen.findByRole('alert')).textContent).toBe('mobileApp.email.invalid');
    expect(mobile.requestOtp).not.toHaveBeenCalled();
    expect(mobile.signInWithPassword).not.toHaveBeenCalled();
  });

  it('carries the email both ways and clears the password on returning to code mode', async () => {
    await reachEmail('alex@example.com');
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.entry' }));
    fireEvent.change(passwordField() as HTMLInputElement, { target: { value: 'correct-horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.show' }));
    fireEvent.change(emailField(), { target: { value: 'sam@example.com' } });
    expect(passwordField()?.value).toBe('correct-horse');

    const returns = [
      () => fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.codeEntry' })),
      () => fireEvent.click(screen.getByRole('button', { name: 'common.back' })),
    ];
    for (const [index, back] of returns.entries()) {
      back();
      expect(passwordField(), `return ${index}`).toBeNull();
      expect(screen.getByRole('button', { name: 'mobileApp.email.send' })).toBeTruthy();
      expect(emailField().value, `return ${index}`).toBe('sam@example.com');
      expect(emailField().getAttribute('autocomplete')).toBe('email');
      expect(document.body.innerHTML).not.toContain('correct-horse');

      fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.entry' }));
      const password = passwordField() as HTMLInputElement;
      expect(password.value, `return ${index}`).toBe('');
      expect(password.type, `return ${index}`).toBe('password');
      expect(emailField().value).toBe('sam@example.com');
      fireEvent.change(password, { target: { value: 'correct-horse' } });
    }
  });

  it('moves from the email to the password on Return, and signs in on Return there', async () => {
    const user = userEvent.setup();
    await reachEmail('');
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.entry' }));
    expect(document.activeElement).toBe(emailField());

    await user.type(emailField(), 'alex@example.com{Enter}');
    expect(document.activeElement).toBe(passwordField());
    expect(mobile.signInWithPassword).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).toBeNull();

    await user.type(passwordField() as HTMLInputElement, 'correct-horse{Enter}');
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.signInWithPassword).toHaveBeenCalledTimes(1);
    expect(mobile.signInWithPassword).toHaveBeenCalledWith('alex@example.com', 'correct-horse');
  });

  it('keeps the email and password through each failure and shows its copy', async () => {
    const cases = [
      { error: { code: 'invalid_credentials' }, alert: 'mobileApp.password.invalid', marked: true, codeFixes: true, wait: 0 },
      { error: { code: 'email_not_confirmed' }, alert: 'mobileApp.password.unconfirmed', marked: false, codeFixes: true, wait: 0 },
      { error: { code: 'rate_limited', retry_after_seconds: 45 }, alert: 'mobileApp.password.rateLimited', marked: false, codeFixes: false, wait: 45 },
      { error: { code: 'rate_limited' }, alert: 'mobileApp.password.rateLimited', marked: false, codeFixes: false, wait: 60 },
      { error: { code: 'network' }, alert: 'mobileApp.errors.network', marked: false, codeFixes: false, wait: 0 },
      { error: { code: 'unexpected_provider_code' }, alert: 'mobileApp.errors.network', marked: false, codeFixes: false, wait: 0 },
      // A session that answered signed out right after the callback stays on this step.
      { result: { status: 'signedOut' } as const, alert: 'mobileApp.errors.network', marked: false, codeFixes: false, wait: 0 },
    ];

    for (const testCase of cases) {
      cleanup();
      resetMobileMocks();
      if (testCase.result) {
        mobile.signInWithPassword.mockResolvedValueOnce(testCase.result);
      } else {
        mobile.signInWithPassword.mockRejectedValueOnce({ message: 'rejected', ...testCase.error });
      }
      const password = await reachPassword();
      fireEvent.change(password, { target: { value: 'correct-horse' } });
      fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));

      const alert = await screen.findByRole('alert');
      expect(alert.textContent, testCase.alert).toBe(testCase.alert);
      expect(alert.parentElement?.id).toBe('mobile-login-password-feedback');
      expect(password.value, testCase.alert).toBe('correct-horse');
      expect(password.disabled, testCase.alert).toBe(false);
      expect(emailField().value).toBe('alex@example.com');
      expect(emailField().getAttribute('aria-invalid')).toBeNull();
      expect(password.getAttribute('aria-invalid'), testCase.alert).toBe(testCase.marked ? 'true' : null);
      // The errors an email code fixes add a way to send one.
      expect(Boolean(codeWayControl()), testCase.alert).toBe(testCase.codeFixes);
      const signIn = screen.getByRole('button', {
        name: testCase.wait > 0
          ? `mobileApp.password.retryIn:${testCase.wait}`
          : 'mobileApp.password.signIn',
      }) as HTMLButtonElement;
      expect(signIn.disabled, testCase.alert).toBe(testCase.wait > 0);
      expect(mobile.requestOtp).not.toHaveBeenCalled();
    }
  });

  it('waits out a password rate limit before Sign in is offered again', async () => {
    mobile.signInWithPassword
      .mockRejectedValueOnce({ code: 'rate_limited', message: 'slow down', retry_after_seconds: 1 })
      .mockResolvedValueOnce(signedIn());
    const password = await reachPassword();
    fireEvent.change(password, { target: { value: 'correct-horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));

    const waiting = await screen.findByRole('button', { name: 'mobileApp.password.retryIn:1' });
    fireEvent.submit(password);
    // Editing stays allowed while Sign in waits.
    fireEvent.change(password, { target: { value: 'correct-horse-2' } });
    expect(password.value).toBe('correct-horse-2');
    expect((waiting as HTMLButtonElement).disabled).toBe(true);
    expect(mobile.signInWithPassword).toHaveBeenCalledTimes(1);

    fireEvent.click(await screen.findByRole(
      'button',
      { name: 'mobileApp.password.signIn' },
      { timeout: 3_000 },
    ));
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.signInWithPassword).toHaveBeenLastCalledWith('alex@example.com', 'correct-horse-2');
  });

  it('locks every control while signing in', async () => {
    const signIn = deferred<MobilePasswordSignInResult>();
    mobile.signInWithPassword.mockReturnValueOnce(signIn.promise);
    const password = await reachPassword();
    fireEvent.change(password, { target: { value: 'correct-horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));

    const busy = screen.getByRole('button', { name: 'mobileApp.password.signingIn' });
    expect(busy.parentElement?.getAttribute('aria-busy')).toBe('true');
    expectScreenLocked();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.codeEntry' }));
    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
    fireEvent.change(emailField(), { target: { value: 'sam@example.com' } });
    fireEvent.submit(password);
    expect(passwordField()).toBe(password);
    expect(emailField().value).toBe('alex@example.com');
    expect(mobile.signInWithPassword).toHaveBeenCalledTimes(1);
    expect(mobile.requestOtp).not.toHaveBeenCalled();

    signIn.resolve(signedIn());
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
  });

  it('clears the password and its error on leaving the step', async () => {
    const leaves = [
      { name: 'code mode', leave: () => fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.codeEntry' })) },
      { name: 'forgot', leave: () => fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.forgot' })) },
    ];
    for (const { name, leave } of leaves) {
      cleanup();
      resetMobileMocks();
      mobile.signInWithPassword.mockRejectedValueOnce({ code: 'invalid_credentials', message: 'wrong' });
      const password = await reachPassword();
      fireEvent.change(password, { target: { value: 'correct-horse' } });
      fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.show' }));
      fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));
      await screen.findByRole('alert');

      leave();
      expect(screen.queryByRole('alert'), name).toBeNull();
      expect(document.body.innerHTML, name).not.toContain('correct-horse');
      if (name === 'forgot') {
        await screen.findByRole('heading', { name: 'mobileApp.password.forgotTitle' });
        // Back returns to password mode.
        fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
      } else {
        // Code mode, then Back leaves the step for Welcome.
        fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
        fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
        expect(emailField().value, name).toBe('alex@example.com');
        fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.entry' }));
      }

      const again = passwordField() as HTMLInputElement;
      expect(again.value, name).toBe('');
      expect(again.type, name).toBe('password');
      expect(emailField().value, name).toBe('alex@example.com');
      expect(screen.queryByRole('alert'), name).toBeNull();
    }
  });

  it('uses an email code instead through the existing request for the same email', async () => {
    const request = deferred<{ cooldown_seconds: number }>();
    mobile.requestOtp.mockReturnValueOnce(request.promise);
    mobile.signInWithPassword.mockRejectedValueOnce({ code: 'invalid_credentials', message: 'wrong' });
    const password = await reachPassword();
    fireEvent.change(password, { target: { value: 'correct-horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));
    await screen.findByRole('alert');

    fireEvent.click(codeWayControl() as HTMLElement);
    expect(mobile.requestOtp).toHaveBeenCalledWith('alex@example.com');
    expect(screen.getByRole('button', { name: 'mobileApp.email.sending' })).toBeTruthy();
    expectScreenLocked();

    request.resolve({ cooldown_seconds: 0 });
    const code = await screen.findByLabelText('mobileApp.code.label');
    expect(screen.getByText('alex@example.com')).toBeTruthy();
    expect(document.body.innerHTML).not.toContain('correct-horse');
    // The approved code step: the sixth digit sends it.
    fireEvent.change(code, { target: { value: '123456' } });
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.verifyOtp).toHaveBeenCalledWith('alex@example.com', '123456');
    expect(mobile.requestOtp).toHaveBeenCalledTimes(1);
    expect(mobile.signInWithPassword).toHaveBeenCalledTimes(1);
  });

  it('returns Change email on the code step to code mode with the email kept', async () => {
    mobile.signInWithPassword.mockRejectedValueOnce({ code: 'email_not_confirmed', message: 'confirm' });
    const password = await reachPassword();
    fireEvent.change(password, { target: { value: 'correct-horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));
    await screen.findByRole('alert');
    fireEvent.click(codeWayControl() as HTMLElement);
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.code.changeEmail' }));

    expect(screen.getByRole('button', { name: 'mobileApp.email.send' })).toBeTruthy();
    expect(passwordField()).toBeNull();
    expect(emailField().value).toBe('alex@example.com');
  });

  it('offers a code or Account security in Safari when the password is forgotten', async () => {
    await reachPassword();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.forgot' }));
    expect(await screen.findByRole('heading', { name: 'mobileApp.password.forgotTitle' })).toBeTruthy();
    expect(screen.getByText('alex@example.com')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.openInSafari' }));
    expect(mobile.openExternal).toHaveBeenCalledWith('https://avibe.bot/app/account/security');

    mobile.requestOtp.mockRejectedValueOnce({ code: 'network', message: 'offline' });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    expect((await screen.findByRole('alert')).textContent).toBe('mobileApp.errors.network');
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    expect(await screen.findByLabelText('mobileApp.code.label')).toBeTruthy();
    expect(mobile.requestOtp).toHaveBeenCalledTimes(2);
    expect(mobile.requestOtp).toHaveBeenLastCalledWith('alex@example.com');
    expect(mobile.signInWithPassword).not.toHaveBeenCalled();
  });

  it('shows a weak-password notice once, after the second factor and before the instances', async () => {
    mobile.signInWithPassword.mockResolvedValueOnce({
      ...owed({ methods: ['totp'] }),
      weak_password: { reasons: ['pwned'] },
    });
    const password = await reachPassword();
    fireEvent.change(password, { target: { value: 'correct-horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));

    // The second factor comes first, unchanged.
    const code = await screen.findByLabelText('mobileApp.twoFactor.totpLabel');
    expect(screen.queryByRole('heading', { name: 'mobileApp.password.weakTitle' })).toBeNull();
    fireEvent.change(code, { target: { value: '287082' } });

    expect(await screen.findByRole('heading', { name: 'mobileApp.password.weakTitle' })).toBeTruthy();
    expect(mobile.verifySecondFactor).toHaveBeenCalledWith('totp', '287082');
    expect(screen.getByText('mobileApp.password.weakPwned')).toBeTruthy();
    expect(screen.getByText('alexander.with.a.very.long.address@example.com')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.changeInSafari' }));
    expect(mobile.openExternal).toHaveBeenCalledWith('https://avibe.bot/app/account/security');

    fireEvent.click(screen.getByRole('button', { name: 'common.continue' }));
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    // Once: a later session read goes straight to the instances.
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.instances.refresh' }));
    await waitFor(() => expect(mobile.refresh).toHaveBeenCalledTimes(1));
    await settle();
    expect(screen.queryByRole('heading', { name: 'mobileApp.password.weakTitle' })).toBeNull();
    expect(screen.getByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
  });

  it('names the password rules for other weak reasons and skips the notice without one', async () => {
    mobile.signInWithPassword.mockResolvedValueOnce({
      ...signedIn(),
      weak_password: { reasons: ['length'] },
    });
    let password = await reachPassword();
    fireEvent.change(password, { target: { value: 'short' } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));
    expect(await screen.findByText('mobileApp.password.weakRules')).toBeTruthy();
    expect(screen.queryByText('mobileApp.password.weakPwned')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'common.continue' }));
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    cleanup();

    resetMobileMocks();
    password = await reachPassword();
    fireEvent.change(password, { target: { value: 'correct-horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'mobileApp.password.weakTitle' })).toBeNull();
  });

  it('gives a new password sign-in its own second-factor throttle window', async () => {
    const user = userEvent.setup();
    mobile.bootstrap.mockResolvedValueOnce(owed({ methods: ['totp'] }));
    mobile.verifySecondFactor.mockRejectedValueOnce({
      code: 'rate_limited',
      message: 'slow down',
      retry_after_seconds: 900,
    });
    render(<MobileOnboarding />);
    await user.type(await screen.findByLabelText('mobileApp.twoFactor.totpLabel'), '287082');
    expect((await screen.findByRole('alert')).textContent).toBe('mobileApp.twoFactor.rateLimited:15');
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.differentAccount' }));

    // The next account signs in with its password and owes its own second factor.
    mobile.signInWithPassword.mockResolvedValueOnce(owed({ methods: ['totp'], email: 'sam@example.com' }));
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
      target: { value: 'sam@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.entry' }));
    fireEvent.change(await screen.findByLabelText('mobileApp.password.label'), {
      target: { value: 'correct-horse' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));

    // Its sixth digit sends the code; the old account's window does not hold it.
    await user.type(await screen.findByLabelText('mobileApp.twoFactor.totpLabel'), '123456');
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(mobile.verifySecondFactor).toHaveBeenCalledTimes(2);
    expect(mobile.verifySecondFactor).toHaveBeenLastCalledWith('totp', '123456');
  });

  it('drops a pending weak-password notice when the sign-in ends before the instances', async () => {
    mobile.signInWithPassword.mockResolvedValueOnce({
      ...owed({ methods: ['totp'] }),
      weak_password: { reasons: ['pwned'] },
    });
    const password = await reachPassword();
    fireEvent.change(password, { target: { value: 'correct-horse' } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.password.signIn' }));
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.twoFactor.differentAccount' }));

    // A new email sign-in owes no notice for the old password.
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
      target: { value: 'alex@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    fireEvent.change(await screen.findByLabelText('mobileApp.code.label'), {
      target: { value: '123456' },
    });
    expect(await screen.findByRole('button', { name: /Personal Avibe/ })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'mobileApp.password.weakTitle' })).toBeNull();
    expect(mobile.logout).toHaveBeenCalledTimes(1);
  });

  describe('open-source licenses', () => {
    const licensesData = {
      schema_version: 1 as const,
      native: { state: 'published' as const, release_tag: 'iroh-apple-v1.2.0-1' },
      web: { sha256: 'a'.repeat(64), packages: 1 },
      components: 3,
      sources: [
        {
          version: '0.31.2',
          upstream: 'https://github.com/mozilla/uniffi-rs/tree/0000000000000000000000000000000000000000',
          crates: [
            { name: 'uniffi', registry: 'https://static.crates.io/crates/uniffi/uniffi-0.31.2.crate' },
            { name: 'uniffi_core', registry: 'https://static.crates.io/crates/uniffi_core/uniffi_core-0.31.2.crate' },
          ],
        },
      ],
      groups: [
        {
          title: 'MIT',
          text: 'Copyright (c) 2018 Carl Lerche\n\nPermission is hereby granted.',
          components: [{ name: 'bytes', version: '1.10.1', origin: 'native' as const }],
        },
        {
          title: 'MIT',
          text: 'Permission is hereby granted.',
          components: [
            { name: 'anyhow', version: '1.0.98', origin: 'native' as const },
            { name: 'objc2', version: '0.6.5', origin: 'native' as const, authors: ['Mads Marquart'] },
          ],
        },
      ],
    };

    it('shows no About entry when the App carries no license data', async () => {
      await openAccount('#account');
      await settle();
      expect(screen.queryByRole('button', { name: 'mobileApp.licenses.title' })).toBeNull();
    });

    it('opens the page from Account, keeps each text as its own group, and opens source links natively', async () => {
      appLicenses.load.mockResolvedValue(licensesData);
      mobile.openExternal.mockResolvedValue({ opened: true });
      await openAccount('#account');
      fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.licenses.title' }));

      expect(await screen.findByRole('heading', { name: 'mobileApp.licenses.title' })).toBeTruthy();
      expect(screen.getAllByText('MIT')).toHaveLength(2);
      // Components of one text are listed together, with declared authors for a canonical text.
      expect(
        screen.getByText(
          'mobileApp.licenses.crate:anyhowmobileApp.licenses.separatormobileApp.licenses.withAuthors',
        ),
      ).toBeTruthy();
      expect(screen.getByText('mobileApp.licenses.crate:bytes')).toBeTruthy();
      expect(screen.getByText('Copyright (c) 2018 Carl Lerche')).toBeTruthy();

      fireEvent.click(screen.getByRole('link', { name: 'mobileApp.licenses.sourceFor:mobileApp.licenses.family:uniffi' }));
      fireEvent.click(screen.getByRole('link', { name: 'mobileApp.licenses.archiveFor:mobileApp.licenses.crate:uniffi_core' }));
      expect(mobile.openExternal.mock.calls).toEqual([
        ['https://github.com/mozilla/uniffi-rs/tree/0000000000000000000000000000000000000000'],
        ['https://static.crates.io/crates/uniffi_core/uniffi_core-0.31.2.crate'],
      ]);

      fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
      expect(await screen.findByRole('heading', { name: 'mobileApp.account.title' })).toBeTruthy();
    });
  });
});

// The instance sheet native opens over the selected instance (ui/design: mobile-app/switcher/*).
describe('mobile instance sheet', () => {
  const lab = {
    id: 'lab',
    name: 'Lab',
    host: 'lab-app.avibe.bot',
    url: 'https://lab-app.avibe.bot',
    relationship: 'owner' as const,
    owner_email: null,
    available: true,
  };
  /** Native's snapshot: the selected instance and the rest by recent use on this device. */
  const inSheet = (): Extract<MobileSessionState, { status: 'signedIn' }> => {
    const base = signedIn();
    return { ...base, selected_instance_id: 'owned', instances: [base.instances[0], lab, base.instances[1]] };
  };
  const openSheet = async (state = inSheet()) => {
    window.history.replaceState({}, '', '/#sheet');
    mobile.bootstrap.mockResolvedValueOnce(state);
    mobile.refresh.mockResolvedValue(state);
    render(<MobileOnboarding />);
    // The sheet's header is drawn from the first frame; the list once the snapshot is read.
    await screen.findByText('mobileApp.sheet.youreIn');
  };
  const rowNames = () => Array.from(document.querySelectorAll('main button'))
    .map((button) => button.textContent ?? '')
    .filter((text) => /Personal Avibe|Lab|Shared Research Avibe/.test(text));

  it('opens on the list over the current instance, never on Account or the first-use page', async () => {
    await openSheet();
    expect(screen.queryByRole('heading', { name: 'mobileApp.account.title', level: 1 })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'mobileApp.instances.title' })).toBeNull();
    expect(screen.getByText('mobileApp.sheet.youreIn')).toBeTruthy();
    // The current instance first, then the others in native's recent order.
    const names = ['Personal Avibe', 'Lab', 'Shared Research Avibe'];
    expect(rowNames().map((text) => names.find((name) => text.startsWith(name)))).toEqual([
      'Personal Avibe', 'Lab', 'Shared Research Avibe',
    ]);
    // One background refresh, while the list is already usable.
    await waitFor(() => expect(mobile.refresh).toHaveBeenCalledTimes(1));
  });

  it('scrolls its document, so native can hand a pull at the top to the sheet', async () => {
    await openSheet();
    const main = screen.getByRole('main');
    expect(main.classList.contains('h-dvh')).toBe(false);
    expect(main.classList.contains('overflow-y-auto')).toBe(false);
  });

  it('returns to the current instance with Close or its row, without selecting it again', async () => {
    await openSheet();
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.sheet.close:Personal Avibe' }));
    await waitFor(() => expect(mobile.dismiss).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.sheet.returnTo:Personal Avibe' }));
    await waitFor(() => expect(mobile.dismiss).toHaveBeenCalledTimes(2));
    expect(mobile.selectInstance).not.toHaveBeenCalled();
  });

  it('opens another instance, and reports a failure on its row with a retry', async () => {
    await openSheet();
    const opening = deferred<{ instance: typeof lab }>();
    mobile.selectInstance.mockReturnValueOnce(opening.promise);
    fireEvent.click(screen.getByRole('button', { name: /^Lab/ }));
    await waitFor(() => expect(mobile.selectInstance).toHaveBeenCalledWith('lab'));
    expect(await screen.findByText('mobileApp.sheet.opening')).toBeTruthy();

    opening.reject({ code: 'network', message: 'The instance did not answer.' });
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('mobileApp.sheet.openFailed:Lab');
    // The list stays; the current instance is still one tap away.
    expect(screen.getByRole('button', { name: 'mobileApp.sheet.returnTo:Personal Avibe' })).toBeTruthy();

    mobile.selectInstance.mockResolvedValueOnce({ instance: lab });
    fireEvent.click(screen.getByRole('button', { name: 'common.retry' }));
    await waitFor(() => expect(mobile.selectInstance).toHaveBeenCalledTimes(2));
    expect(mobile.dismiss).not.toHaveBeenCalled();
  });

  it('explains an offline instance in place instead of opening it', async () => {
    await openSheet();
    const offline = screen.getByRole('button', { name: /Shared Research Avibe/ });
    fireEvent.click(offline);
    expect(offline.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('mobileApp.sheet.offlineBody:Shared Research Avibe')).toBeTruthy();
    expect(mobile.selectInstance).not.toHaveBeenCalled();
  });

  it('keeps the list usable when its refresh fails, with a banner that retries', async () => {
    window.history.replaceState({}, '', '/#sheet');
    mobile.bootstrap.mockResolvedValueOnce(inSheet());
    mobile.refresh.mockRejectedValueOnce({ code: 'network', message: 'offline' });
    render(<MobileOnboarding />);
    expect(await screen.findByText('mobileApp.sheet.stale')).toBeTruthy();
    // No blanket "can't reach avibe.bot": the cached list is still there.
    expect(screen.queryByText('mobileApp.errors.network')).toBeNull();
    expect(screen.getByRole('button', { name: /^Lab/ })).toBeTruthy();

    mobile.refresh.mockResolvedValueOnce(inSheet());
    fireEvent.click(screen.getByRole('button', { name: 'common.retry' }));
    await waitFor(() => expect(screen.queryByText('mobileApp.sheet.stale')).toBeNull());
    expect(mobile.refresh).toHaveBeenCalledTimes(2);
  });

  it('pushes Account and Add inside the sheet, each with a Back the edge swipe presses', async () => {
    await openSheet();
    fireEvent.click(screen.getByRole('button', { name: /alexander\.with\.a\.very\.long/ }));
    await screen.findByRole('heading', { name: 'mobileApp.account.title' });
    // The list already names the current instance; Account does not repeat it.
    expect(screen.queryByText('mobileApp.account.current')).toBeNull();
    const back = screen.getByRole('button', { name: 'common.back' });
    expect(back.hasAttribute('data-mobile-back')).toBe(true);
    fireEvent.click(back);
    await screen.findByRole('heading', { name: 'mobileApp.sheet.title' });
    expect(mobile.returnToInstance).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /mobileApp\.sheet\.add/ }));
    await screen.findByRole('heading', { name: 'mobileApp.empty.title' });
    fireEvent.click(screen.getByRole('button', { name: 'common.back' }));
    await screen.findByRole('heading', { name: 'mobileApp.sheet.title' });
  });

  it('waits for the background refresh before it opens an instance or signs out', async () => {
    const refreshing = deferred<MobileSessionState>();
    window.history.replaceState({}, '', '/#sheet');
    mobile.bootstrap.mockResolvedValueOnce(inSheet());
    mobile.refresh.mockReturnValueOnce(refreshing.promise);
    render(<MobileOnboarding />);
    await screen.findByText('mobileApp.sheet.updating');

    fireEvent.click(screen.getByRole('button', { name: /^Lab/ }));
    expect(await screen.findByText('mobileApp.sheet.opening')).toBeTruthy();
    // Native runs one account operation at a time: the open waits rather than failing as busy.
    expect(mobile.selectInstance).not.toHaveBeenCalled();
    refreshing.resolve(inSheet());
    await waitFor(() => expect(mobile.selectInstance).toHaveBeenCalledWith('lab'));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('signs out and refreshes again only once the background refresh has settled', async () => {
    const refreshing = deferred<MobileSessionState>();
    window.history.replaceState({}, '', '/#sheet');
    mobile.bootstrap.mockResolvedValueOnce(inSheet());
    mobile.refresh.mockReturnValueOnce(refreshing.promise);
    render(<MobileOnboarding />);
    await screen.findByText('mobileApp.sheet.updating');

    fireEvent.click(screen.getByRole('button', { name: /mobileApp\.sheet\.add/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.empty.refresh' }));
    await Promise.resolve();
    expect(mobile.refresh).toHaveBeenCalledTimes(1);
    refreshing.resolve(inSheet());
    await waitFor(() => expect(mobile.refresh).toHaveBeenCalledTimes(2));
    cleanup();

    resetMobileMocks();
    const again = deferred<MobileSessionState>();
    window.history.replaceState({}, '', '/#sheet');
    mobile.bootstrap.mockResolvedValueOnce(inSheet());
    mobile.refresh.mockReturnValueOnce(again.promise);
    render(<MobileOnboarding />);
    await screen.findByText('mobileApp.sheet.updating');
    fireEvent.click(screen.getByRole('button', { name: /alexander\.with\.a\.very\.long/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.account.signOut' }));
    await Promise.resolve();
    expect(mobile.logout).not.toHaveBeenCalled();
    again.resolve(inSheet());
    await waitFor(() => expect(mobile.logout).toHaveBeenCalledTimes(1));
  });

  it('joins a second refresh to the one in flight', async () => {
    const refreshing = deferred<MobileSessionState>();
    window.history.replaceState({}, '', '/#sheet');
    mobile.bootstrap.mockResolvedValueOnce(inSheet());
    mobile.refresh.mockReturnValueOnce(refreshing.promise);
    render(<MobileOnboarding />);
    await screen.findByText('mobileApp.sheet.updating');
    fireEvent.click(screen.getByRole('button', { name: /Shared Research Avibe/ }));
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.sheet.refreshList' }));
    await Promise.resolve();
    expect(mobile.refresh).toHaveBeenCalledTimes(1);

    refreshing.resolve(inSheet());
    await waitFor(() => expect(screen.queryByText('mobileApp.sheet.updating')).toBeNull());
    expect(screen.queryByText('mobileApp.sheet.stale')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /^Lab/ }));
    await waitFor(() => expect(mobile.selectInstance).toHaveBeenCalledWith('lab'));
  });

  it('returns a refresh from Add an instance to the sheet list', async () => {
    await openSheet();
    await waitFor(() => expect(mobile.refresh).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: /mobileApp\.sheet\.add/ }));
    await screen.findByRole('heading', { name: 'mobileApp.empty.title' });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.empty.refresh' }));
    await waitFor(() => expect(mobile.refresh).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('heading', { name: 'mobileApp.sheet.title' })).toBeTruthy();
    // Still the sheet: Close returns to the instance.
    expect(screen.getByRole('button', { name: 'mobileApp.sheet.close:Personal Avibe' })).toBeTruthy();
  });

  it('pulls to refresh only on the full-screen list', async () => {
    mobile.bootstrap.mockResolvedValueOnce(signedIn());
    render(<MobileOnboarding />);
    await screen.findByRole('heading', { name: 'mobileApp.instances.title' });
    // Once the first read has settled and the list is live.
    await waitFor(() => expect(
      (screen.getByRole('button', { name: 'mobileApp.instances.refresh' }) as HTMLButtonElement).disabled,
    ).toBe(false));
    const main = screen.getByRole('main');
    fireEvent.touchStart(main, { touches: [{ clientY: 10 }] });
    fireEvent.touchMove(main, { touches: [{ clientY: 100 }] });
    expect(screen.getByText('mobileApp.sheet.releaseToRefresh')).toBeTruthy();
    fireEvent.touchEnd(main);
    await waitFor(() => expect(mobile.refresh).toHaveBeenCalledTimes(1));
    // A short pull, or one that starts below the top, refreshes nothing.
    await waitFor(() => expect(
      (screen.getByRole('button', { name: 'mobileApp.instances.refresh' }) as HTMLButtonElement).disabled,
    ).toBe(false));
    fireEvent.touchStart(main, { touches: [{ clientY: 10 }] });
    fireEvent.touchMove(main, { touches: [{ clientY: 40 }] });
    fireEvent.touchEnd(main);
    Object.defineProperty(main, 'scrollTop', { configurable: true, value: 200 });
    fireEvent.touchStart(main, { touches: [{ clientY: 10 }] });
    fireEvent.touchMove(main, { touches: [{ clientY: 100 }] });
    fireEvent.touchEnd(main);
    expect(mobile.refresh).toHaveBeenCalledTimes(1);
    // Scrolling back up to the top counts only the travel past the top as a pull.
    fireEvent.touchStart(main, { touches: [{ clientY: 10 }] });
    fireEvent.touchMove(main, { touches: [{ clientY: 60 }] });
    Object.defineProperty(main, 'scrollTop', { configurable: true, value: 0 });
    fireEvent.touchMove(main, { touches: [{ clientY: 111 }] });
    fireEvent.touchEnd(main);
    expect(mobile.refresh).toHaveBeenCalledTimes(1);
    cleanup();

    resetMobileMocks();
    await openSheet();
    await waitFor(() => expect(mobile.refresh).toHaveBeenCalledTimes(1));
    const sheetMain = screen.getByRole('main');
    fireEvent.touchStart(sheetMain, { touches: [{ clientY: 10 }] });
    fireEvent.touchMove(sheetMain, { touches: [{ clientY: 100 }] });
    fireEvent.touchEnd(sheetMain);
    expect(mobile.refresh).toHaveBeenCalledTimes(1);
  });
});

// A second factor owed at launch, after which native has restored the saved selection (E4).
describe('mobile launch second factor', () => {
  const passAtLaunch = async (
    owedEmail: string | null,
    after: Extract<MobileSessionState, { status: 'signedIn' }>,
  ) => {
    mobile.bootstrap.mockResolvedValueOnce(owed({ email: owedEmail, methods: ['totp'] }));
    mobile.verifySecondFactor.mockResolvedValueOnce(after);
    render(<MobileOnboarding />);
    fireEvent.change(await screen.findByLabelText('mobileApp.twoFactor.totpLabel'), {
      target: { value: '287082' },
    });
    await waitFor(() => expect(mobile.verifySecondFactor).toHaveBeenCalledWith('totp', '287082'));
  };
  const email = signedIn().account.email;

  it('continues into the restored instance', async () => {
    await passAtLaunch(email, { ...signedIn(), selected_instance_id: 'owned' });
    await waitFor(() => expect(mobile.selectInstance).toHaveBeenCalledWith('owned'));
    expect(mobile.selectInstance).toHaveBeenCalledTimes(1);
  });

  it('shows the list when nothing was restored', async () => {
    await passAtLaunch(email, signedIn());
    expect(await screen.findByRole('heading', { name: 'mobileApp.instances.title' })).toBeTruthy();
    expect(mobile.selectInstance).not.toHaveBeenCalled();
  });

  it('shows the list when another account passed', async () => {
    await passAtLaunch('alex@example.com', { ...signedIn(), selected_instance_id: 'owned' });
    expect(await screen.findByRole('heading', { name: 'mobileApp.instances.switchTitle' })).toBeTruthy();
    expect(mobile.selectInstance).not.toHaveBeenCalled();
  });

  it('shows the list after a second factor owed by a sign-in on this page', async () => {
    // Owed at launch, but the person signs in again on this page instead.
    mobile.bootstrap.mockResolvedValueOnce(owed({ email, methods: ['totp'] }));
    mobile.verifyOtp.mockResolvedValueOnce(owed({ email, methods: ['totp'] }));
    mobile.verifySecondFactor.mockResolvedValueOnce({ ...signedIn(), selected_instance_id: 'owned' });
    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.twoFactor.differentAccount' }));
    fireEvent.click(await screen.findByRole('button', { name: 'mobileApp.welcome.action' }));
    fireEvent.change(screen.getByLabelText('mobileApp.email.label'), { target: { value: email } });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));
    fireEvent.change(await screen.findByLabelText('mobileApp.code.label'), { target: { value: '123456' } });
    fireEvent.change(await screen.findByLabelText('mobileApp.twoFactor.totpLabel'), {
      target: { value: '287082' },
    });
    expect(await screen.findByRole('heading', { name: 'mobileApp.instances.switchTitle' })).toBeTruthy();
    expect(mobile.selectInstance).not.toHaveBeenCalled();
  });
});
