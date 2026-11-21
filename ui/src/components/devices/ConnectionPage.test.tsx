/** @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import i18n from '../../i18n';
import { DevicesApiError, LostAnswer, devicesApi } from './api';
import { writeClaimMarker } from './claimFlow';
import { ConnectionPage } from './ConnectionPage';
import { ConnectionPageView, type ConnectionPageViewProps } from './ConnectionPageView';
import { said } from './testText';
import type { AddressState, ConnectionState, TunnelChange } from './types';

// ui/design: settings/devices-connection/*, settings/devices-rename/*
const host = 'max-app.avibe.bot';
const settings = { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' } as const;
const connection = (extra: Partial<ConnectionState> = {}): ConnectionState => ({ settings: { ...settings }, interfaces: [], direct: 'ready', ...extra });
const address = (extra: Partial<AddressState> = {}): AddressState => ({
  hostname: host, eligible: true, holdDays: 30, releaseAtIfChangedNow: '2026-12-23T00:00:00.000Z', retired: [], healthConnected: false, ...extra,
});
const status = { ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}` };

function Where() {
  const location = useLocation();
  return <div data-testid="where">{location.pathname}</div>;
}

function mount(path = '/settings/remote-access/connection', remote = false) {
  const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
  return render(
    <InstanceAuthorizationContext.Provider value={{ remote, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/settings/remote-access/connection" element={<><ConnectionPage /><Where /></>} />
          <Route path="/settings/remote-access/connection/address" element={<><ConnectionPage /><Where /></>} />
        </Routes>
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

const view = (props: Partial<ConnectionPageViewProps> = {}) => render(
  <ConnectionPageView load="ready" connection={connection()} tunnel={{ state: 'up', active: 'quic' }} remote={false} host={host} address={address()} {...props} />,
);

let read: ReturnType<typeof vi.spyOn>;
beforeEach(async () => {
  await i18n.changeLanguage('zh');
  window.sessionStorage.clear();
  vi.spyOn(devicesApi, 'interfaces').mockResolvedValue([]);
  read = vi.spyOn(devicesApi, 'connection').mockResolvedValue(connection());
  vi.spyOn(devicesApi, 'status').mockResolvedValue(status as never);
  vi.spyOn(devicesApi, 'address').mockResolvedValue(address());
  vi.spyOn(devicesApi, 'rebind').mockResolvedValue({ state: 'ready' });
  vi.spyOn(devicesApi, 'devices').mockResolvedValue({ devices: [], unlinkedHealth: [], signedOut: [], bulk: null });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('ConnectionPage · settings', () => {
  it('puts 「自动」 first in every option order', async () => {
    mount();
    const ip = await screen.findByRole('radiogroup', { name: '边缘网络 IP' });
    expect(within(ip).getAllByRole('radio').map((each) => each.textContent)).toEqual(['自动', 'IPv4', 'IPv6']);
    const protocol = screen.getByRole('radiogroup', { name: '传输协议' });
    expect(within(protocol).getAllByRole('radio')[0].textContent).toBe('自动');
  });

  it('treats a lost answer as unknown: it reads the result again and never sends the change twice', async () => {
    const change = vi.spyOn(devicesApi, 'changeConnection').mockRejectedValue(new LostAnswer());
    mount();
    fireEvent.click(within(await screen.findByRole('radiogroup', { name: '传输协议' })).getByRole('radio', { name: 'HTTP/2' }));

    expect(await screen.findByText(said('没收到结果。重新连上后会自动读取结果，不会重复这次更改。'))).toBeTruthy();
    expect(screen.queryByText(/失败/)).toBeNull();
    const reads = read.mock.calls.length;
    // The instance's own state, read over the tunnel once it answers, says how it ended.
    read.mockResolvedValue(connection({ settings: { ...settings, transport_protocol: 'http2' }, change: { setting: 'protocol', state: 'applied', to: 'http2' } }));
    expect(await screen.findByText('已生效', {}, { timeout: 4000 })).toBeTruthy();
    expect(read.mock.calls.length).toBeGreaterThan(reads);
    expect(change).toHaveBeenCalledTimes(1);
    expect(change).toHaveBeenCalledWith('protocol', 'http2');
  });

  it('refuses a direct switch with one sentence when a healthy tunnel cannot be confirmed', async () => {
    vi.spyOn(devicesApi, 'changeConnection').mockRejectedValue(new DevicesApiError(409, 'tunnel_unhealthy', {}));
    mount('/settings/remote-access/connection', true);
    const direct = await screen.findByRole('switch', { name: '直连' });
    fireEvent.click(direct);

    expect(await screen.findByText(said('没有改：现在确认不了 Cloudflare 隧道是通的，关掉可能让你断开。稍后再试。'))).toBeTruthy();
    expect(screen.getByRole('switch', { name: '直连' }).getAttribute('aria-checked')).toBe('true');
  });

  it('says the state is not readable instead of showing settings it cannot trust', async () => {
    read.mockRejectedValue(new DevicesApiError(503, 'connection_state_unavailable', {}));
    mount();
    expect(await screen.findByText(said('现在读不准连接方式的状态，先不显示这些设置。稍后再试。'))).toBeTruthy();
    expect(screen.queryByRole('radiogroup', { name: '传输协议' })).toBeNull();
  });

  it('sends anyone but the owner back to the Devices page and reads nothing', async () => {
    const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: false };
    render(
      <InstanceAuthorizationContext.Provider value={{ remote: true, instanceKind: 'personal', instanceRole: 'member', capabilities }}>
        <MemoryRouter initialEntries={['/settings/remote-access/connection']}>
          <Routes>
            <Route path="/settings/remote-access/connection" element={<ConnectionPage />} />
            <Route path="/settings/remote-access" element={<div>devices</div>} />
          </Routes>
        </MemoryRouter>
      </InstanceAuthorizationContext.Provider>,
    );
    expect(await screen.findByText('devices')).toBeTruthy();
    expect(read).not.toHaveBeenCalled();
    expect(devicesApi.address).not.toHaveBeenCalled();
    expect(devicesApi.rebind).not.toHaveBeenCalled();
  });

  it('lets a remote session see that binding again is done at the computer, not the control', async () => {
    mount('/settings/remote-access/connection', true);
    expect(await screen.findByText(said('重新绑定远程访问要在运行 Avibe 的那台电脑上做'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重新绑定' })).toBeNull();
    expect(devicesApi.rebind).not.toHaveBeenCalled();
  });
});

const cooldown = (extra: Record<string, unknown> = {}) => ({
  schema_version: 2, state: 'degraded', grade: 'poor', sampled_at: new Date().toISOString(),
  protocol: 'quic', connector_count: 1, ha_connections: 4,
  rtt_ms: { min: 200, median: 250, max: 350 }, baseline_median_rtt_ms: 100,
  edge_locations: [], window_seconds: 60, request_errors_per_minute: 0, packet_loss_per_minute: 0,
  recovery: {
    state: 'cooldown', last_attempt_at: new Date(Date.now() - 30_000).toISOString(), last_trigger: 'latency', last_result: 'no_improvement',
    previous_median_rtt_ms: 250, result_median_rtt_ms: 240, next_attempt_at: new Date(Date.now() + 120_000).toISOString(), attempt_count_window: 2,
    ...extra,
  },
});
const withQuality = (quality: unknown, extra: Record<string, unknown> = {}) => (
  { ...status, settings: { ...settings }, tunnel_quality: quality, ...extra }
);

describe('ConnectionPage · 优化线路', () => {
  it('RA-TQ-040 explains automatic waiting without disabling a manual retry', async () => {
    vi.spyOn(devicesApi, 'status').mockResolvedValue(withQuality(cooldown()) as never);
    mount();
    const row = await screen.findByRole('button', { name: /优化线路/ });
    await waitFor(() => expect(row.textContent).toContain('自动优化将在'));
    expect(row.textContent).toContain('现在仍可手动优化。');
    expect((row as HTMLButtonElement).disabled).toBe(false);
  });

  it('keeps an evaluation that is still running visible, and does not start another', async () => {
    vi.spyOn(devicesApi, 'status').mockResolvedValue(withQuality(cooldown({ state: 'evaluating' })) as never);
    const optimize = vi.spyOn(devicesApi, 'optimizeRoute');
    mount();
    const row = await screen.findByRole('button', { name: /优化中/ });
    expect((row as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(row);
    expect(optimize).not.toHaveBeenCalled();
  });

  it('waits out the short pause after an attempt before a manual one can be asked for again', async () => {
    vi.spyOn(devicesApi, 'status').mockResolvedValue(withQuality(cooldown({ last_attempt_at: new Date().toISOString() })) as never);
    mount();
    const row = await screen.findByRole('button', { name: /优化线路/ });
    await waitFor(() => expect(row.textContent).toContain('再次优化'));
    expect((row as HTMLButtonElement).disabled).toBe(true);
  });

  it.each(['route_optimization_evaluating', 'remote_access_busy', 'route_optimization_debounce', 'route_optimization_evidence_unavailable'])(
    'reports %s instead of claiming an evaluation started', async (error) => {
      vi.spyOn(devicesApi, 'optimizeRoute').mockResolvedValue({ ...status, ok: false, error, retry_after_seconds: 6 } as never);
      mount();
      const row = await screen.findByRole('button', { name: /优化线路/ });
      await waitFor(() => expect((row as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(row);
      await waitFor(() => expect(row.textContent).toContain('没有开始优化：'));
      expect(row.textContent).toContain(i18n.t(`errors.${error}`, { seconds: 6 }));
      expect(row.textContent).not.toContain(i18n.t('remoteAccess.optimizeStarted'));
    },
  );
});

describe('ConnectionPage · 连接诊断与网络路径', () => {
  const open = async () => {
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '连接诊断与网络路径' }));
    return screen.findByRole('dialog');
  };

  it('carries the direct certificate\'s expiry and the last connection\'s timing while direct is on', async () => {
    vi.spyOn(devicesApi, 'status').mockResolvedValue({ ...status, direct: { enabled: true, state: 'running', reason: '', certificate_not_after: '2099-02-14T00:00:00Z' } } as never);
    const dialog = await open();
    const rows = await within(dialog).findByTestId('direct-details');
    expect(rows.textContent).toContain('直连证书');
    expect(rows.textContent).toContain('有效期至 2099年2月14日');
    // Only the Avibe App on this device records the race; a browser says so instead of a number.
    expect(rows.textContent).toContain('最近一次连接');
    expect(rows.textContent).toContain('这台设备没有记录');
  });

  it('direct off: rows absent', async () => {
    vi.spyOn(devicesApi, 'status').mockResolvedValue({ ...status, direct: { enabled: false, state: 'off', reason: '', certificate_not_after: '2099-02-14T00:00:00Z' } } as never);
    const dialog = await open();
    expect(within(dialog).getByText('连接诊断')).toBeTruthy();
    expect(within(dialog).queryByTestId('direct-details')).toBeNull();
    expect(dialog.textContent).not.toContain('直连证书');
    expect(dialog.textContent).not.toContain('最近一次连接');
  });
});

describe('ConnectionPage · the domain', () => {
  it('goes back to the form with the one sentence when the name cannot be taken', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(devicesApi, 'previewRename').mockResolvedValue({ confirmationId: 'conf-1', hostname: 'maxwell-app.avibe.bot', isReturn: false, holdDays: 30, releaseAt: '2026-12-23T00:00:00.000Z', healthConnected: false, devices: [] });
    const change = vi.spyOn(devicesApi, 'changeAddress').mockRejectedValue(new DevicesApiError(409, 'taken', { ok: false, error: 'taken' }));
    mount('/settings/remote-access/connection/address');

    const input = await screen.findByRole('textbox', { name: '新名称' });
    // What renaming does is said before anything is asked: the old address stops at once, and
    // the day it is kept until is the real one.
    expect(screen.getByText(said(`只有你的账号能拿回 ${host}；之后别人可以领取。`))).toBeTruthy();
    expect(document.body.textContent).not.toContain('48');
    expect(document.body.textContent).not.toContain('不会还给你');
    fireEvent.change(input, { target: { value: 'Maxwell' } });
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(await screen.findByText('改成 maxwell-app.avibe.bot？')).toBeTruthy();
    expect(devicesApi.previewRename).toHaveBeenCalledWith('maxwell', undefined);
    expect(screen.getByText(said(`${host} 立即停用`))).toBeTruthy();

    const confirm = screen.getByRole('button', { name: /更改域名/ });
    expect(confirm.hasAttribute('disabled')).toBe(true);
    for (let second = 0; second < 6; second += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    }
    fireEvent.click(screen.getByRole('button', { name: '更改域名' }));

    expect(await screen.findByText(said('已被占用，换一个名称。'))).toBeTruthy();
    expect(change).toHaveBeenCalledWith('conf-1');
    // Refused: what renaming would do is folded away, and nothing was changed.
    expect(screen.queryByText('改名后')).toBeNull();
  });

  it('reopens on a change that is still running, and offers no second one', async () => {
    vi.spyOn(devicesApi, 'address').mockResolvedValue(address({ operation: { state: 'pending', hostname: 'maxwell-app.avibe.bot' } }));
    mount('/settings/remote-access/connection/address');
    expect(await screen.findByText(said('avibe.bot 还没有答复，更改还在进行；这一页会自己更新，不用再点一次。'))).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: '新名称' })).toBeNull();
  });
});

describe('ConnectionPage · binding again', () => {
  it('confirms with the sentence about copied data, then leaves through the local API\'s own form', async () => {
    let fields: Record<string, string> | null = null;
    vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(function submit(this: HTMLFormElement) {
      fields = Object.fromEntries(Array.from(this.querySelectorAll('input')).map((input) => [input.name, input.value]));
    });
    const start = vi.spyOn(devicesApi, 'startRebind').mockResolvedValue({ claimId: 'rebind-1', action: 'https://avibe.bot/api/v1/claims/redirect-entry', method: 'POST', fields: { user_code: 'CODE', redirect_state: 'STATE' }, referrerPolicy: 'strict-origin' });
    vi.spyOn(devicesApi, 'rebindClaim').mockResolvedValue({ state: 'pending' });
    mount();

    fireEvent.click(await screen.findByRole('button', { name: '重新绑定' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(said('如果这台电脑上的 Avibe 数据可能被人整个复制走过，请在 avibe.bot 上删除这个 Avibe，重新绑定后再把名字拿回来。'))).toBeTruthy();
    expect(start).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: '重新绑定' }));
    await waitFor(() => expect(fields).toEqual({ user_code: 'CODE', redirect_state: 'STATE' }));
  });

  it('offers binding as a new computer only after a refusal that allows it, to the browser that started the attempt', async () => {
    vi.spyOn(devicesApi, 'rebind').mockResolvedValue({ state: 'asNewOffered', reason: 'other_account', hint: 'm***@gmail.com' });
    vi.spyOn(devicesApi, 'rebindClaim').mockResolvedValue({ state: 'error', code: 'installation_unverified', reasonCode: 'other_account' });
    const preview = vi.spyOn(devicesApi, 'previewReset').mockResolvedValue({ confirmationId: 'reset-1', healthConnected: true });
    const reset = vi.spyOn(devicesApi, 'reset').mockResolvedValue('unpaired');
    writeClaimMarker({ kind: 'rebind', id: 'rebind-1', at: Date.now() });
    mount();

    expect(await screen.findByText(said('属于另一个账号（m***@gmail.com）'))).toBeTruthy();
    fireEvent.click(await screen.findByRole('button', { name: '作为一台新电脑重新绑定' }));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(preview).toHaveBeenCalledWith('rebind-1'));
    // Whose Avibe it is comes first, then what is discarded; the copied-data sentence is the reset's own.
    expect(dialog.textContent).toContain('这台电脑绑定的 Avibe 属于另一个账号（m***@gmail.com）。');
    expect(dialog.textContent).toContain('你授权过的人都会失去访问权限');
    expect(dialog.textContent).toContain('已同步的健康数据会删除');
    expect(dialog.textContent).toContain('还要在 avibe.bot 上删除原来的 Avibe：在那之前，拿到副本的人仍然可以用它。');
    expect(reset).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: '作为一台新电脑重新绑定' }));
    await waitFor(() => expect(reset).toHaveBeenCalledWith('rebind-1', 'reset-1'));
    expect(await screen.findByText(said('完成。这台电脑不再绑定任何 Avibe，电脑上的内容原样保留。'))).toBeTruthy();
  });

  it('offers nothing after an unexplained refusal, and continues an unfinished attempt instead of starting one', async () => {
    vi.spyOn(devicesApi, 'rebind').mockResolvedValue({ state: 'ready', refusal: { code: 'binding_unavailable' } });
    const { unmount } = mount();
    expect(await screen.findByText(said('avibe.bot 拒绝了这次请求，没有给出原因。什么都没有绑定。'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: '作为一台新电脑重新绑定' })).toBeNull();
    unmount();

    vi.spyOn(devicesApi, 'rebind').mockResolvedValue({ state: 'recovering' });
    mount();
    expect(await screen.findByText(said('Avibe 正在继续那一次，不会开始新的绑定。'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重新绑定' })).toBeNull();
  });
});

describe('ConnectionPageView', () => {
  const line = (change: TunnelChange) => {
    const { unmount } = view({ connection: connection({ change }) });
    const text = screen.getByRole('status').textContent;
    unmount();
    return text;
  };

  it('has one status line for a changed tunnel setting and no keep button', () => {
    expect(line({ setting: 'protocol', state: 'applied' })).toBe('已生效');
    expect(line({ setting: 'protocol', state: 'restored', reason: 'verification_failed', from: 'auto' })).toBe('没连上，已改回「自动」');
    expect(line({ setting: 'outbound', state: 'restoring', from: '' })).toBe('正在改回「跟随系统」');
    view({ connection: connection({ change: { setting: 'protocol', state: 'verifying' } }) });
    expect(screen.queryByRole('button', { name: /保留/ })).toBeNull();
    expect(screen.getByRole('radio', { name: 'QUIC' }).hasAttribute('disabled')).toBe(true);
  });

  it('says how the last connection\'s two paths raced where the App recorded it, and an expired certificate has no date', () => {
    view({ diagnostics: { status: null, diagnostics: null, now: Date.now(), direct: { certificate: null, lastConnection: { direct: 'timeout', public: 212 } } } });
    const rows = screen.getByTestId('direct-details');
    expect(rows.textContent).toContain('直连 超时 / Cloudflare 212 ms');
    expect(rows.textContent).toContain('没有有效的证书');
  });

  it('shows the relay only on an instance enrolled in the trial', () => {
    const { unmount } = view();
    expect(screen.queryByRole('switch', { name: '中继' })).toBeNull();
    unmount();
    view({ connection: connection({ relay: { enabled: false, connected: false, unavailable: 'direct_permission_required' } }) });
    expect(screen.getByRole('switch', { name: '中继' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByText(said('暂时开不了：这个 Avibe 还没有拿到直连的许可。'))).toBeTruthy();
  });

  it('lists exactly the settings that went back to automatic, and says the temporary route in one line', () => {
    view({ connection: connection({
      returnedToAutomatic: { at: '2026-11-21T01:00:00.000Z', changes: [{ setting: 'protocol', from: 'http2' }] },
      temporaryRoute: { protocol: 'http2', differences: ['ipFamily'] },
    }) });
    const returned = screen.getByText(said('固定的设置已改回自动'));
    expect(returned.textContent).toContain('传输协议（原来是「HTTP/2」）');
    expect(returned.textContent).not.toContain('边缘网络 IP');
    expect(returned.textContent).not.toContain('出口网卡');
    expect(screen.getByText(said('隧道现在临时用 HTTP/2 连着，没有按固定的边缘网络 IP；保存的设置没有变。'))).toBeTruthy();
  });

  it('offers this device 「自动」 and 「仅 Cloudflare 隧道」 only, and never a number of seconds', () => {
    view({ remote: true, preference: { mode: 'tunnel', reverted: { at: '2026-11-21T01:00:00.000Z', reason: 'unreachable' } } });
    const choice = screen.getByRole('radiogroup', { name: '这台设备' });
    expect(within(choice).getAllByRole('radio').map((each) => each.textContent?.slice(0, 2))).toEqual(['自动', '仅 ']);
    expect(document.body.textContent).not.toMatch(/\d+\s*秒/);
  });

  it('shows names still kept for the account with their real days, and never calls the list clean', () => {
    const retired = { hostname: 'studio-app.avibe.bot', releaseAt: '2026-12-21T00:00:00.000Z', devices: [{ id: 'd1', name: 'Chrome', kind: 'browser' as const, cleanup: 'cleared' as const }] };
    view({ address: address({ retired: [retired] }), rename: { step: 'cleanup', retired } });
    expect(screen.getByText(said('12月21日之前只有你的账号能拿回'))).toBeTruthy();
    const dialog = screen.getByRole('dialog');
    expect(dialog.textContent).toContain('前提是运行 Avibe 的这台电脑开着');
    expect(dialog.textContent).toContain('清单全部打勾也不代表没有残留');
    expect(dialog.textContent).not.toContain('都已清理');
  });
});
