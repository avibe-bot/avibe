/** @vitest-environment jsdom */

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';

// 「连接方式」 in a window of Avibe Desktop that shows another computer's instance. The bridge is
// the script the shell really injects (desktop/src-tauri/src/direct/page_bridge.js), as in
// src/lib/native.direct.desktop.test.ts; what the shell has already said about this window's path
// is given to it before it runs, as the shell does.
const script = new Function(`return (${readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../../../desktop/src-tauri/src/direct/page_bridge.js'),
  'utf8',
)})`)() as (token: string, version: string, asksBeforeLeaving: boolean) => void;

/** Runs the script as a new document would (its two properties are made removable for the test). */
const install = (early?: unknown) => {
  if (early) (window as unknown as Record<string, unknown>).__AVIBE_DIRECT_EARLY__ = early;
  const page = window;
  const defineProperty = Object.defineProperty;
  Object.defineProperty = ((target: object, key: PropertyKey, descriptor: PropertyDescriptor) =>
    defineProperty(target, key, target === page ? { ...descriptor, configurable: true } : descriptor)
  ) as typeof Object.defineProperty;
  try {
    script('window-token', '3.2.7', false);
  } finally {
    Object.defineProperty = defineProperty;
  }
};

const said = (path: 'ipv4' | 'ipv6' | 'relay' | null, scope?: 'local' | 'global') => ({
  status: { preferred: path ? 'direct' : 'tunnel', sequence: 7, closure: 0, path, ...(scope ? { path_scope: scope } : {}) },
  preference: { mode: 'automatic', pendingRelaunch: false, reverted: null },
});

const host = 'max-app.avibe.bot';

/** The page and everything it reads, loaded after the bridge so that they share one `native.ts`. */
async function mount(via: 'direct' | 'other') {
  vi.resetModules();
  const [{ ConnectionPage }, { devicesApi }, { InstanceAuthorizationContext }, { DENIED_INSTANCE_CAPABILITIES }, { MemoryRouter, Route, Routes }, { default: i18n }, { setLiveStreamVia }, native] = await Promise.all([
    import('./ConnectionPage'), import('./api'), import('@/context/InstanceAuthorizationContext'), import('@/lib/sessionInfo'),
    import('react-router-dom'), import('../../i18n'), import('@/lib/liveStreamVia'), import('@/lib/native'),
  ]);
  await i18n.changeLanguage('zh');
  setLiveStreamVia(via);
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
  return native;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  for (const name of ['avibeNative', '__AVIBE_DIRECT_DELIVER__', '__AVIBE_DIRECT_EARLY__']) Reflect.deleteProperty(window, name);
});

describe('ConnectionPage · in an Avibe Desktop window of another computer\'s instance', () => {
  it('says the path this device uses, as the phone App does, and offers its choice through the window\'s bridge', async () => {
    install(said('ipv4', 'local'));
    const native = await mount('direct');
    // The desktop bridge is not the phone App's: only its direct path is asked of it.
    expect(native.isNativeShell()).toBe(false);
    expect(native.hasNativeCapability('direct')).toBe(false);
    expect(native.nativeDirectAvailable()).toBe(true);

    expect(await screen.findByText('这台设备')).toBeTruthy();
    // The desktop core measures no round trip: the path and its family, nothing else.
    await waitFor(() => expect(screen.getByText('本设备正在使用：局域网直连 · IPv4')).toBeTruthy());
    // The choice is the window's own, read through its bridge. The shell is at `directPreference`
    // 2 and the instance has direct on: 「仅直连和中继」 is offered, and the hint says that the
    // desktop app sets it back itself.
    const group = await screen.findByRole('radiogroup', { name: '这台设备' });
    expect(Array.from(group.querySelectorAll('[role="radio"]')).map((each) => each.textContent?.split(/[哪不建]/)[0])).toEqual(['自动', '仅 Cloudflare 隧道', '仅直连和中继']);
    expect(within(group).getByRole('radio', { name: /^自动/ }).getAttribute('aria-checked')).toBe('true');
    expect(within(group).getByRole('radio', { name: /^仅直连和中继/ }).getAttribute('aria-checked')).toBe('false');
    expect(group.parentElement!.textContent).toContain('这个选择只管这台设备。');
    expect(group.parentElement!.textContent).toContain('选「仅直连和中继」后连不上，Avibe Desktop 会自己改回「自动」。');
    // Nothing waits for the app to be opened again here.
    expect(document.body.textContent).not.toContain('重新打开 App 后生效');
    // Not a browser: the page does not say what browsers do.
    expect(document.body.textContent).not.toContain('浏览器只走 Cloudflare 隧道');
  });

  it('hears a choice the app\'s own menu changed while the page is open, as the shell tells it (version 2)', async () => {
    install(said('ipv4', 'local'));
    await mount('direct');
    const group = await screen.findByRole('radiogroup', { name: '这台设备' });
    await waitFor(() => expect(within(group).getByRole('radio', { name: /^自动/ }).getAttribute('aria-checked')).toBe('true'));

    // The shell's menu: a new choice delivered to the bridge, which tells the page by the window
    // event. The window does not lose or regain focus, and no time passes.
    (window as unknown as { __AVIBE_DIRECT_DELIVER__: (message: unknown) => void }).__AVIBE_DIRECT_DELIVER__({ preference: { mode: 'tunnel', pendingRelaunch: false, reverted: null }, preferenceOrder: 9 });
    await waitFor(() => expect(within(group).getByRole('radio', { name: /^仅 Cloudflare/ }).getAttribute('aria-checked')).toBe('true'));

    // The app sets 「仅直连和中继」 back itself: the page hears that too, and says so.
    (window as unknown as { __AVIBE_DIRECT_DELIVER__: (message: unknown) => void }).__AVIBE_DIRECT_DELIVER__({
      preference: { mode: 'automatic', pendingRelaunch: false, reverted: { at: 1795000000000, reason: 'unreachable', from: 'direct' } },
      preferenceOrder: 10,
    });
    await waitFor(() => expect(within(group).getByRole('radio', { name: /^自动/ }).getAttribute('aria-checked')).toBe('true'));
  });

  it('never calls a relayed path direct: on the relay the 「直连」 row is not marked in use', async () => {
    install(said('relay'));
    // The stream came in by the direct ingress, as a relayed one does.
    await mount('direct');
    await waitFor(() => expect(screen.getByText('本设备正在使用：中继')).toBeTruthy());
    const direct = screen.getByRole('switch', { name: '直连' }).closest('div')!.parentElement!;
    expect(direct.textContent).not.toContain('使用中');
    // This instance reports no relay row, so nothing carries the mark.
    expect(document.body.textContent).not.toContain('使用中');
  });

  it('says the tunnel where the window has no route of its own', async () => {
    install(said(null));
    await mount('other');
    expect(await screen.findByText('这台设备')).toBeTruthy();
    await waitFor(() => expect(screen.getByText('本设备正在使用：Cloudflare 隧道')).toBeTruthy());
  });

  it('is as in a browser where the shell gives the page no bridge', async () => {
    // This computer's own instance, a custom domain, macOS before 14: nothing is injected.
    const native = await mount('other');
    expect(native.nativeDirectAvailable()).toBe(false);
    expect(await screen.findByText(/浏览器只走 Cloudflare 隧道/)).toBeTruthy();
    expect(screen.queryByText('这台设备')).toBeNull();
    expect(document.body.textContent).not.toContain('本设备正在使用');
  });
});
