/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BotsContext, type BotsState } from '@/context/BotsContext';
import { NotificationPreferencesContext } from '@/context/NotificationPreferencesContext';
import type { NotificationPreferences } from '@/lib/notificationPreferences';
import { SettingsNotificationsPage } from './SettingsNotificationsPage';

const authorization = vi.hoisted(() => ({
  capabilities: { can_read_instance: false },
}));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));
vi.mock('../workbench/WebPushControl', () => ({
  WebPushControl: () => <div data-testid="web-push-control" />,
}));

function mount(preferences: NotificationPreferences, hasBots: boolean) {
  const update = vi.fn(async () => true);
  render(
    <BotsContext.Provider value={{ enabled: hasBots } as BotsState}>
      <NotificationPreferencesContext.Provider value={{ preferences, loaded: true, update }}>
        <SettingsNotificationsPage />
      </NotificationPreferencesContext.Provider>
    </BotsContext.Provider>,
  );
  return update;
}

const switches = () => screen.getAllByRole('switch').map((node) => ({
  label: node.getAttribute('aria-label'),
  checked: node.getAttribute('aria-checked'),
  disabled: (node as HTMLButtonElement).disabled,
}));

afterEach(() => {
  cleanup();
  authorization.capabilities.can_read_instance = false;
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __AVIBE_DESKTOP_NOTIFICATIONS__?: true }).__AVIBE_DESKTOP_NOTIFICATIONS__;
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

describe('Settings › Notifications', () => {
  it('saves each switch as a patch of the account record', () => {
    const update = mount({ enabled: true, bots: true, sessions: true }, true);

    expect(switches().map(({ label }) => label)).toEqual([
      'workbench.notifications.title',
      'workbench.notifications.bots',
      'workbench.notifications.sessions',
    ]);
    fireEvent.click(screen.getByRole('switch', { name: 'workbench.notifications.sessions' }));
    expect(update).toHaveBeenCalledWith({ sessions: false });
  });

  it('holds the categories while the master switch is off', () => {
    mount({ enabled: false, bots: true, sessions: false }, true);

    expect(switches()).toEqual([
      { label: 'workbench.notifications.title', checked: 'false', disabled: false },
      { label: 'workbench.notifications.bots', checked: 'true', disabled: true },
      { label: 'workbench.notifications.sessions', checked: 'false', disabled: true },
    ]);
  });

  it('offers only the master switch to a member without Bots', () => {
    mount({ enabled: true, bots: true, sessions: true }, false);

    expect(switches().map(({ label }) => label)).toEqual(['workbench.notifications.title']);
  });

  it('shows Web Push in a browser when the instance can be read', () => {
    authorization.capabilities.can_read_instance = true;
    mount({ enabled: true, bots: true, sessions: true }, false);
    expect(screen.getByTestId('web-push-control')).toBeTruthy();
    expect(screen.getByText('settings.notifications.deviceTitle')).toBeTruthy();
    expect(screen.queryByText('settings.notifications.desktopTitle')).toBeNull();
  });

  it('shows the menu-bar fallback in an older desktop shell', () => {
    authorization.capabilities.can_read_instance = true;
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    mount({ enabled: true, bots: true, sessions: true }, false);

    expect(screen.queryByTestId('web-push-control')).toBeNull();
    expect(screen.getByText('settings.notifications.desktopTitle')).toBeTruthy();
    expect(screen.getByText('settings.notifications.desktopHintMenuBar')).toBeTruthy();
    expect(screen.queryByRole('switch', { name: 'settings.notifications.desktopTitle' })).toBeNull();
  });

  it('loads and saves the native desktop switch when the shell exposes its commands', async () => {
    authorization.capabilities.can_read_instance = true;
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_NOTIFICATIONS__', { configurable: true, value: true });
    const invoke = vi.fn(async (command: string) => (
      command === 'desktop_notifications_state' ? false : true
    ));
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });
    mount({ enabled: true, bots: true, sessions: true }, false);

    const desktopSwitch = await waitFor(() => {
      const element = screen.getByRole('switch', { name: 'settings.notifications.desktopTitle' });
      expect(element.getAttribute('aria-checked')).toBe('false');
      return element;
    });
    expect(screen.queryByTestId('web-push-control')).toBeNull();

    fireEvent.click(desktopSwitch);
    await waitFor(() => expect(desktopSwitch.getAttribute('aria-checked')).toBe('true'));
    expect(invoke).toHaveBeenCalledWith('desktop_notifications_set_enabled', { enabled: true });
    expect(screen.getByText('settings.notifications.desktopHint')).toBeTruthy();
  });

  it('keeps the native switch unchanged and shows the save error when IPC rejects', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_NOTIFICATIONS__', { configurable: true, value: true });
    const invoke = vi.fn(async (command: string) => {
      if (command === 'desktop_notifications_state') return true;
      throw new Error('write failed');
    });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });
    mount({ enabled: true, bots: true, sessions: true }, false);

    const desktopSwitch = await waitFor(() => screen.getByRole('switch', {
      name: 'settings.notifications.desktopTitle',
    }));
    expect(desktopSwitch.getAttribute('aria-checked')).toBe('true');
    fireEvent.click(desktopSwitch);
    await waitFor(() => expect(screen.getByText('settings.notifications.desktopSaveFailed')).toBeTruthy());
    expect(desktopSwitch.getAttribute('aria-checked')).toBe('true');
  });
});
