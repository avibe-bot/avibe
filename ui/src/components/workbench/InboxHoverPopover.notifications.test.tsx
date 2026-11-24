/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { Popover } from '../ui/popover';
import { InboxHoverPopover } from './InboxHoverPopover';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => ({ capabilities: { can_read_instance: true } }) }));
vi.mock('../../context/BotsContext', () => ({ useBots: () => ({ enabled: true, bots: [], displayName: () => '', botForSession: () => undefined }) }));
vi.mock('../../context/NotificationPreferencesContext', () => ({ useNotificationPreferences: () => ({ preferences: { enabled: true, bots: true, sessions: true }, update: vi.fn() }) }));
vi.mock('./WebPushControl', () => ({ WebPushControl: () => <div data-testid="web-push" /> }));

function Location() { return <output data-testid="location">{useLocation().pathname}</output>; }
function mount(invoke: (command: string, args?: { enabled: boolean }) => Promise<unknown>) {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
  Object.defineProperty(window, '__AVIBE_DESKTOP_NOTIFICATIONS__', { configurable: true, value: true });
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });
  render(<MemoryRouter initialEntries={['/chat/example']}><Location /><Popover open>
    <InboxHoverPopover sessions={[]} unreadBySession={{}} unreadSessions={0} totalUnread={0} canMarkRead
      onItemClick={() => {}} onMarkAllRead={() => {}} onMouseEnter={() => {}} onMouseLeave={() => {}} />
  </Popover></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: 'workbench.notifications.open' }));
  expect(screen.getByRole('button', { name: 'workbench.notifications.back' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'workbench.notifications.moreSettings' })).toBeTruthy();
  expect(screen.getAllByRole('switch')).toHaveLength(3);
}
afterEach(() => {
  cleanup();
  for (const key of ['__AVIBE_DESKTOP_SHELL__', '__AVIBE_DESKTOP_NOTIFICATIONS__', '__TAURI_INTERNALS__']) Reflect.deleteProperty(window, key);
});

describe('the sidebar Inbox popover notification view (Back to Inbox)', () => {
  it('enables the existing preference in place only after native confirmation and follows tray changes', async () => {
    let enabled = false;
    let complete: (() => void) | undefined;
    const invoke = vi.fn(async (command: string, args?: { enabled: boolean }) => {
      if (command === 'desktop_notifications_authorization') return 'authorized';
      if (command === 'desktop_notifications_set_enabled') {
        await new Promise<void>(resolve => { complete = resolve; });
        enabled = args!.enabled;
      }
      return enabled;
    });
    mount(invoke);
    fireEvent.click(await screen.findByRole('button', { name: 'workbench.inbox.notifications.enable' }));
    expect(invoke).toHaveBeenCalledWith('desktop_notifications_set_enabled', { enabled: true });
    expect(screen.queryByText('workbench.inbox.notifications.enabled')).toBeNull();
    complete!();
    await screen.findByText('workbench.inbox.notifications.enabled');
    expect(screen.getByTestId('location').textContent).toBe('/chat/example');
    enabled = false;
    fireEvent(window, new CustomEvent('avibe:desktop-notifications', { detail: { enabled: false } }));
    await screen.findByRole('button', { name: 'workbench.inbox.notifications.enable' });
    enabled = true;
    fireEvent.focus(window);
    await screen.findByText('workbench.inbox.notifications.enabled');
    expect(invoke.mock.calls.every(([command]) => ['desktop_notifications_state', 'desktop_notifications_set_enabled', 'desktop_notifications_authorization'].includes(command))).toBe(true);
  });

  it('keeps failed writes visibly off and does not navigate to Settings', async () => {
    mount(async command => {
      if (command === 'desktop_notifications_set_enabled') throw new Error('preference write failed');
      return command === 'desktop_notifications_authorization' ? 'authorized' : false;
    });
    fireEvent.click(await screen.findByRole('button', { name: 'workbench.inbox.notifications.enable' }));
    await screen.findByRole('alert');
    expect(screen.queryByText('workbench.inbox.notifications.enabled')).toBeNull();
    expect(screen.getByTestId('location').textContent).toBe('/chat/example');
  });

  it('offers no native action to an ACL-rejected remote page or retired document', async () => {
    const invoke = vi.fn(async () => { throw new Error('not allowed'); });
    mount(invoke);
    await screen.findByText('settings.notifications.desktopHintMenuBar');
    expect(screen.queryByRole('button', { name: 'workbench.inbox.notifications.enable' })).toBeNull();
    expect(invoke).not.toHaveBeenCalledWith('desktop_notifications_set_enabled', expect.anything());
  });

  it('does not label a system-denied preference as working notifications', async () => {
    mount(async command => command === 'desktop_notifications_authorization' ? 'denied' : true);
    await waitFor(() => expect(screen.getByText('settings.notifications.desktopDenied')).toBeTruthy());
    expect(screen.queryByText('workbench.inbox.notifications.enabled')).toBeNull();
  });
});
