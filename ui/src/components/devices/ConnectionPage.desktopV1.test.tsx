/** @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// 「连接方式」 in a window of an older Avibe Desktop, still in the field: its bridge says
// `directPreference` 1, offers 「自动」 and 「仅 Cloudflare 隧道」 only, and sends no event when its own
// menu changes the choice. The page reads the choice again when it is shown again and on its own
// refresh (every 30 s). The real bridge script is version 2 now (ConnectionPage.desktop.test.tsx),
// so this one is a stub of the older shell.

let preference: { mode: 'automatic' | 'tunnel'; reverted: null };

function olderShell() {
  preference = { mode: 'automatic', reverted: null };
  const call = vi.fn(async (method: string, params?: { mode?: 'automatic' | 'tunnel' }) => {
    if (method === 'direct.status') return { preferred: 'direct', sequence: 7, closure: 0, path: 'ipv4', path_scope: 'local' };
    if (method === 'direct.preference') return { ...preference };
    if (method === 'direct.setPreference' && (params?.mode === 'automatic' || params?.mode === 'tunnel')) {
      preference = { mode: params.mode, reverted: null };
      return { ...preference };
    }
    throw { code: 'invalid_argument' };
  });
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: Object.freeze({
      contract: 1, platform: 'desktop', shellVersion: '3.2.7',
      capabilities: Object.freeze({ direct: 3, directPreference: 1 }),
      call, on: () => () => undefined,
    }),
  });
  return call;
}

const host = 'max-app.avibe.bot';

/** The page and everything it reads, loaded after the shell so that they share one `native.ts`. */
async function mount() {
  vi.resetModules();
  const [{ ConnectionPage }, { devicesApi }, { InstanceAuthorizationContext }, { DENIED_INSTANCE_CAPABILITIES }, { MemoryRouter, Route, Routes }, { default: i18n }, { setLiveStreamVia }] = await Promise.all([
    import('./ConnectionPage'), import('./api'), import('@/context/InstanceAuthorizationContext'), import('@/lib/sessionInfo'),
    import('react-router-dom'), import('../../i18n'), import('@/lib/liveStreamVia'),
  ]);
  await i18n.changeLanguage('zh');
  setLiveStreamVia('direct');
  vi.spyOn(devicesApi, 'interfaces').mockResolvedValue([]);
  vi.spyOn(devicesApi, 'connection').mockResolvedValue({
    settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' },
    interfaces: [], direct: 'ready',
  });
  vi.spyOn(devicesApi, 'status').mockResolvedValue({ ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}` } as never);
  vi.spyOn(devicesApi, 'address').mockResolvedValue({ hostname: host, eligible: false, retired: [], healthConnected: false });
  vi.spyOn(devicesApi, 'rebind').mockRejectedValue(new Error('none'));
  vi.spyOn(devicesApi, 'devices').mockResolvedValue({ devices: [], unlinkedHealth: [], signedOut: [], bulk: null });
  const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
  render(
    <InstanceAuthorizationContext.Provider value={{ remote: true, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
      <MemoryRouter initialEntries={['/settings/remote-access/connection']}>
        <Routes><Route path="/settings/remote-access/connection" element={<ConnectionPage />} /></Routes>
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  Reflect.deleteProperty(window, 'avibeNative');
});

const chosen = (group: HTMLElement, name: RegExp) => within(group).getByRole('radio', { name }).getAttribute('aria-checked');

describe('ConnectionPage · in a window of an older Avibe Desktop (directPreference 1)', () => {
  it('offers 「自动」 and 「仅 Cloudflare 隧道」 only, and says only that the choice is this device\'s', async () => {
    olderShell();
    await mount();
    const group = await screen.findByRole('radiogroup', { name: '这台设备' });
    await waitFor(() => expect(chosen(group, /^自动/)).toBe('true'));
    expect(within(group).getAllByRole('radio')).toHaveLength(2);
    expect(document.body.textContent).not.toContain('仅直连和中继');
    expect(group.parentElement!.textContent).toContain('这个选择只管这台设备。');
    expect(document.body.textContent).not.toContain('会自己改回');
  });

  it('sees a choice the app\'s own menu made, with no event, once the page is shown again', async () => {
    const call = olderShell();
    await mount();
    const group = await screen.findByRole('radiogroup', { name: '这台设备' });
    await waitFor(() => expect(chosen(group, /^自动/)).toBe('true'));

    // The app's menu: the shell keeps the new choice and tells no one.
    preference = { mode: 'tunnel', reverted: null };
    expect(chosen(group, /^仅 Cloudflare/)).toBe('false');

    // The window lost focus to the menu, then has it back.
    const focus = vi.spyOn(document, 'hasFocus').mockReturnValue(false);
    window.dispatchEvent(new Event('blur'));
    focus.mockReturnValue(true);
    window.dispatchEvent(new Event('focus'));
    await waitFor(() => expect(chosen(group, /^仅 Cloudflare/)).toBe('true'));
    expect(call).toHaveBeenCalledWith('direct.preference');
  });

  it('sees it on the page\'s own refresh too, where the window never lost focus', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    olderShell();
    await mount();
    const group = await screen.findByRole('radiogroup', { name: '这台设备' });
    await waitFor(() => expect(chosen(group, /^自动/)).toBe('true'));

    preference = { mode: 'tunnel', reverted: null };
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
    await waitFor(() => expect(chosen(group, /^仅 Cloudflare/)).toBe('true'));
  });
});
