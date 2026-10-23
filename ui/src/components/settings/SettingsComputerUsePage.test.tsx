/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import {
  COMPUTER_USE_GUIDE_STEPS,
  computerUseGuideProgress,
} from '../workbench/computerUseStatus';
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
  screen.getAllByRole('listitem').map((item) => [
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

  it('gives each current step its native action', async () => {
    const invoke = enterActionableShell();
    statusSequence({ supported: true, status: 'off', reason: 'never_enabled' });
    const user = userEvent.setup();

    const { unmount } = render(<SettingsComputerUsePage />);
    await user.click(await screen.findByTestId('computer-use-action-enable'));
    expect(invoke).toHaveBeenLastCalledWith('computer_use_enable');
    // Only the current step acts; later steps wait for the status to reach them.
    expect(screen.queryByTestId('computer-use-action-accessibility')).toBeNull();
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
