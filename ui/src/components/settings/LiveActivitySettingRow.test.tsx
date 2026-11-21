/** @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { BotsContext, type BotsState } from '@/context/BotsContext';
import { NotificationPreferencesContext } from '@/context/NotificationPreferencesContext';
import { SettingsNotificationsPage } from './SettingsNotificationsPage';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => ({ capabilities: { can_read_instance: false } }) }));
function mount(capabilities: Record<string, number> = {}, systemEnabled = true) {
  let setting = { enabled: false, systemEnabled };
  const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'liveActivity.setSetting') setting = { ...setting, enabled: params?.enabled === true };
    return method === 'liveActivity.openSettings' ? { ok: true } : setting;
  });
  vi.stubGlobal('avibeNative', { contract: 1, platform: 'ios', shellVersion: 'test', capabilities, call, on: () => () => {} });
  const update = vi.fn(async () => true);
  render(<BotsContext.Provider value={{ enabled: false } as BotsState}>
    <NotificationPreferencesContext.Provider value={{ loaded: true, preferences: { enabled: false, bots: true, sessions: true }, update }}>
      <SettingsNotificationsPage />
    </NotificationPreferencesContext.Provider>
  </BotsContext.Provider>);
  return { call, update };
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it.each([{}, { liveActivities: 1 }, { liveActivities: 2 }, { lockScreenProgress: 0 }])('hides the device setting for capabilities %j', (capabilities) => {
  const { call } = mount(capabilities);
  expect(screen.queryByRole('switch', { name: 'liveActivity.title' })).toBeNull();
  expect(call).not.toHaveBeenCalled();
});
it.each([1, 2])('reads and saves only on the device for capability %s, independently of account notifications', async (version) => {
  const { call, update } = mount({ lockScreenProgress: version });
  const toggle = screen.getByRole('switch', { name: 'liveActivity.title' }) as HTMLButtonElement;
  await waitFor(() => expect(toggle.disabled).toBe(false));
  expect(toggle.getAttribute('aria-checked')).toBe('false');
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle.getAttribute('aria-checked')).toBe('true'));
  expect(call).toHaveBeenCalledWith('liveActivity.setting', {});
  expect(call).toHaveBeenCalledWith('liveActivity.setSetting', { enabled: true });
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle.getAttribute('aria-checked')).toBe('false'));
  expect(call).toHaveBeenCalledWith('liveActivity.setSetting', { enabled: false });
  expect(update).not.toHaveBeenCalled();
});
it('disables the switch when the system disallows activities and opens Settings', async () => {
  const { call } = mount({ lockScreenProgress: 1 }, false);
  await screen.findByText('liveActivity.systemOff');
  const toggle = screen.getByRole('switch', { name: 'liveActivity.title' }) as HTMLButtonElement;
  expect(toggle.disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'liveActivity.settings' }));
  expect(call).toHaveBeenCalledWith('liveActivity.openSettings', {});
  call.mockResolvedValue({ enabled: false, systemEnabled: true });
  vi.spyOn(document, 'hasFocus').mockReturnValue(true);
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  expect(toggle.disabled).toBe(false);
});
it('keeps the authoritative setting when a save fails', async () => {
  const { call } = mount({ lockScreenProgress: 1 });
  const toggle = screen.getByRole('switch', { name: 'liveActivity.title' }) as HTMLButtonElement;
  await waitFor(() => expect(toggle.disabled).toBe(false));
  call.mockRejectedValueOnce(new Error('bridge unavailable'));
  fireEvent.click(toggle);
  await screen.findByRole('alert');
  expect(toggle.getAttribute('aria-checked')).toBe('false');
  expect(toggle.disabled).toBe(false);
});
