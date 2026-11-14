/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import {
  COMPUTER_USE_GUIDE_STEPS,
  computerUseGuideProgress,
  readComputerUseStatus,
} from '../workbench/computerUseStatus';
import { computerUseStatusCopy } from '../workbench/computerUseStatusCopy';
import { SettingsComputerUsePage } from './SettingsComputerUsePage';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('@/lib/apiFetch', () => ({ apiFetch }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const statusResponse = (body: Record<string, unknown>) => ({ ok: true, json: async () => body });

const enterDesktopShell = () =>
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });

// A shell that answers the guide's actions, recording every native call.
const enterActionableShell = (invoke = vi.fn(async (_command: string, _args?: Record<string, unknown>) => undefined)) => {
  enterDesktopShell();
  Object.defineProperty(window, '__AVIBE_DESKTOP_COMPUTER_USE__', { value: true, configurable: true });
  Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
  return invoke;
};

// The browser's own OS, which must never decide the host the page describes.
const browserOn = (os: 'windows' | 'mac') => {
  Object.defineProperty(window.navigator, 'platform', { value: os === 'windows' ? 'Win32' : 'MacIntel', configurable: true });
  Object.defineProperty(window.navigator, 'userAgent', {
    value: os === 'windows'
      ? 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Edg/131.0'
      : 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15',
    configurable: true,
  });
};

const windowsHost = { supported: true, host_platform: 'windows' };

const setVisibility = (state: DocumentVisibilityState) => {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
};

// Answers each status read with the next body, then keeps the last one.
const statusSequence = (...bodies: Record<string, unknown>[]) => {
  let index = 0;
  apiFetch.mockImplementation(async () => statusResponse(bodies[Math.min(index++, bodies.length - 1)]));
};

const stepStates = () => Object.fromEntries(
  within(screen.getByTestId('computer-use-steps')).getAllByRole('listitem').map((item) => [
    item.getAttribute('data-step'),
    item.getAttribute('data-step-state'),
  ]),
);

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  apiFetch.mockReset();
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_COMPUTER_USE__');
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
  Reflect.deleteProperty(document, 'visibilityState');
  Reflect.deleteProperty(window.navigator, 'platform');
  Reflect.deleteProperty(window.navigator, 'userAgent');
});

describe('computerUseGuideProgress', () => {
  const progress = (status: string, reason: string | null = null) =>
    COMPUTER_USE_GUIDE_STEPS.map((step) => computerUseGuideProgress({ status, reason })[step]);

  it('advances the steps only as far as the status proves', () => {
    expect(progress('off', 'never_enabled')).toEqual(['current', 'pending', 'pending']);
    expect(progress('needs_permission', 'accessibility')).toEqual(['complete', 'current', 'pending']);
    // The driver checks Accessibility first, so a Screen Recording gap proves it passed.
    expect(progress('needs_permission', 'screen_recording')).toEqual(['complete', 'complete', 'current']);
    expect(progress('needs_permission', null)).toEqual(['complete', 'current', 'pending']);
    expect(progress('ready')).toEqual(['complete', 'complete', 'complete']);
  });

  it('claims no permission progress for states that happen before or beside the permission checks', () => {
    for (const status of ['starting', 'needs_runtime', 'error']) {
      expect(progress(status, 'any')).toEqual(['complete', 'pending', 'pending']);
    }
    expect(progress('unavailable', 'shell_not_running')).toEqual(['pending', 'pending', 'pending']);
    // An unknown state reads as off, matching the status copy's fallback.
    expect(progress('future_state')).toEqual(['current', 'pending', 'pending']);
  });
});

describe('SettingsComputerUsePage', () => {
  it('renders the numbered guide inside the desktop shell, driven by the status API', async () => {
    enterDesktopShell();
    apiFetch.mockResolvedValue(statusResponse({
      supported: true,
      status: 'needs_permission',
      reason: 'screen_recording',
    }));

    render(<SettingsComputerUsePage />);

    await waitFor(() => {
      expect(screen.getByTestId('computer-use-guide-status').getAttribute('data-computer-use-status'))
        .toBe('needs_permission');
    });
    expect(apiFetch).toHaveBeenCalledWith('/api/desktop/computer-use/status', { cache: 'no-store' });
    expect(screen.getByText('workbench.home.computerUse.screenRecordingDetail')).toBeTruthy();
    expect(stepStates()).toEqual({
      enable: 'complete',
      accessibility: 'complete',
      screenRecording: 'current',
    });
    expect(screen.getAllByRole('listitem').find((item) => item.getAttribute('aria-current') === 'step')
      ?.getAttribute('data-step')).toBe('screenRecording');
    expect(screen.queryByTestId('computer-use-unsupported')).toBeNull();
  });

  it('rechecks the status API on demand and moves the steps forward', async () => {
    enterDesktopShell();
    apiFetch.mockResolvedValue(statusResponse({
      supported: true,
      status: 'needs_permission',
      reason: 'accessibility',
    }));
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);
    await waitFor(() => expect(stepStates().accessibility).toBe('current'));
    const callsBefore = apiFetch.mock.calls.length;

    apiFetch.mockResolvedValue(statusResponse({ supported: true, status: 'ready', reason: null }));
    await user.click(screen.getByRole('button', { name: /settings\.computerUse\.recheck/ }));

    await waitFor(() => expect(stepStates().screenRecording).toBe('complete'));
    expect(apiFetch.mock.calls.length).toBeGreaterThan(callsBefore);
    expect(Object.values(stepStates())).toEqual(['complete', 'complete', 'complete']);
  });

  it('gives each actionable step its native action', async () => {
    const invoke = enterActionableShell();
    statusSequence({ supported: true, status: 'off', reason: 'never_enabled' });
    const user = userEvent.setup();

    const { unmount } = render(<SettingsComputerUsePage />);
    await user.click(await screen.findByTestId('computer-use-action-enable'));
    expect(invoke).toHaveBeenLastCalledWith('computer_use_enable');
    expect(screen.getByTestId('computer-use-action-accessibility')).toBeTruthy();
    expect(screen.getByTestId('computer-use-action-screenRecording')).toBeTruthy();
    unmount();

    statusSequence({ supported: true, status: 'needs_permission', reason: 'accessibility' });
    const second = render(<SettingsComputerUsePage />);
    await user.click(await screen.findByTestId('computer-use-action-accessibility'));
    expect(invoke).toHaveBeenLastCalledWith('computer_use_open_permission', { pane: 'accessibility' });
    expect(screen.queryByTestId('computer-use-action-enable')).toBeNull();
    second.unmount();

    statusSequence({ supported: true, status: 'needs_permission', reason: 'screen_recording' });
    render(<SettingsComputerUsePage />);
    await user.click(await screen.findByTestId('computer-use-action-screenRecording'));
    expect(invoke).toHaveBeenLastCalledWith('computer_use_open_permission', { pane: 'screen_recording' });
  });

  it.each([
    ['error', 'driver_not_ready', false, false],
    ['starting', null, false, false],
    ['needs_runtime', null, false, false],
    ['needs_permission', 'accessibility', false, false],
    ['needs_permission', 'screen_recording', true, false],
    ['unavailable', 'invalid_state_file', false, false],
    ['unavailable', 'snapshot_invalid', false, false],
    ['unavailable', 'shell_not_running', false, false],
    ['unavailable', 'controller_unavailable', false, false],
    ['ready', null, true, true],
  ])('offers native settings for every incomplete permission step in %s (%s)', async (
    status, reason, accessibilityComplete, screenRecordingComplete,
  ) => {
    const invoke = enterActionableShell();
    statusSequence({ supported: true, status, reason });
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);
    await waitFor(() => expect(
      screen.getByTestId('computer-use-guide-status').getAttribute('data-computer-use-status'),
    ).toBe(status));
    expect(screen.queryByTestId('computer-use-action-enable')).toBeNull();

    for (const [step, pane, complete] of [
      ['accessibility', 'accessibility', accessibilityComplete],
      ['screenRecording', 'screen_recording', screenRecordingComplete],
    ] as const) {
      if (complete) {
        expect(screen.queryByTestId(`computer-use-action-${step}`)).toBeNull();
      } else {
        const button = screen.getByTestId(`computer-use-action-${step}`);
        expect((button as HTMLButtonElement).disabled).toBe(false);
        await user.click(button);
        expect(invoke).toHaveBeenLastCalledWith('computer_use_open_permission', { pane });
      }
    }
  });

  it('waits for the first readable status before offering native actions', async () => {
    enterActionableShell();
    let finish!: (response: ReturnType<typeof statusResponse>) => void;
    apiFetch.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    apiFetch.mockResolvedValue(statusResponse({ supported: true, status: 'error', reason: 'driver_not_ready' }));
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);
    expect(screen.queryAllByTestId(/computer-use-action-/)).toHaveLength(0);
    // An unreadable response still provides no progress; keep waiting.
    await act(async () => finish(statusResponse({})));
    expect(screen.queryAllByTestId(/computer-use-action-/)).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: /settings\.computerUse\.recheck/ }));
    expect(await screen.findByTestId('computer-use-action-accessibility')).toBeTruthy();
    expect(screen.getByTestId('computer-use-action-screenRecording')).toBeTruthy();
    expect(screen.queryByTestId('computer-use-action-enable')).toBeNull();
  });

  it('disables all permission actions until the running native action settles', async () => {
    let finish!: () => void;
    const invoke = enterActionableShell(vi.fn(() => new Promise<void>((resolve) => { finish = resolve; })));
    statusSequence({ supported: true, status: 'error', reason: 'driver_not_ready' });
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);
    await waitFor(() => expect(stepStates().enable).toBe('complete'));
    const accessibility = screen.getByTestId('computer-use-action-accessibility') as HTMLButtonElement;
    const screenRecording = screen.getByTestId('computer-use-action-screenRecording') as HTMLButtonElement;
    await user.click(accessibility);
    expect(accessibility.disabled).toBe(true);
    expect(screenRecording.disabled).toBe(true);
    await user.click(screenRecording);
    expect(invoke).toHaveBeenCalledTimes(1);

    await act(async () => finish());
    expect(accessibility.disabled).toBe(false);
    expect(screenRecording.disabled).toBe(false);
  });

  it('flips each step to done as the polled status advances, without a recheck', async () => {
    enterActionableShell();
    statusSequence(
      { supported: true, status: 'off', reason: 'never_enabled' },
      { supported: true, status: 'needs_permission', reason: 'accessibility' },
      { supported: true, status: 'needs_permission', reason: 'screen_recording' },
      { supported: true, status: 'ready', reason: null },
    );
    vi.useFakeTimers();

    render(<SettingsComputerUsePage />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(stepStates()).toEqual({ enable: 'current', accessibility: 'pending', screenRecording: 'pending' });

    const expected = [
      { enable: 'complete', accessibility: 'current', screenRecording: 'pending' },
      { enable: 'complete', accessibility: 'complete', screenRecording: 'current' },
      { enable: 'complete', accessibility: 'complete', screenRecording: 'complete' },
    ];
    for (const states of expected) {
      await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
      expect(stepStates()).toEqual(states);
    }
  });

  it('converges when every read is slower than the refresh interval, with one read at a time', async () => {
    enterActionableShell();
    const bodies = [
      { supported: true, status: 'off', reason: 'never_enabled' },
      { supported: true, status: 'needs_permission', reason: 'accessibility' },
      { supported: true, status: 'needs_permission', reason: 'screen_recording' },
      { supported: true, status: 'ready', reason: null },
    ];
    let index = 0;
    let open = 0;
    let maxOpen = 0;
    // `ready` can wait up to 2 s on the Controller: make every read take 2 s.
    apiFetch.mockImplementation(() => {
      open += 1;
      maxOpen = Math.max(maxOpen, open);
      const body = bodies[Math.min(index++, bodies.length - 1)];
      return new Promise((resolve) => {
        window.setTimeout(() => {
          open -= 1;
          resolve(statusResponse(body));
        }, 2_000);
      });
    });
    vi.useFakeTimers();

    render(<SettingsComputerUsePage />);
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(stepStates().enable).toBe('current');
    // Rechecking and focusing mid-read join the read already in flight.
    await act(async () => { await vi.advanceTimersByTimeAsync(2_500); });
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /settings\.computerUse\.recheck/ }));
      window.dispatchEvent(new Event('focus'));
    });

    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(Object.values(stepStates())).toEqual(['complete', 'complete', 'complete']);
    expect(screen.getByTestId('computer-use-guide-status').getAttribute('data-computer-use-status')).toBe('ready');
    expect(maxOpen).toBe(1);
  });

  it('stops polling while the page is hidden and reads again when it returns', async () => {
    enterActionableShell();
    statusSequence({ supported: true, status: 'needs_permission', reason: 'accessibility' });
    vi.useFakeTimers();

    render(<SettingsComputerUsePage />);
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(apiFetch.mock.calls.length).toBeGreaterThanOrEqual(2);

    act(() => setVisibility('hidden'));
    const hiddenAt = apiFetch.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(apiFetch.mock.calls.length).toBe(hiddenAt);

    act(() => setVisibility('visible'));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(apiFetch.mock.calls.length).toBe(hiddenAt + 1);
  });

  it('explains the Quit & Reopen relaunch once Screen Recording is requested, until it is granted', async () => {
    enterActionableShell();
    statusSequence({ supported: true, status: 'needs_permission', reason: 'screen_recording' });
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);
    const action = await screen.findByTestId('computer-use-action-screenRecording');
    expect(screen.queryByTestId('computer-use-relaunch-notice')).toBeNull();

    await user.click(action);
    expect((await screen.findByTestId('computer-use-relaunch-notice')).textContent)
      .toContain('settings.computerUse.relaunchNotice');

    statusSequence({ supported: true, status: 'ready', reason: null });
    await user.click(screen.getByRole('button', { name: /settings\.computerUse\.recheck/ }));
    await waitFor(() => expect(stepStates().screenRecording).toBe('complete'));
    expect(screen.queryByTestId('computer-use-relaunch-notice')).toBeNull();
  });

  it('reports a failed native action without leaving the step', async () => {
    enterActionableShell(vi.fn(async () => { throw new Error('denied'); }));
    statusSequence({ supported: true, status: 'needs_permission', reason: 'accessibility' });
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);
    await user.click(await screen.findByTestId('computer-use-action-accessibility'));

    expect(await screen.findByTestId('computer-use-action-failed')).toBeTruthy();
    expect(stepStates().accessibility).toBe('current');
  });

  it('keeps a failed action on its own step once the guide moves on', async () => {
    enterActionableShell(vi.fn(async () => { throw new Error('denied'); }));
    statusSequence({ supported: true, status: 'needs_permission', reason: 'accessibility' });
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);
    await user.click(await screen.findByTestId('computer-use-action-accessibility'));
    expect(await screen.findByTestId('computer-use-action-failed')).toBeTruthy();

    // The user grants Accessibility another way; the next step never failed.
    statusSequence({ supported: true, status: 'needs_permission', reason: 'screen_recording' });
    await user.click(screen.getByRole('button', { name: /settings\.computerUse\.recheck/ }));
    await waitFor(() => expect(stepStates().screenRecording).toBe('current'));
    expect(screen.getByTestId('computer-use-action-screenRecording')).toBeTruthy();
    expect(screen.queryByTestId('computer-use-action-failed')).toBeNull();
  });

  it('follows the live status read-only in a browser, pointing to the desktop app on the Mac', async () => {
    statusSequence(
      { supported: true, status: 'needs_permission', reason: 'accessibility' },
      { supported: true, status: 'needs_permission', reason: 'screen_recording' },
    );
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);

    expect(screen.getByTestId('computer-use-remote-notice').textContent)
      .toContain('settings.computerUse.remoteNotice');
    await waitFor(() => expect(stepStates().accessibility).toBe('current'));
    expect(screen.queryByTestId(/computer-use-action-/)).toBeNull();
    await user.click(screen.getByRole('button', { name: /settings\.computerUse\.recheck/ }));
    await waitFor(() => expect(stepStates().screenRecording).toBe('current'));
    expect(screen.queryByTestId(/computer-use-action-/)).toBeNull();
  });

  it('keeps a shell without the guide actions read-only instead of offering dead buttons', async () => {
    enterDesktopShell();
    statusSequence({ supported: true, status: 'off', reason: 'never_enabled' });

    render(<SettingsComputerUsePage />);

    await waitFor(() => expect(stepStates().enable).toBe('current'));
    expect(screen.queryByTestId(/computer-use-action-/)).toBeNull();
    expect(screen.queryByTestId('computer-use-remote-notice')).toBeNull();
  });

  it('shows the same notice when the host reports Computer Use unsupported', async () => {
    enterDesktopShell();
    apiFetch.mockResolvedValue(statusResponse({
      supported: false,
      status: 'unavailable',
      reason: 'unsupported_host',
    }));

    render(<SettingsComputerUsePage />);

    expect(await screen.findByTestId('computer-use-unsupported')).toBeTruthy();
    expect(screen.queryByTestId('computer-use-steps')).toBeNull();
  });

  it('shows the desktop-app notice in a browser when the host has no desktop shell', async () => {
    statusSequence({ supported: false, status: 'unavailable', reason: 'unsupported_host' });

    render(<SettingsComputerUsePage />);

    expect(await screen.findByTestId('computer-use-unsupported')).toBeTruthy();
    expect(screen.queryByTestId('computer-use-steps')).toBeNull();
  });

  it('ships the page copy in both locales', () => {
    expect(en.settings.sections.computerUse).toBe('Computer Use');
    expect(zh.settings.sections.computerUse).toBe('电脑操作');
    expect(en.settings.computerUse.unsupportedTitle).toBe('Available in the Avibe desktop app');
    expect(zh.settings.computerUse.unsupportedTitle).toBe('仅桌面端可用');
    for (const step of COMPUTER_USE_GUIDE_STEPS) {
      for (const locale of [en, zh]) {
        expect(locale.settings.computerUse.steps[step].title).toBeTruthy();
        expect(locale.settings.computerUse.steps[step].detail).toBeTruthy();
      }
    }
    expect(zh.settings.computerUse.steps.screenRecording.detail).toContain('屏幕与系统音频录制');
    expect(en.settings.computerUse.steps.screenRecording.detail).toContain('Screen & System Audio Recording');
    for (const locale of [en, zh]) {
      const copy = locale.settings.computerUse;
      for (const text of [copy.remoteNotice, copy.relaunchNotice, copy.actionFailed, copy.actions.enable, copy.actions.openSettings]) {
        expect(text).toBeTruthy();
      }
    }
    expect(zh.settings.computerUse.remoteNotice).toContain('请在这台 Mac 上的 Avibe 桌面应用中操作');
    expect(zh.settings.computerUse.relaunchNotice).toContain('Avibe 将重新打开，完成后本页会自动继续');
    expect(zh.settings.computerUse.actions).toEqual({ enable: '开启', openSettings: '去设置' });
  });
});

describe('computerUseStatusCopy on a Windows host', () => {
  const copy = (status: string, reason: string | null, host?: 'macos' | 'windows') =>
    computerUseStatusCopy({ status, reason }, (key) => key, host);

  it('never sends a Windows user to macOS privacy settings', () => {
    for (const reason of ['accessibility', 'screen_recording', null]) {
      expect(copy('needs_permission', reason, 'windows').detail).toBe('workbench.home.computerUse.errorDetail');
    }
    for (const reason of ['ax_capability', 'capture_failed', 'socket_path_too_long']) {
      expect(copy('error', reason, 'windows').detail).toBe('workbench.home.computerUse.errorDetail');
    }
    // Platform-neutral reasons keep their specific detail on Windows.
    expect(copy('error', 'spawn_failed', 'windows').detail).toBe('workbench.home.computerUse.spawnFailedDetail');
  });

  it('explains each Windows admission refusal, keeping a Runtime running elevated apart from elevated targets', () => {
    const details = {
      unsupported_architecture: 'unsupportedArchitectureDetail',
      elevated_runtime: 'elevatedRuntimeDetail',
      session_unavailable: 'sessionUnavailableDetail',
      session_locked: 'sessionLockedDetail',
      session_mismatch: 'sessionMismatchDetail',
      pipe_unverified: 'pipeUnverifiedDetail',
      state_unverified: 'stateUnverifiedDetail',
      lock_unverified: 'lockUnverifiedDetail',
      shell_not_running: 'shellNotRunningDetail',
      daemon_unreachable: 'daemonUnreachableDetail',
    };
    for (const [reason, key] of Object.entries(details)) {
      expect(copy('unavailable', reason, 'windows').detail).toBe(`workbench.home.computerUse.${key}`);
      expect(en.workbench.home.computerUse[key as keyof typeof en.workbench.home.computerUse]).toBeTruthy();
      expect(zh.workbench.home.computerUse[key as keyof typeof zh.workbench.home.computerUse]).toBeTruthy();
    }
    expect(en.workbench.home.computerUse.elevatedRuntimeDetail).toContain('Avibe service is running as administrator');
    expect(en.settings.computerUse.windows.elevated).toContain('App windows running as administrator are refused');
    // A reason this build does not know still reads as the generic notice.
    expect(copy('unavailable', 'future_reason', 'windows').detail).toBe('workbench.home.computerUse.unavailableDetail');
  });

  it('keeps the macOS copy unchanged by default', () => {
    expect(copy('needs_permission', 'accessibility').detail).toBe('workbench.home.computerUse.accessibilityDetail');
    expect(copy('needs_permission', 'screen_recording', 'macos').detail)
      .toBe('workbench.home.computerUse.screenRecordingDetail');
    expect(copy('error', 'ax_capability').detail).toBe('workbench.home.computerUse.axCapabilityDetail');
  });
});

describe('readComputerUseStatus host', () => {
  it('takes the host from the server, reading a missing field as an older macOS server', async () => {
    const read = async (body: Record<string, unknown>) => {
      apiFetch.mockResolvedValueOnce(statusResponse(body));
      return readComputerUseStatus();
    };
    expect(await read({ supported: true, status: 'off', reason: null })).toMatchObject({ host: 'macos' });
    expect(await read({ ...windowsHost, status: 'off', reason: null })).toMatchObject({ host: 'windows' });
    expect(await read({ supported: false, host_platform: 'windows' })).toEqual({ kind: 'unsupported', host: 'windows' });
    expect(await read({ supported: false, host_platform: 'linux' })).toEqual({ kind: 'unsupported', host: 'linux' });
    expect(await read({ supported: false })).toEqual({ kind: 'unsupported', host: 'macos' });
    expect(await read({ supported: true, host_platform: 'beos', status: 'off' })).toMatchObject({ host: 'unknown' });
  });
});

describe('SettingsComputerUsePage on a Windows host', () => {
  it('labels the feature Preview and offers only the switch, without macOS permission steps', async () => {
    const invoke = enterActionableShell();
    statusSequence({ ...windowsHost, status: 'off', reason: 'never_enabled' });
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);

    await waitFor(() => expect(stepStates()).toEqual({ enable: 'current' }));
    expect(screen.getByTestId('computer-use-preview-badge').textContent)
      .toBe('settings.computerUse.windows.previewBadge');
    expect(screen.getByText('settings.computerUse.windows.subtitle')).toBeTruthy();
    expect(screen.getByText('settings.computerUse.windows.enableDetail')).toBeTruthy();
    for (const note of ['apps', 'elevated', 'stop']) {
      expect(screen.getByTestId(`computer-use-windows-${note}`).textContent)
        .toBe(`settings.computerUse.windows.${note}`);
    }
    expect(screen.queryByText(/steps\.(accessibility|screenRecording)/)).toBeNull();
    expect(screen.queryByText('settings.computerUse.steps.enable.detail')).toBeNull();

    await user.click(screen.getByTestId('computer-use-action-enable'));
    expect(invoke).toHaveBeenLastCalledWith('computer_use_enable');
    expect(invoke.mock.calls.some(([command]) => command === 'computer_use_open_permission')).toBe(false);
  });

  it('describes a Windows runtime to a browser on a Mac, read-only', async () => {
    browserOn('mac');
    statusSequence({ ...windowsHost, status: 'unavailable', reason: 'session_locked' });

    render(<SettingsComputerUsePage />);

    await waitFor(() => expect(stepStates()).toEqual({ enable: 'pending' }));
    expect(screen.getByTestId('computer-use-preview-badge')).toBeTruthy();
    expect(screen.getByTestId('computer-use-remote-notice').textContent)
      .toBe('settings.computerUse.windows.remoteNotice');
    expect(screen.getByText('workbench.home.computerUse.sessionLockedDetail')).toBeTruthy();
    expect(screen.queryAllByTestId(/computer-use-action-/)).toHaveLength(0);
  });

  it('describes a macOS runtime to a browser on Windows with the macOS guide', async () => {
    browserOn('windows');
    statusSequence({ supported: true, host_platform: 'macos', status: 'needs_permission', reason: 'accessibility' });

    render(<SettingsComputerUsePage />);

    await waitFor(() => expect(stepStates().accessibility).toBe('current'));
    expect(Object.keys(stepStates())).toEqual([...COMPUTER_USE_GUIDE_STEPS]);
    expect(screen.queryByTestId('computer-use-preview-badge')).toBeNull();
    expect(screen.getByTestId('computer-use-remote-notice').textContent)
      .toBe('settings.computerUse.remoteNotice');
  });

  it('keeps the macOS guide for an older server without host_platform, even in a Windows shell', async () => {
    browserOn('windows');
    enterActionableShell();
    statusSequence({ supported: true, status: 'needs_permission', reason: 'screen_recording' });

    render(<SettingsComputerUsePage />);

    await waitFor(() => expect(stepStates().screenRecording).toBe('current'));
    expect(screen.queryByTestId('computer-use-preview-badge')).toBeNull();
    expect(screen.getByTestId('computer-use-action-screenRecording')).toBeTruthy();
  });

  it('marks the switch done once the host reports any state past off', async () => {
    enterActionableShell();
    statusSequence({ ...windowsHost, status: 'needs_permission', reason: 'accessibility' });

    render(<SettingsComputerUsePage />);

    await waitFor(() => expect(stepStates()).toEqual({ enable: 'complete' }));
    expect(screen.queryAllByTestId(/computer-use-action-/)).toHaveLength(0);
    expect(screen.getByText('workbench.home.computerUse.errorDetail')).toBeTruthy();
  });

  it('reports a failed switch with Windows recovery, not the macOS menu bar', async () => {
    enterActionableShell(vi.fn(async () => { throw new Error('denied'); }));
    statusSequence({ ...windowsHost, status: 'off', reason: 'never_enabled' });
    const user = userEvent.setup();

    render(<SettingsComputerUsePage />);
    await user.click(await screen.findByTestId('computer-use-action-enable'));

    expect((await screen.findByTestId('computer-use-action-failed')).textContent)
      .toBe('settings.computerUse.windows.actionFailed');
  });

  it('keeps the unsupported notice for an unsupported Windows host, in Windows terms and in Preview', async () => {
    enterActionableShell();
    statusSequence({ supported: false, host_platform: 'windows', status: 'unavailable', reason: 'unsupported_host' });

    render(<SettingsComputerUsePage />);

    const notice = await screen.findByTestId('computer-use-unsupported');
    expect(notice.textContent).toContain('settings.computerUse.windows.unsupportedDetail');
    expect(notice.textContent).not.toContain('settings.computerUse.unsupportedDetail');
    expect(screen.getByTestId('computer-use-preview-badge')).toBeTruthy();
    expect(screen.queryByTestId('computer-use-steps')).toBeNull();
  });

  it('ships the Windows copy in both locales without macOS permission guidance', () => {
    expect(en.settings.computerUse.windows.previewBadge).toBe('Preview');
    expect(zh.settings.computerUse.windows.previewBadge).toBe('预览');
    for (const locale of [en, zh]) {
      const copy = locale.settings.computerUse.windows;
      for (const text of [...Object.values(copy), ...Object.values(locale.workbench.home.setup.computer_use_windows)]) {
        expect(text).toBeTruthy();
        expect(text).not.toMatch(/\bMac\b|System Settings|系统设置|菜单栏|menu bar/);
      }
    }
    expect(en.workbench.home.setup.computer_use_windows.title).toContain('Preview');
    expect(zh.workbench.home.setup.computer_use_windows.title).toContain('预览');
    expect(en.settings.computerUse.windows.elevated).toContain('administrator');
    expect(zh.settings.computerUse.windows.elevated).toContain('管理员');
  });
});
