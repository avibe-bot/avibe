/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MobileBridgeError, MobileSessionState } from './bridge';

const mobile = vi.hoisted(() => ({
  bootstrap: vi.fn<() => Promise<MobileSessionState>>(),
  logout: vi.fn<() => Promise<MobileSessionState>>(),
  openExternal: vi.fn(),
  refresh: vi.fn<() => Promise<MobileSessionState>>(),
  requestOtp: vi.fn(),
  selectInstance: vi.fn(),
  verifyOtp: vi.fn<() => Promise<MobileSessionState>>(),
  verifyPasskey: vi.fn<() => Promise<MobileSessionState>>(),
  verifySecondFactor: vi.fn<(method: string, code: string) => Promise<MobileSessionState>>(),
}));

vi.mock('./bridge', () => ({
  ACCOUNT_SECURITY_URL: 'https://avibe.bot/app/account/security',
  bootstrapMobileSession: mobile.bootstrap,
  logoutMobileSession: mobile.logout,
  openMobileExternal: mobile.openExternal,
  refreshMobileSession: mobile.refresh,
  requestMobileOtp: mobile.requestOtp,
  selectMobileInstance: mobile.selectInstance,
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
  mobile.bootstrap.mockReset();
  mobile.logout.mockReset();
  mobile.openExternal.mockReset();
  mobile.refresh.mockReset();
  mobile.requestOtp.mockReset();
  mobile.selectInstance.mockReset();
  mobile.verifyOtp.mockReset();
  mobile.verifyPasskey.mockReset();
  mobile.verifySecondFactor.mockReset();
  mobile.bootstrap.mockResolvedValue({ status: 'signedOut' });
  mobile.logout.mockResolvedValue({ status: 'signedOut' });
  mobile.refresh.mockResolvedValue(signedIn());
  mobile.requestOtp.mockResolvedValue({ cooldown_seconds: 0 });
  mobile.selectInstance.mockResolvedValue({ instance: signedIn().instances[0] });
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
    'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]',
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
  if (
    (operation === 'request' || operation === 'resend')
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
  fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));

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
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));
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
          } else {
            fireEvent.change(await screen.findByLabelText('mobileApp.twoFactor.totpLabel'), {
              target: { value: '287082' },
            });
          }
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.verify' }));
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

        const expectedRequestCalls = operation === 'request'
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
        fireEvent.change(code, { target: { value: '123456' } });
        if (action === 'resend') {
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.resend' }));
        } else {
          fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));
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
        expect(mobile.requestOtp).toHaveBeenCalledTimes(action === 'resend' ? 2 : 1);
        expect(mobile.verifyOtp).toHaveBeenCalledTimes(action === 'verify' ? 1 : 0);
      }
    }
  });

  it('keeps successful send cooldowns scoped to another send', async () => {
    mobile.requestOtp.mockResolvedValueOnce({ cooldown_seconds: 9 });

    render(<MobileOnboarding />);
    fireEvent.click(await screen.findByRole('button', {
      name: 'mobileApp.welcome.action',
    }));
    fireEvent.change(screen.getByLabelText('mobileApp.email.label'), {
      target: { value: 'alex@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.email.send' }));

    const code = await screen.findByLabelText('mobileApp.code.label');
    fireEvent.change(code, { target: { value: '123456' } });

    const verify = screen.getByRole('button', { name: 'mobileApp.code.verify' });
    const resend = screen.getByRole('button', {
      name: 'mobileApp.code.resendIn:9',
    });
    expect((verify as HTMLButtonElement).disabled).toBe(false);
    expect((resend as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(verify);
    fireEvent.click(resend);
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
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));

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
    expect(
      (screen.getByRole('button', { name: 'mobileApp.code.verify' }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
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
    await user.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));

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

    expect(await screen.findByText('mobileApp.instances.sharedBy:owner@example.com')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /Shared Research Avibe/ }));
    expect(mobile.selectInstance).not.toHaveBeenCalled();
    expect(screen.getByText('mobileApp.instances.offlineTitle:Shared Research Avibe')).toBeTruthy();

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
    const code = await screen.findByLabelText('mobileApp.code.label');
    await user.type(code, '123456');
    mobile.verifyOtp.mockReturnValueOnce(deferred<MobileSessionState>().promise);
    await user.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));
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
    await user.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));

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
    await user.click(screen.getByRole('button', { name: 'mobileApp.code.verify' }));

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
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.verify' }));

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

    for (const expected of [
      'mobileApp.twoFactor.codeInvalid',
      'mobileApp.twoFactor.rateLimited:15',
      'mobileApp.twoFactor.rateLimited:15',
      'mobileApp.errors.network',
    ]) {
      fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.verify' }));
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
    fireEvent.change(screen.getByLabelText('mobileApp.twoFactor.totpLabel'), {
      target: { value: '287082' },
    });
    mobile.verifySecondFactor.mockReturnValueOnce(deferred<MobileSessionState>().promise);
    fireEvent.click(screen.getByRole('button', { name: 'mobileApp.twoFactor.verify' }));
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
});
