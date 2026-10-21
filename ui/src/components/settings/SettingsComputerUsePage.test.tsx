/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
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

const stepStates = () => Object.fromEntries(
  screen.getAllByRole('listitem').map((item) => [
    item.getAttribute('data-step'),
    item.getAttribute('data-step-state'),
  ]),
);

afterEach(() => {
  cleanup();
  apiFetch.mockReset();
  Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
});

describe('computerUseGuideProgress', () => {
  const progress = (status: string, reason: string | null = null) =>
    COMPUTER_USE_GUIDE_STEPS.map((step) => computerUseGuideProgress({ status, reason })[step]);

  it('advances the steps only as far as the status proves', () => {
    expect(progress('off', 'never_enabled')).toEqual(['current', 'pending', 'pending', 'pending']);
    expect(progress('needs_permission', 'accessibility'))
      .toEqual(['complete', 'current', 'pending', 'pending']);
    // The driver checks Accessibility first, so a Screen Recording gap proves it passed.
    expect(progress('needs_permission', 'screen_recording'))
      .toEqual(['complete', 'complete', 'current', 'pending']);
    expect(progress('needs_permission', null)).toEqual(['complete', 'current', 'pending', 'pending']);
    expect(progress('ready')).toEqual(['complete', 'complete', 'complete', 'complete']);
  });

  it('claims no permission progress for states that happen before or beside the permission checks', () => {
    for (const status of ['starting', 'needs_runtime', 'error']) {
      expect(progress(status, 'any')).toEqual(['complete', 'pending', 'pending', 'pending']);
    }
    expect(progress('unavailable', 'shell_not_running')).toEqual(['pending', 'pending', 'pending', 'pending']);
    // An unknown state reads as off, matching the status copy's fallback.
    expect(progress('future_state')).toEqual(['current', 'pending', 'pending', 'pending']);
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
      recheck: 'pending',
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

    await waitFor(() => expect(stepStates().recheck).toBe('complete'));
    expect(apiFetch.mock.calls.length).toBeGreaterThan(callsBefore);
    expect(Object.values(stepStates())).toEqual(['complete', 'complete', 'complete', 'complete']);
  });

  it('shows only the desktop-app notice without the shell, and never reads the status', () => {
    render(<SettingsComputerUsePage />);

    expect(screen.getByTestId('computer-use-unsupported')).toBeTruthy();
    expect(screen.getByText('settings.computerUse.unsupportedTitle')).toBeTruthy();
    expect(screen.queryByTestId('computer-use-steps')).toBeNull();
    expect(screen.queryByRole('button', { name: /settings\.computerUse\.recheck/ })).toBeNull();
    expect(apiFetch).not.toHaveBeenCalled();
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
  });
});
