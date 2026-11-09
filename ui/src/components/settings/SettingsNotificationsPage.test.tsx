/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BotsContext, type BotsState } from '@/context/BotsContext';
import { NotificationPreferencesContext } from '@/context/NotificationPreferencesContext';
import type { NotificationPreferences } from '@/lib/notificationPreferences';
import { SettingsNotificationsPage } from './SettingsNotificationsPage';

const authorization = vi.hoisted(() => ({
  capabilities: { can_read_instance: false },
}));
const native = vi.hoisted(() => ({ shell: false }));

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => authorization,
}));
vi.mock('@/lib/native', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/native')>()),
  isNativeShell: () => native.shell,
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
  native.shell = false;
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

  it('shows the native push control when the instance can be read', () => {
    authorization.capabilities.can_read_instance = true;
    native.shell = true;
    mount({ enabled: true, bots: true, sessions: true }, false);
    expect(screen.getByTestId('web-push-control')).toBeTruthy();
    expect(screen.getByText('settings.notifications.deviceTitle')).toBeTruthy();
  });
});
