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
}));

vi.mock('./bridge', () => ({
  bootstrapMobileSession: mobile.bootstrap,
  logoutMobileSession: mobile.logout,
  openMobileExternal: mobile.openExternal,
  refreshMobileSession: mobile.refresh,
  requestMobileOtp: mobile.requestOtp,
  selectMobileInstance: mobile.selectInstance,
  verifyMobileOtp: mobile.verifyOtp,
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

const resetMobileMocks = () => {
  mobile.bootstrap.mockReset();
  mobile.logout.mockReset();
  mobile.openExternal.mockReset();
  mobile.refresh.mockReset();
  mobile.requestOtp.mockReset();
  mobile.selectInstance.mockReset();
  mobile.verifyOtp.mockReset();
  mobile.bootstrap.mockResolvedValue({ status: 'signedOut' });
  mobile.logout.mockResolvedValue({ status: 'signedOut' });
  mobile.refresh.mockResolvedValue(signedIn());
  mobile.requestOtp.mockResolvedValue({ cooldown_seconds: 0 });
  mobile.selectInstance.mockResolvedValue({ instance: signedIn().instances[0] });
  mobile.verifyOtp.mockResolvedValue(signedIn());
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

beforeEach(() => {
  window.history.replaceState({}, '', '/');
  resetMobileMocks();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('mobile onboarding', () => {
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
    fireEvent.change(code, { target: { value: '12 3a456' } });

    expect((code as HTMLInputElement).value).toBe('123456');
    expect(screen.getAllByText(/^[1-6]$/)).toHaveLength(6);
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
});
