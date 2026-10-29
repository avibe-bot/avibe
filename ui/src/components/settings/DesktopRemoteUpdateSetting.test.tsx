// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { DesktopRemoteUpdateSetting } from './DesktopRemoteUpdateSetting';

function shell(invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown>) {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
  Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
}
function show() {
  return render(<I18nextProvider i18n={i18n}><DesktopRemoteUpdateSetting /></I18nextProvider>);
}
afterEach(() => {
  cleanup();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

// No prior General setting wrote through Tauri IPC. These exercise real command
// wrappers to catch false capability detection and a write before risk consent.
describe('Desktop remote install setting', () => {
  it('is absent on Web even with a generic invoke bridge', async () => {
    const invoke = vi.fn();
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    show();
    expect(screen.queryByRole('switch')).toBeNull();
    expect(invoke).not.toHaveBeenCalled();
  });

  it.each(['rejected', 'undefined'] as const)('hides on an old shell with a %s probe', async (kind) => {
    const invoke = vi.fn(async () => { if (kind === 'rejected') throw new Error('unknown command'); });
    shell(invoke);
    await act(async () => { show(); });
    expect(invoke).toHaveBeenCalledWith('desktop_remote_install_allowed');
    expect(screen.queryByRole('switch')).toBeNull();
  });

  it.each([true, false])('reads the shell value %s without writing', async (enabled) => {
    const invoke = vi.fn(async () => enabled);
    shell(invoke); show();
    const control = await screen.findByRole('switch');
    expect(control.getAttribute('aria-checked')).toBe(String(enabled));
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it('explains risks before enabling, can cancel, and persists only after confirmation', async () => {
    const invoke = vi.fn(async (command: string, args?: Record<string, unknown>) => command.endsWith('_set') ? args?.enabled : false);
    shell(invoke); show();
    const control = await screen.findByRole('switch');
    fireEvent.click(control);
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(screen.getByText(i18n.t('settings.general.remoteInstallWarning'))).toBeTruthy();
    expect(invoke).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: i18n.t('common.cancel') }));
    expect(invoke).toHaveBeenCalledTimes(1);
    fireEvent.click(control);
    fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.general.remoteInstallEnable') }));
    await vi.waitFor(() => expect(control.getAttribute('aria-checked')).toBe('true'));
    expect(invoke).toHaveBeenCalledWith('desktop_remote_install_set', { enabled: true });
    fireEvent.click(control);
    await vi.waitFor(() => expect(control.getAttribute('aria-checked')).toBe('false'));
    expect(invoke).toHaveBeenCalledWith('desktop_remote_install_set', { enabled: false });
  });

  it('preserves the shell value and displays a rejected save', async () => {
    const invoke = vi.fn(async (command: string) => { if (command.endsWith('_set')) throw new Error('not allowed'); return false; });
    shell(invoke); show();
    const control = await screen.findByRole('switch');
    fireEvent.click(control);
    fireEvent.click(screen.getByRole('button', { name: i18n.t('settings.general.remoteInstallEnable') }));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(control.getAttribute('aria-checked')).toBe('false');
  });
});
