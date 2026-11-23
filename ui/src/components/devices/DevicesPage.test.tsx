/** @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import i18n from '../../i18n';
import { DevicesApiError, LostAnswer, devicesApi, type ClaimForm } from './api';
import { awayOnClaim, readClaimMarker, writeClaimMarker } from './claimFlow';
import { DevicesPage } from './DevicesPage';
import { DevicesPageView } from './DevicesPageView';
import { accountResultTexts, functionControl, lockedOutCount } from './rules';
import { said } from './testText';
import type { Device, Reach } from './types';
import type { DevicesSnapshot } from './wire';

// ui/design: settings/devices/*
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));

const host = 'max-app.avibe.bot';
const account = 'max@example.test';
const on: Reach = { state: 'on', host, account, tunnel: 'good', ms: 120 };

const macbook: Device = {
  id: 'host', group: 'mine', kind: 'host', name: 'MacBook', client: { kind: 'runtime' }, self: true,
  functions: [{ kind: 'computerUse', status: 'ready' }, { kind: 'notifications', on: true, canChange: true }],
};
const iphone: Device = {
  id: 'iphone', group: 'mine', kind: 'iphone', name: 'iPhone', account, client: { kind: 'app' }, online: { path: 'punched', ms: 38 },
  functions: [{ kind: 'notifications', on: true, canChange: true }, { kind: 'health', on: true, canChange: true, data: ['steps'] }],
};
const chrome: Device = { id: 'chrome', group: 'mine', kind: 'browser', name: 'Chrome', account, client: { kind: 'browser' }, functions: [] };
const sara: Device = { ...chrome, id: 'sara', group: 'others', name: 'Sara Chrome', account: 'sara@example.test', person: 'sara@example.test', functions: [{ kind: 'notifications', on: true, canChange: false }] };

const snapshot = (devices: Device[], extra: Partial<DevicesSnapshot> = {}): DevicesSnapshot => ({ devices, unlinkedHealth: [], signedOut: [], bulk: null, ...extra });
const paired = { ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}` };
const unpaired = { ok: true, enabled: false, paired: false, running: false };

function Where() {
  const location = useLocation();
  return <div data-testid="where">{`${location.pathname}${location.search}`}</div>;
}

function mount(path = '/settings/remote-access', who: 'owner' | 'member' | 'guest' = 'owner', away?: boolean) {
  const owner = who === 'owner';
  // A Member may operate the instance (`can_manage_instance`) without being its owner.
  const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: who !== 'guest', is_instance_owner: owner };
  return render(
    <InstanceAuthorizationContext.Provider value={{ remote: away ?? !owner, instanceKind: 'personal', instanceRole: owner ? 'owner' : 'member', capabilities }}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/settings/remote-access" element={<><DevicesPage /><Where /></>} />
          <Route path="/settings/remote-access/activity" element={<DevicesPage />} />
          <Route path="/settings/remote-access/connection" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

const api = vi.mocked(devicesApi, { partial: true });
let submitted: { action: string; fields: Record<string, string>; policy: string | undefined } | null;

beforeEach(async () => {
  await i18n.changeLanguage('zh');
  window.sessionStorage.clear();
  submitted = null;
  vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(function submit(this: HTMLFormElement) {
    submitted = {
      action: this.action,
      fields: Object.fromEntries(Array.from(this.querySelectorAll('input')).map((input) => [input.name, input.value])),
      policy: document.querySelector<HTMLMetaElement>('meta[name="referrer"]')?.content,
    };
  });
  vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook]));
  vi.spyOn(devicesApi, 'status').mockResolvedValue(unpaired as never);
  vi.spyOn(devicesApi, 'activity').mockResolvedValue({ items: [], nextCursor: null });
  // The build these pages ship in first has no binding again; a test that needs it says so.
  vi.spyOn(devicesApi, 'rebindOffered').mockResolvedValue(false);
  vi.spyOn(devicesApi, 'address').mockResolvedValue(null);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('DevicesPage · turning remote access on', () => {
  it('has no pairing-key field, and takes the user code and state only from the local API\'s answer', async () => {
    const form: ClaimForm = {
      claimId: 'claim-1', action: 'https://avibe.bot/api/v1/claims/redirect-entry', method: 'POST',
      fields: { user_code: 'FROM-API', redirect_state: 'state-from-api' }, referrerPolicy: 'strict-origin',
    };
    vi.spyOn(devicesApi, 'startClaim').mockResolvedValue(form);
    vi.spyOn(devicesApi, 'claim').mockResolvedValue({ state: 'pending' });
    // Whatever the address bar carries is not a source of either value.
    mount('/settings/remote-access?user_code=EVIL&redirect_state=EVIL&claim_id=EVIL');

    const turnOn = await screen.findByRole('button', { name: '开启远程访问' });
    expect(screen.queryByRole('textbox')).toBeNull();
    // No pairing key, and no word about one, anywhere on the page.
    expect(document.body.textContent).not.toContain('配对密钥');
    expect(document.body.textContent).not.toContain('vibe remote pair');
    fireEvent.click(turnOn);

    await waitFor(() => expect(submitted).not.toBeNull());
    expect(api.startClaim).toHaveBeenCalledWith('zh');
    expect(submitted).toEqual({ action: form.action, fields: form.fields, policy: 'strict-origin' });
    expect(readClaimMarker()).toMatchObject({ kind: 'claim', id: 'claim-1' });
    expect(JSON.stringify(window.sessionStorage)).not.toContain('EVIL');
  });

  it('asks for the claim in a language the instance speaks, whatever region the browser detected', async () => {
    const start = vi.spyOn(devicesApi, 'startClaim').mockRejectedValue(new DevicesApiError(503, 'control_plane_unreachable', {}));
    for (const [detected, sent] of [['zh-CN', 'zh'], ['en-US', 'en'], ['fr', 'en']] as const) {
      await i18n.changeLanguage(detected);
      const { unmount } = mount();
      fireEvent.click(await screen.findByRole('button', { name: /开启远程访问|Turn on remote access/ }));
      await waitFor(() => expect(start).toHaveBeenLastCalledWith(sent));
      unmount();
    }
  });

  it('shows "open the new address" when the status read fails because this browser lost admission', async () => {
    writeClaimMarker({ kind: 'claim', id: 'claim-1', at: Date.now() });
    vi.spyOn(devicesApi, 'claim')
      .mockResolvedValueOnce({ state: 'accepting', hostname: host })
      .mockRejectedValue(new DevicesApiError(503, 'remote_access_host_mismatch', {}));
    mount();

    expect(await screen.findByText('远程访问已开启', {}, { timeout: 4000 })).toBeTruthy();
    // The address is the one the local API reported while it was accepting, nothing else.
    expect(screen.getByRole('link', { name: `https://${host}` }).getAttribute('href')).toBe(`https://${host}`);
    expect(screen.getByText(said('这个页面是从局域网地址打开的，绑定后需要登录，所以不再更新。'))).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('points to avibe.bot when the new address was never read', async () => {
    writeClaimMarker({ kind: 'claim', id: 'claim-1', at: Date.now() });
    vi.spyOn(devicesApi, 'claim').mockRejectedValue(new DevicesApiError(503, 'remote_access_host_mismatch', {}));
    mount();

    expect(await screen.findByText(said('从现在起，请用新地址打开这个 Avibe。登录 avibe.bot 就能看到并打开它。'))).toBeTruthy();
    expect(screen.getByRole('link', { name: '打开 avibe.bot' }).getAttribute('href')).toBe('https://avibe.bot/app');
  });

  it('says a refused return in one sentence and drops the word from the address', async () => {
    mount('/settings/remote-access?claim_error=invalid_return');
    expect(await screen.findByText(said('没有开启：从 avibe.bot 回来的这一步没有通过验证，什么都没有绑定。请再开启一次。'))).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/settings/remote-access'));
  });

  it('follows an open request with a way to cancel it, and says an unknown refusal with its code', async () => {
    writeClaimMarker({ kind: 'claim', id: 'claim-1', at: Date.now() });
    const claim = vi.spyOn(devicesApi, 'claim').mockResolvedValue({ state: 'pending' });
    vi.spyOn(devicesApi, 'cancelClaim').mockResolvedValue();
    mount();

    expect(await screen.findByText(said('正在等待 avibe.bot 上的确认…'))).toBeTruthy();
    claim.mockResolvedValue({ state: 'error', code: 'local_claim_unavailable' });
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => expect(api.cancelClaim).toHaveBeenCalledWith('claim-1'));
    expect(await screen.findByText(said('avibe.bot 没有完成这次请求，什么都没有绑定。（代码：local_claim_unavailable）'), {}, { timeout: 4000 })).toBeTruthy();
    expect(readClaimMarker()).toBeNull();
  });

  it('hands a bind-again that comes back here to the connection page, that once and not on any later visit', async () => {
    writeClaimMarker({ kind: 'rebind', id: 'rebind-1', at: Date.now(), away: true });
    const first = mount();
    expect(await screen.findByText('/settings/remote-access/connection')).toBeTruthy();
    first.unmount();

    // The attempt is still kept (only its starter may confirm binding as a new computer), and the
    // same tab can open Devices again like any other page.
    expect(readClaimMarker()).toMatchObject({ kind: 'rebind', id: 'rebind-1' });
    mount();
    expect(await screen.findByRole('button', { name: '开启远程访问' })).toBeTruthy();
    expect(screen.getByTestId('where').textContent).toBe('/settings/remote-access');
  });

  it('offers no personal claim on an Avibe this computer records as an organization\'s', async () => {
    const start = vi.spyOn(devicesApi, 'startClaim');
    const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
    render(
      <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: 'organization', instanceRole: 'owner', capabilities }}>
        <MemoryRouter initialEntries={['/settings/remote-access']}>
          <Routes><Route path="/settings/remote-access" element={<DevicesPage />} /></Routes>
        </MemoryRouter>
      </InstanceAuthorizationContext.Provider>,
    );
    expect(await screen.findByText(said('这个 Avibe 属于一个组织，远程访问不在这里开启。'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: '开启远程访问' })).toBeNull();
    expect(document.body.textContent).not.toContain('配对密钥');
    expect(start).not.toHaveBeenCalled();
  });

  it('reads the trip without ending it, so a render that is thrown away cannot lose the hand-over', async () => {
    writeClaimMarker({ kind: 'rebind', id: 'rebind-1', at: Date.now(), away: true });
    // Asking is only a read, however often a render asks.
    expect(awayOnClaim('rebind')).toBe(true);
    expect(awayOnClaim('rebind')).toBe(true);
    expect(awayOnClaim('claim')).toBe(false);
    expect(readClaimMarker()).toMatchObject({ away: true });
    // React renders twice here before any effect runs: the hand-over still happens, and the
    // trip is over only once the page has been shown.
    const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
    render(
      <StrictMode>
        <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
          <MemoryRouter initialEntries={['/settings/remote-access']}>
            <Routes>
              <Route path="/settings/remote-access" element={<DevicesPage />} />
              <Route path="/settings/remote-access/connection" element={<Where />} />
            </Routes>
          </MemoryRouter>
        </InstanceAuthorizationContext.Provider>
      </StrictMode>,
    );
    expect(await screen.findByText('/settings/remote-access/connection')).toBeTruthy();
    expect(readClaimMarker()).toMatchObject({ kind: 'rebind', id: 'rebind-1' });
    expect(readClaimMarker()?.away).toBeUndefined();
  });

  // With the product's own router (a data router), where two navigations in one tick do not both
  // happen: the later one wins. One navigation carries the bind-again on, with the callback's word.
  it('hands a refused bind-again return on in one navigation, and is never left loading', async () => {
    writeClaimMarker({ kind: 'rebind', id: 'rebind-1', at: Date.now(), away: true });
    const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
    const router = createMemoryRouter([
      { path: '/settings/remote-access', element: <DevicesPage /> },
      { path: '/settings/remote-access/connection', element: <Where /> },
    ], { initialEntries: ['/settings/remote-access?claim_error=claim_unavailable'] });
    render(
      <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
        <RouterProvider router={router} />
      </InstanceAuthorizationContext.Provider>,
    );
    expect((await screen.findByTestId('where')).textContent).toBe('/settings/remote-access/connection?claim_error=claim_unavailable');
    expect(router.state.location.pathname).toBe('/settings/remote-access/connection');
    // The marker is the connection page's to settle: it is still there for it.
    expect(readClaimMarker()).toMatchObject({ kind: 'rebind', id: 'rebind-1' });
  });

  it('does not hide a refused return behind "waiting": the request this tab kept is withdrawn', async () => {
    writeClaimMarker({ kind: 'claim', id: 'claim-1', at: Date.now() });
    const claim = vi.spyOn(devicesApi, 'claim').mockResolvedValue({ state: 'pending' });
    const cancel = vi.spyOn(devicesApi, 'cancelClaim').mockResolvedValue();
    mount('/settings/remote-access?claim_error=invalid_return');

    expect(await screen.findByText(said('没有开启：从 avibe.bot 回来的这一步没有通过验证，什么都没有绑定。请再开启一次。'))).toBeTruthy();
    expect(screen.getByRole('button', { name: '开启远程访问' })).toBeTruthy();
    expect(screen.queryByText(said('正在等待 avibe.bot 上的确认…'))).toBeNull();
    await waitFor(() => expect(cancel).toHaveBeenCalledWith('claim-1'));
    expect(claim).not.toHaveBeenCalled();
    expect(readClaimMarker()).toBeNull();
  });

  it('keeps following a request whose state it does not know, and says a retry as a retry', async () => {
    writeClaimMarker({ kind: 'claim', id: 'claim-1', at: Date.now() });
    const claim = vi.spyOn(devicesApi, 'claim').mockResolvedValue({ state: 'approving_elsewhere' });
    mount();

    // Unknown is not "nothing was bound": the request is still followed, and still cancellable.
    expect(await screen.findByText(said('正在等待 avibe.bot 上的确认…'))).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(readClaimMarker()).toMatchObject({ id: 'claim-1' });
    claim.mockResolvedValue({ state: 'waiting', code: 'control_plane_unreachable', retryAfterSeconds: 5 });
    expect(await screen.findByText(said('暂时连不上 avibe.bot，这次请求还开着，Avibe 在重试…'), {}, { timeout: 4000 })).toBeTruthy();
    expect(screen.getByRole('button', { name: '取消' })).toBeTruthy();
  });
});

describe('DevicesPage · devices', () => {
  beforeEach(() => {
    vi.spyOn(devicesApi, 'status').mockResolvedValue(paired as never);
  });

  it('signs a plain browser out at once and says what avibe.bot was told, never that the sign-in ended', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook, chrome]));
    vi.spyOn(devicesApi, 'signOut').mockResolvedValue({
      signedOut: [{ id: 'o1', device: 'Chrome', kind: 'browser', owner: 'unknown', results: ['accepted'] }], healthCleanupPending: true,
    });
    mount();

    fireEvent.click(await screen.findByText('Chrome'));
    fireEvent.click(await screen.findByRole('button', { name: '退出这台设备' }));
    await waitFor(() => expect(api.signOut).toHaveBeenCalledWith('chrome'));
    const notice = await screen.findByRole('status');
    expect(within(notice).getByText('已在这个 Avibe 上退出 Chrome')).toBeTruthy();
    expect(within(notice).getByText(said('已通知 avibe.bot 结束它的账号登录。'))).toBeTruthy();
    // Pending clean-up is conditional: the row showed no Health, so it is not claimed to have had any.
    expect(within(notice).getByText(said('如果它同步过健康数据，清理尚未确认。'))).toBeTruthy();
    expect(document.body.textContent).not.toContain('已结束');
  });

  it('says nothing about Health after signing out a device whose functions are listed without it', async () => {
    const browser: Device = { ...chrome, functions: [{ kind: 'notifications', on: false, canChange: false }] };
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook, browser]));
    vi.spyOn(devicesApi, 'signOut').mockResolvedValue({
      signedOut: [{ id: 'o1', device: 'Chrome', kind: 'browser', owner: 'mine', results: ['queued'] }], healthCleanupPending: true,
    });
    mount();
    fireEvent.click(await screen.findByText('Chrome'));
    fireEvent.click(await screen.findByRole('button', { name: '退出这台设备' }));
    const notice = await screen.findByRole('status');
    expect(within(notice).getByText('已在这个 Avibe 上退出 Chrome')).toBeTruthy();
    expect(notice.textContent).not.toContain('健康');
  });

  it('names a device signed out lately in the activity as its sign-out receipt does', async () => {
    const receipt = { id: 'o1', deviceId: 'gone', device: 'Chrome · macOS', kind: 'browser' as const, owner: 'mine' as const, results: ['accepted' as const] };
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook], { signedOut: [receipt] }));
    vi.spyOn(devicesApi, 'status').mockResolvedValue(paired as never);
    vi.spyOn(devicesApi, 'activity').mockImplementation(async (nameOf) => ({
      items: [{ id: 'e1', deviceId: 'gone', at: new Date().toISOString(), device: nameOf('gone'), event: { kind: 'signedIn' } }],
      nextCursor: null,
    }));
    mount();
    const panel = (await screen.findByText('最近活动')).closest('section') ?? document.body;
    await waitFor(() => expect(panel.textContent).toContain('Chrome · macOS'));
    expect(document.body.textContent).not.toContain('一台已不在列表里的设备');
  });

  it('asks before signing out a device that synced Health, and names the local-only half for whose device it was', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook, iphone, sara]));
    const signOut = vi.spyOn(devicesApi, 'signOut').mockResolvedValue({
      signedOut: [{ id: 'o1', device: 'iPhone', kind: 'iphone', owner: 'unknown', results: ['localOnly'] }], healthCleanupPending: true,
    });
    mount();

    fireEvent.click(await screen.findByText('iPhone'));
    fireEvent.click(await screen.findByRole('button', { name: '退出这台设备' }));
    expect(signOut).not.toHaveBeenCalled();
    // At the computer running Avibe the data goes only once the owner is confirmed: said so, and
    // the button promises no deletion.
    await screen.findByRole('button', { name: '退出' });
    const asked = screen.getByRole('dialog');
    expect(asked.textContent).toContain('这台设备同步过来的健康数据，会在确认这个 Avibe 的所有者之后删除；确认不了就不会删，之后可以在这里单独断开健康。');
    expect(within(asked).queryByRole('button', { name: '退出并删除健康数据' })).toBeNull();
    // (The notice afterwards keeps its Health line: this device listed Health.)
    fireEvent.click(within(asked).getByRole('button', { name: '退出' }));
    await waitFor(() => expect(signOut).toHaveBeenCalledWith('iphone'));
    // The row the page acted on was one of the viewer's own: only that half is said.
    expect(await screen.findByText(said('在那台设备上退出 avibe.bot；设备不在手边'))).toBeTruthy();
    expect(screen.queryByText(said('就在「访问」里移除对方'))).toBeNull();
    expect(screen.getByText(said('它同步过的健康数据，清理尚未确认。'))).toBeTruthy();
  });

  it('says from another device that signing out deletes the Health data', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([{ ...macbook, self: undefined }, iphone, { ...chrome, self: true }]));
    vi.spyOn(devicesApi, 'signOut').mockResolvedValue({ signedOut: [], healthCleanupPending: false });
    mount('/settings/remote-access', 'owner', true);

    fireEvent.click(await screen.findByText('iPhone'));
    fireEvent.click(await screen.findByRole('button', { name: '退出这台设备' }));
    await screen.findByRole('button', { name: '退出并删除健康数据' });
    expect(screen.getByRole('dialog').textContent).toContain('这台设备同步到这个 Avibe 的健康数据会被删除。');
  });

  it('turns Health off only after a confirmation, and disconnects an unproven pairing by its own id', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook, iphone], { unlinkedHealth: [{ id: 'pairing-9', device: 'iPhone 13', data: ['sleep'] }] }));
    const revoke = vi.spyOn(devicesApi, 'revokeHealth').mockResolvedValue();
    const revokeUnlinked = vi.spyOn(devicesApi, 'revokeUnlinkedHealth').mockResolvedValue();
    mount();

    fireEvent.click(await screen.findByText('iPhone'));
    fireEvent.click(await screen.findByRole('switch', { name: '健康' }));
    expect(revoke).not.toHaveBeenCalled();
    expect(screen.getByText(said('已同步的健康数据会删除。要再用，在 iPhone 上重新连接健康。'))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '关闭健康' }));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('iphone'));

    // Back on the device's detail; the unproven pairing is a row of the page itself.
    fireEvent.click(await screen.findByRole('button', { name: '关闭' }));
    fireEvent.click(await screen.findByRole('button', { name: '断开' }));
    fireEvent.click(await screen.findByRole('button', { name: '关闭健康' }));
    await waitFor(() => expect(revokeUnlinked).toHaveBeenCalledWith('pairing-9'));
  });

  it('says an answer that never arrived as unknown and reads again, never as a failure', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook, chrome]));
    vi.spyOn(devicesApi, 'signOut').mockRejectedValue(new LostAnswer());
    mount();

    fireEvent.click(await screen.findByText('Chrome'));
    const reads = api.devices.mock.calls.length;
    fireEvent.click(await screen.findByRole('button', { name: '退出这台设备' }));
    expect(await screen.findByText(said('没有收到结果，不确定有没有完成。下面是重新读取到的状态。'))).toBeTruthy();
    expect(screen.queryByText(said('没有完成，请再试一次。'))).toBeNull();
    expect(api.devices.mock.calls.length).toBeGreaterThan(reads);
  });

  it('says a tunnel it has just bound as connecting, with no retry, until a connection is read', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    writeClaimMarker({ kind: 'claim', id: 'claim-1', at: Date.now() });
    vi.spyOn(devicesApi, 'claim').mockResolvedValue({ state: 'bound', hostname: host } as never);
    const registering = { ...paired, tunnel_quality: {
      schema_version: 2, state: 'degraded', grade: 'unknown', sampled_at: new Date().toISOString(), protocol: 'quic',
      connector_count: 1, ha_connections: 0, rtt_ms: null, recovery: { state: 'idle', last_attempt_at: null, next_attempt_at: null },
    } };
    const status = vi.spyOn(devicesApi, 'status').mockResolvedValue(registering as never);
    mount();
    expect(await screen.findByText('正在连接…')).toBeTruthy();
    expect(screen.queryByText('连不上 Cloudflare，其他设备暂时打不开')).toBeNull();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
    // Registered: the next read, soon after, says so.
    status.mockResolvedValue({ ...registering, tunnel_quality: { ...registering.tunnel_quality, state: 'healthy', grade: 'good', ha_connections: 4, rtt_ms: { min: 180, median: 202, max: 240 } } } as never);
    for (let second = 0; second < 3; second += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    }
    expect(await screen.findByText(said('连接良好'))).toBeTruthy();
  });

  it('stops saying connecting a minute after binding when the metrics still cannot be read, and stops following', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    writeClaimMarker({ kind: 'claim', id: 'claim-1', at: Date.now() });
    vi.spyOn(devicesApi, 'claim').mockResolvedValue({ state: 'bound', hostname: host } as never);
    // Running, and its metrics never read: no reading, and the zero it began with (fresh each time).
    const status = vi.spyOn(devicesApi, 'status').mockImplementation(async () => ({ ...paired, tunnel_quality: {
      schema_version: 2, state: 'unknown', grade: 'unknown', sampled_at: new Date().toISOString(), protocol: 'quic',
      connector_count: 1, ha_connections: 0, rtt_ms: null, recovery: { state: 'idle', last_attempt_at: null, next_attempt_at: null },
    } }) as never);
    mount();
    expect(await screen.findByText('正在连接…')).toBeTruthy();
    for (let second = 0; second < 80; second += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    }
    // A minute on, this is no longer the start: said as it is, with nothing promised.
    expect(await screen.findByText('现在读不到连接的状态')).toBeTruthy();
    expect(screen.queryByText('正在连接…')).toBeNull();
    // And the page no longer reads every two seconds: only its ordinary refresh is left.
    const reads = status.mock.calls.length;
    for (let second = 0; second < 10; second += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    }
    expect(status.mock.calls.length - reads).toBeLessThanOrEqual(1);
  });

  it('says turning remote access off from afar as what it is: no answer comes back when it worked', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([{ ...macbook, self: undefined }, { ...chrome, self: true }]));
    const status = vi.spyOn(devicesApi, 'status').mockResolvedValue(paired as never);
    vi.spyOn(devicesApi, 'stop').mockImplementation(async () => {
      // The tunnel is gone before the answer leaves, and nothing can be read through it after.
      status.mockRejectedValue(new LostAnswer());
      vi.spyOn(devicesApi, 'devices').mockRejectedValue(new LostAnswer());
      throw new LostAnswer();
    });
    mount('/settings/remote-access', 'owner', true);

    fireEvent.click(await screen.findByRole('switch', { name: '远程访问' }));
    fireEvent.click(await screen.findByRole('button', { name: '关闭远程访问' }));
    expect(await screen.findByText(said('没有收到回答，这正是关闭成功时的样子'))).toBeTruthy();
    expect(screen.queryByText(said('没有完成，请再试一次。'))).toBeNull();
    expect(screen.queryByText(said('现在读不到远程访问的状态。'))).toBeNull();
  });

  it('takes "it worked" back when a later read shows remote access still on', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([{ ...macbook, self: undefined }, { ...chrome, self: true, online: { path: 'tunnel' } }]));
    vi.spyOn(devicesApi, 'status').mockResolvedValue(paired as never);
    // The request never left (the phone dropped off the network for a moment): nothing was turned off.
    const stop = vi.spyOn(devicesApi, 'stop').mockRejectedValue(new LostAnswer());
    mount('/settings/remote-access', 'owner', true);

    fireEvent.click(await screen.findByRole('switch', { name: '远程访问' }));
    fireEvent.click(await screen.findByRole('button', { name: '关闭远程访问' }));
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
    // The read that follows goes through the tunnel that was supposed to be gone.
    expect(await screen.findByText(said('没有收到结果，不确定有没有完成。下面是重新读取到的状态。'))).toBeTruthy();
    expect(screen.queryByText(said('这正是关闭成功时的样子'))).toBeNull();
    expect(screen.getByRole('switch', { name: '远程访问' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getAllByText('Cloudflare 隧道').length).toBeGreaterThan(0);
  });

  it('does not call a device list it could not read empty', async () => {
    vi.spyOn(devicesApi, 'devices').mockRejectedValue(new DevicesApiError(500, 'http_500', {}));
    mount();
    expect(await screen.findByText('现在读不到设备列表。')).toBeTruthy();
    expect(screen.queryByText('还没有设备。')).toBeNull();
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook]));
    fireEvent.click(screen.getAllByRole('button', { name: '重试' })[0]);
    expect(await screen.findByText('MacBook')).toBeTruthy();
  });

  it('says so when a switch needs the device itself, and changes nothing on the page', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook, iphone]));
    vi.spyOn(devicesApi, 'setFunction').mockRejectedValue(new DevicesApiError(409, 'device_enrollment_required', {}));
    mount();

    fireEvent.click(await screen.findByText('iPhone'));
    fireEvent.click(await screen.findByRole('switch', { name: '通知' }));
    expect(await screen.findByText(said('要先在那台设备上打开这一项。'))).toBeTruthy();
    expect(screen.getByRole('switch', { name: '通知' }).getAttribute('aria-checked')).toBe('true');
  });

  it('signs out every other device only after a confirmation, through the one route that keeps the asker', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([macbook, { ...iphone, self: true }, chrome, { ...chrome, id: 'safari', name: 'Safari' }].map((each) => (each.id === 'host' ? { ...each, self: undefined } : each)), { bulk: 'other_sessions' }));
    const others = vi.spyOn(devicesApi, 'signOutOthers').mockResolvedValue({ signedOut: [], healthCleanupPending: false });
    const one = vi.spyOn(devicesApi, 'signOut');
    mount();

    fireEvent.click(await screen.findByRole('button', { name: '退出其他所有设备' }));
    expect(others).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button', { name: '全部退出' }));
    await waitFor(() => expect(others).toHaveBeenCalledTimes(1));
    expect(one).not.toHaveBeenCalled();
  });

  // A Member may operate the instance (`can_manage_instance`) and still is not its owner
  // (docs/plans/member-instance-operations.md): starting, stopping and optimizing the tunnel are
  // operation; binding is identity.
  it('PERMISSIONS-018 keeps Member transport usable while pairing is disabled', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([{ ...chrome, self: true }]));
    const status = vi.spyOn(devicesApi, 'status').mockResolvedValue(paired as never);
    const stop = vi.spyOn(devicesApi, 'stop').mockImplementation(async () => {
      status.mockResolvedValue({ ...paired, running: false } as never);
      return { ...paired, running: false } as never;
    });
    const start = vi.spyOn(devicesApi, 'start').mockImplementation(async () => {
      status.mockResolvedValue(paired as never);
      return paired as never;
    });
    const optimize = vi.spyOn(devicesApi, 'optimizeRoute').mockResolvedValue(paired as never);
    const claim = vi.spyOn(devicesApi, 'startClaim');
    mount('/settings/remote-access', 'member');

    // Nothing of the owner's: no binding, no connection settings, no domain, nobody else's devices.
    const remoteAccess = await screen.findByRole('switch', { name: '远程访问' });
    expect(screen.queryByRole('button', { name: '开启远程访问' })).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByText('连接方式')).toBeNull();
    expect(screen.queryByText('其他人的设备')).toBeNull();
    expect(api.activity).not.toHaveBeenCalled();
    expect(api.address).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /优化线路/ }));
    await waitFor(() => expect(optimize).toHaveBeenCalledOnce());

    // Turning it off from afar asks first, as it does for the owner.
    fireEvent.click(remoteAccess);
    expect(stop).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog').textContent).toContain('所有远程设备（包括这一台）都打不开');
    // The domain is not a member's: it is kept, not kept for them.
    expect(screen.getByRole('dialog').textContent).toContain('域名会保留。');
    expect(screen.getByRole('dialog').textContent).not.toContain('为你保留');
    fireEvent.click(screen.getByRole('button', { name: '关闭远程访问' }));
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.getByRole('switch', { name: '远程访问' }).getAttribute('aria-checked')).toBe('false'));
    fireEvent.click(screen.getByRole('switch', { name: '远程访问' }));
    await waitFor(() => expect(start).toHaveBeenCalledOnce());
    expect(claim).not.toHaveBeenCalled();
  });

  it('PERMISSIONS-018 offers an unbound Member nothing to bind with', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([{ ...chrome, self: true }]));
    vi.spyOn(devicesApi, 'status').mockResolvedValue(unpaired as never);
    const claim = vi.spyOn(devicesApi, 'startClaim');
    mount('/settings/remote-access', 'member');
    expect(await screen.findByText('Chrome')).toBeTruthy();
    await waitFor(() => expect(api.status).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: '开启远程访问' })).toBeNull();
    expect(screen.queryByRole('switch', { name: '远程访问' })).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(claim).not.toHaveBeenCalled();
  });

  it.each([true, false])('PERMISSIONS-018 preserves the Owner\'s controls (bound=%s)', async (bound) => {
    vi.spyOn(devicesApi, 'status').mockResolvedValue((bound ? paired : unpaired) as never);
    mount('/settings/remote-access', 'owner');
    if (bound) {
      expect(await screen.findByRole('switch', { name: '远程访问' })).toBeTruthy();
      expect(screen.getByText('连接方式')).toBeTruthy();
    } else {
      expect(await screen.findByRole('button', { name: '开启远程访问' })).toBeTruthy();
    }
  });

  it('shows someone who may not operate the instance their own devices and nothing else', async () => {
    vi.spyOn(devicesApi, 'devices').mockResolvedValue(snapshot([{ ...chrome, self: true }]));
    mount('/settings/remote-access', 'guest');

    expect(await screen.findByText('Chrome')).toBeTruthy();
    expect(screen.getByText(said('你登录了这个 Avibe 的设备。'))).toBeTruthy();
    expect(screen.queryByRole('switch', { name: '远程访问' })).toBeNull();
    expect(screen.queryByText('连接方式')).toBeNull();
    expect(api.status).not.toHaveBeenCalled();
    expect(api.activity).not.toHaveBeenCalled();
    expect(api.address).not.toHaveBeenCalled();
  });

  it('reads older activity a page at a time, as far as the instance keeps it', async () => {
    const entry = (id: string, minutes: number) => ({ id, deviceId: 'host', at: new Date(Date.now() - minutes * 60_000).toISOString(), device: 'MacBook', event: { kind: 'remoteAccessOn' as const } });
    const read = vi.spyOn(devicesApi, 'activity').mockImplementation(async (_names, cursor) => (
      cursor === 'page-2' ? { items: [entry('e3', 30), entry('e2', 20)], nextCursor: null } : { items: [entry('e1', 10), entry('e2', 20)], nextCursor: 'page-2' }
    ));
    mount('/settings/remote-access/activity');

    const more = await screen.findByRole('button', { name: '更早的活动' });
    expect(screen.getAllByText('远程访问已开启')).toHaveLength(2);
    fireEvent.click(more);
    // The entry both pages carry is listed once; with no cursor left, nothing more is offered.
    await waitFor(() => expect(screen.getAllByText('远程访问已开启')).toHaveLength(3));
    expect(read).toHaveBeenLastCalledWith(expect.any(Function), 'page-2');
    expect(screen.queryByRole('button', { name: '更早的活动' })).toBeNull();
  });

  it('keeps the entries a refresh pushes off the newest page once older ones were read', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const entry = (id: string, minutes: number) => ({ id, deviceId: 'host', at: new Date(Date.now() - minutes * 60_000).toISOString(), device: 'MacBook', event: { kind: 'remoteAccessOn' as const } });
    let newest = [entry('e3', 30), entry('e2', 40)];
    vi.spyOn(devicesApi, 'activity').mockImplementation(async (_names, cursor) => (
      cursor ? { items: [entry('e1', 50)], nextCursor: null } : { items: newest, nextCursor: 'older' }
    ));
    mount('/settings/remote-access/activity');
    fireEvent.click(await screen.findByRole('button', { name: '更早的活动' }));
    await waitFor(() => expect(screen.getAllByText('远程访问已开启')).toHaveLength(3));

    // A new event arrives: the newest page is now e4, e3, and e2 is no longer on it.
    newest = [entry('e4', 1), entry('e3', 30)];
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
    await waitFor(() => expect(screen.getAllByText('远程访问已开启')).toHaveLength(4));
    vi.useRealTimers();
  });

  it('shows the notice that the address was changed somewhere else, with the way to bind again', async () => {
    vi.spyOn(devicesApi, 'address').mockResolvedValue({
      hostname: 'sam-app.avibe.bot', eligible: true, retired: [], healthConnected: false,
      unrecognizedChange: { previousHostname: host, hostname: 'sam-app.avibe.bot' },
    });
    mount();

    expect(await screen.findByText('这个 Avibe 的地址在别处被改了')).toBeTruthy();
    expect(screen.getByText(said('如果这台电脑上的 Avibe 数据可能被人整个复制走过，请在 avibe.bot 上删除这个 Avibe，重新绑定后再把名字拿回来。'))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '去重新绑定' }));
    expect(await screen.findByText('/settings/remote-access/connection')).toBeTruthy();
  });
});

describe('DevicesPage · an instance without binding again', () => {
  it('sends nobody to an entry or a command that is not there', async () => {
    vi.spyOn(devicesApi, 'startClaim').mockRejectedValue(new DevicesApiError(409, 'link_required', { state: 'error', code: 'link_required' }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '开启远程访问' }));
    expect(await screen.findByText(said('从这个地址开不了远程访问。请在运行 Avibe 的那台电脑上打开 Avibe 再开启。'))).toBeTruthy();
    expect(document.body.textContent).not.toContain('vibe remote');
    expect(document.body.textContent).not.toContain('重新绑定');
  });

  it('says the same refusals with their way out once the instance has it', async () => {
    vi.spyOn(devicesApi, 'rebindOffered').mockResolvedValue(true);
    vi.spyOn(devicesApi, 'startClaim').mockRejectedValue(new DevicesApiError(409, 'link_required', { state: 'error', code: 'link_required' }));
    mount();
    await waitFor(() => expect(devicesApi.rebindOffered).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole('button', { name: '开启远程访问' }));
    expect(await screen.findByText(said('服务器上没有浏览器的话，在它的终端运行 vibe remote rebind。'))).toBeTruthy();
  });

  it('says a refused credential without pointing to binding again, in the list and in the notice', () => {
    const row = { id: 'o1', device: 'Chrome', kind: 'browser' as const, owner: 'mine' as const, results: ['refusedCredential' as const] };
    const without = render(<DevicesPageView reach={on} devices={[macbook]} signedOut={[row]} rebindOffered={false} />);
    expect(screen.getByText(said('avibe.bot 不再接受这台电脑的绑定，所以没有结束它的账号登录。'))).toBeTruthy();
    expect(document.body.textContent).not.toContain('重新绑定');
    without.unmount();
    render(<DevicesPageView reach={on} devices={[macbook]} signedOut={[row]} />);
    expect(screen.getByText(said('avibe.bot 不再接受这台电脑的绑定，所以没有结束它的账号登录。先重新绑定远程访问。'))).toBeTruthy();
  });
});

describe('DevicesPageView', () => {
  it('groups devices by the list each one names, and keeps other people\'s functions read-only', () => {
    const orphan: Device = { id: 'orphan', group: 'unattributed', kind: 'browser', name: null, client: { kind: 'browser' }, pushOnly: true, functions: [] };
    render(<DevicesPageView reach={on} devices={[macbook, iphone, sara, orphan]} detail="sara" />);
    expect(screen.getByText('我的设备')).toBeTruthy();
    expect(screen.getByText('其他人的设备')).toBeTruthy();
    expect(screen.getByText('认不出主人的通知设备')).toBeTruthy();
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).queryByRole('switch')).toBeNull();
    expect(within(dialog).getByText(said('这是别人的设备，功能由对方在自己的设备上开关。'))).toBeTruthy();
  });

  it('names the path of a device only where a connection is observed', () => {
    render(<DevicesPageView reach={on} devices={[macbook, iphone, { ...chrome, lastActiveAt: new Date(Date.now() - 3 * 86_400_000).toISOString() }]} />);
    expect(screen.getAllByText('打洞直连').length).toBeGreaterThan(0);
    expect(screen.queryByText('Cloudflare 隧道')).toBeNull();
  });

  it('never draws a switch that would be refused', () => {
    expect(functionControl({ kind: 'notifications', on: true, canChange: true })).toBe('switch');
    expect(functionControl({ kind: 'notifications', on: true, canChange: false })).toBe('state');
    expect(functionControl({ kind: 'health', on: false, canChange: true, data: [] })).toBe('state');
    expect(functionControl({ kind: 'computerUse', status: 'ready' })).toBe('state');
  });

  it('says both halves of the local-only advice only when whose device it was is not known', () => {
    const t = i18n.t.bind(i18n);
    const [mine] = accountResultTexts(t, { owner: 'mine', results: ['localOnly'] });
    const [others] = accountResultTexts(t, { owner: 'others', results: ['localOnly'] });
    const [either] = accountResultTexts(t, { owner: 'unknown', results: ['localOnly', 'localOnly'] });
    expect(mine).toContain('在那台设备上退出 avibe.bot');
    expect(mine).not.toContain('移除对方');
    expect(others).toContain('在「访问」里移除对方');
    expect(others).not.toContain('账号安全');
    expect(either).toContain('是你自己的设备的话');
    expect(either).toContain('是别人的设备的话');
    expect(accountResultTexts(t, { owner: 'mine', results: ['queued'] })).toEqual(['已排队，还没有通知到 avibe.bot。远程访问关着或这台电脑休眠时会一直等着。']);
  });

  it('counts who is locked out without counting notification-only rows', () => {
    const push: Device = { ...chrome, id: 'push', pushOnly: true };
    expect(lockedOutCount([{ ...macbook, self: undefined }, { ...iphone, self: true }, chrome, push])).toBe(1);
  });
});
