/** @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import i18n from '../../i18n';
import { DevicesApiError, LostAnswer, devicesApi, type RenamePreview } from './api';
import { readClaimMarker, writeClaimMarker } from './claimFlow';
import { ConnectionPage } from './ConnectionPage';
import { ConnectionPageView, type ConnectionPageViewProps } from './ConnectionPageView';
import { said } from './testText';
import type { AddressState, ConnectionState, TunnelChange } from './types';

// ui/design: settings/devices-connection/*, settings/devices-rename/*
// Where the entry is offered at all: ConnectionPage.renameEntry.test.tsx.
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

  it('follows a direct switch the instance accepted until it is written, and says a refusal that came after', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // `202`: accepted, not applied. The state still holds the old value and the change on its way.
    const change = vi.spyOn(devicesApi, 'changeConnection').mockResolvedValue({ operationId: 'op-1' });
    mount('/settings/remote-access/connection', true);
    const direct = await screen.findByRole('switch', { name: '直连' });
    read.mockResolvedValue(connection({ switching: { setting: 'direct', id: 'op-1' } }));
    fireEvent.click(direct);
    await waitFor(() => expect(change).toHaveBeenCalledWith('direct', false));
    await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
    // Still on its way: the row waits, nothing is said, and the page keeps reading.
    expect((screen.getByRole('switch', { name: '直连' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('alert')).toBeNull();
    const reads = read.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
    expect(read.mock.calls.length).toBeGreaterThan(reads);

    // Written: the switch shows it, and nothing is said.
    read.mockResolvedValue(connection({ direct: 'off', switched: { setting: 'direct', id: 'op-1', failed: false, reason: 'saved' } }));
    await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
    expect(screen.getByRole('switch', { name: '直连' }).getAttribute('aria-checked')).toBe('false');
    expect(screen.queryByRole('alert')).toBeNull();

    // Asked again, accepted, and refused when the instance came to write it: said like a refusal before.
    change.mockResolvedValue({ operationId: 'op-2' });
    read.mockResolvedValue(connection({ direct: 'ready' }));
    for (let second = 0; second < 31; second += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    }
    expect(screen.getByRole('switch', { name: '直连' }).getAttribute('aria-checked')).toBe('true');
    read.mockResolvedValue(connection({ direct: 'ready', switching: { setting: 'direct', id: 'op-2' } }));
    fireEvent.click(screen.getByRole('switch', { name: '直连' }));
    await waitFor(() => expect(change).toHaveBeenCalledTimes(2));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    read.mockResolvedValue(connection({ direct: 'ready', switched: { setting: 'direct', id: 'op-2', failed: true, reason: 'tunnel_unhealthy' } }));
    await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
    expect(await screen.findByText(said('没有改：现在确认不了 Cloudflare 隧道是通的，关掉可能让你断开。稍后再试。'))).toBeTruthy();
    expect(screen.getByRole('switch', { name: '直连' }).getAttribute('aria-checked')).toBe('true');
    expect(change).toHaveBeenCalledTimes(2);
  });

  it('reads at once after a switch whose answer was lost, and says why a refused setting did not change', async () => {
    const change = vi.spyOn(devicesApi, 'changeConnection').mockRejectedValueOnce(new LostAnswer());
    mount();
    fireEvent.click(await screen.findByRole('switch', { name: '直连' }));
    await waitFor(() => expect(change).toHaveBeenCalledWith('direct', false));
    const reads = read.mock.calls.length;
    // The state, read again over whatever path is left, says what became of it.
    read.mockResolvedValue(connection({ direct: 'off' }));
    await waitFor(() => expect(screen.getByRole('switch', { name: '直连' }).getAttribute('aria-checked')).toBe('false'), { timeout: 4000 });
    expect(read.mock.calls.length).toBeGreaterThan(reads);
    expect(change).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('alert')).toBeNull();

    change.mockRejectedValue(new DevicesApiError(409, 'connection_change_busy', {}));
    fireEvent.click(within(screen.getByRole('radiogroup', { name: '优化策略' })).getByRole('radio', { name: '低延迟' }));
    expect(await screen.findByText(said('没有改：另一项更改还在进行，稍后再试。'))).toBeTruthy();
  });

  it('lets a status it can no longer read again grow too old to call the tunnel available', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const sampled = new Date().toISOString();
    const quality = { state: 'healthy', grade: 'good', sampled_at: sampled, protocol: 'quic', ha_connections: 4, rtt_ms: { min: 90, median: 120, max: 160 }, recovery: { state: 'idle', last_attempt_at: null, next_attempt_at: null } };
    const statusRead = vi.spyOn(devicesApi, 'status').mockResolvedValueOnce({ ...status, tunnel_quality: quality } as never);
    mount();
    const row = (await screen.findByText('Cloudflare 隧道')).closest<HTMLElement>('div[class*="justify-between"]')!;
    await waitFor(() => expect(within(row).getByText('可用')).toBeTruthy());
    // Every later read fails; the sample stays on the page and the clock moves on.
    statusRead.mockRejectedValue(new LostAnswer());
    for (let step = 0; step < 12; step += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    }
    expect(within(row).queryByText('可用')).toBeNull();
    expect(within(row).getByText('未知')).toBeTruthy();
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
    // The instance has binding again and refuses its overview away from the computer.
    vi.spyOn(devicesApi, 'rebind').mockRejectedValue(new DevicesApiError(403, 'direct_loopback_required', {}));
    mount('/settings/remote-access/connection', true);
    expect(await screen.findByText(said('重新绑定远程访问要在运行 Avibe 的那台电脑上做'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重新绑定' })).toBeNull();
  });

  it.each([
    ['no such route', () => new DevicesApiError(404, 'not_found', {}), false],
    ['no such route, seen from afar', () => new DevicesApiError(404, 'not_found', {}), true],
    ['an answer that is not the instance\'s JSON', () => new LostAnswer(), false],
    ['the tunnel\'s own error, seen from afar', () => new DevicesApiError(502, 'http_502', {}), true],
    ['the role gate in front of the route', () => new DevicesApiError(403, 'instance_access_forbidden', {}), true],
  ] as const)('shows no bind-again row and says nothing on an instance without it (%s)', async (_name, refusal, away) => {
    vi.spyOn(devicesApi, 'rebind').mockRejectedValue(refusal());
    // Opened at the row's own anchor, as the Devices page's notice would.
    mount('/settings/remote-access/connection#rebind', away);
    expect(await screen.findByText('域名')).toBeTruthy();
    await waitFor(() => expect(devicesApi.rebind).toHaveBeenCalled());
    expect(screen.queryByText('重新绑定远程访问')).toBeNull();
    expect(screen.queryByRole('button', { name: '重新绑定' })).toBeNull();
    expect(document.body.textContent).not.toContain('重新绑定');
    expect(document.body.textContent).not.toContain('vibe remote');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByTestId('where').textContent).toBe('/settings/remote-access/connection');
  });

  it('shows only the domain where this Avibe\'s name cannot be changed here, whatever the reason given', async () => {
    // A reason this page has never heard of (an instance that has not read its name's state yet).
    vi.spyOn(devicesApi, 'address').mockResolvedValue(address({ eligible: false, ineligibleReason: 'address_state_unknown', retired: [] }));
    vi.spyOn(devicesApi, 'rebind').mockRejectedValue(new DevicesApiError(404, 'not_found', {}));
    mount();
    expect(await screen.findByText('域名')).toBeTruthy();
    expect(screen.getByText(host)).toBeTruthy();
    await waitFor(() => expect(devicesApi.address).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: '更改域名' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('button', { name: '清理情况' })).toBeNull();
    expect(document.body.textContent).not.toContain('address_state_unknown');
    expect(document.body.textContent).not.toContain('改名');
  });

  it('opens no rename dialog at the rename\'s own address where the name cannot be changed here', async () => {
    vi.spyOn(devicesApi, 'address').mockResolvedValue(address({ eligible: false, ineligibleReason: 'address_state_unknown' }));
    mount('/settings/remote-access/connection/address');
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/settings/remote-access/connection'));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('textbox', { name: '新名称' })).toBeNull();
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

  it('carries the direct certificate\'s expiry while direct is on, and never the last connection\'s race', async () => {
    vi.spyOn(devicesApi, 'status').mockResolvedValue({ ...status, direct: { enabled: true, state: 'running', reason: '', certificate_not_after: '2099-02-14T00:00:00Z' } } as never);
    const dialog = await open();
    const rows = await within(dialog).findByTestId('direct-details');
    expect(rows.textContent).toContain('直连证书');
    expect(rows.textContent).toContain('有效期至 2099年2月14日');
    // Its two numbers measured different things: 「测速」 replaces it.
    expect(dialog.textContent).not.toContain('最近一次连接');
    // In a browser the shell cannot measure: no 「测速」.
    expect(within(dialog).queryByTestId('direct-measure')).toBeNull();
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

  it('keeps following a change whose answer was lost after its dialog is closed, and shows where it ended', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const newHost = 'maxwell-app.avibe.bot';
    vi.spyOn(devicesApi, 'previewRename').mockResolvedValue({ confirmationId: 'conf-1', hostname: newHost, isReturn: false, holdDays: 30, releaseAt: '2026-12-23T00:00:00.000Z', healthConnected: false, devices: [] });
    vi.spyOn(devicesApi, 'changeAddress').mockRejectedValue(new LostAnswer());
    const read = vi.spyOn(devicesApi, 'address').mockResolvedValue(address());
    mount('/settings/remote-access/connection/address');

    fireEvent.change(await screen.findByRole('textbox', { name: '新名称' }), { target: { value: 'maxwell' } });
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    await screen.findByText(`改成 ${newHost}？`);
    for (let second = 0; second < 6; second += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    }
    fireEvent.click(screen.getByRole('button', { name: '更改域名' }));
    expect(await screen.findByText(said('avibe.bot 还没有答复，更改还在进行'))).toBeTruthy();

    // Closed before any read has seen the change: the row says it, and the page keeps reading.
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByText(`正在把域名改为 ${newHost}…`)).toBeTruthy();
    // It had gone through after all.
    const retired = { hostname: host, releaseAt: '2026-12-23T00:00:00.000Z', devices: [] };
    read.mockResolvedValue(address({ hostname: newHost, retired: [retired] }));
    await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
    expect(await screen.findByText(`域名已改为 ${newHost}`)).toBeTruthy();
    expect(screen.getAllByText(newHost).length).toBeGreaterThan(0);
    // The name given up is kept for this account, until the day the instance reports.
    expect(screen.getByRole('dialog').textContent).toContain(`12月23日之前只有你的账号能拿回 ${host}；之后别人可以领取。`);
  });

  it('says first, under the title, that the old name is kept, with its day or without one', () => {
    const newHost = 'maxwell-app.avibe.bot';
    const retired = { hostname: host, releaseAt: '2026-12-23T00:00:00.000Z', devices: [] };
    const { unmount } = view({ host: newHost, address: address({ hostname: newHost, retired: [retired] }), rename: { step: 'done', hostname: newHost, previous: host, retired } });
    const dated = screen.getByRole('dialog').textContent ?? '';
    const kept = dated.indexOf(`12月23日之前只有你的账号能拿回 ${host}；之后别人可以领取。`);
    expect(kept).toBeGreaterThan(dated.indexOf(`域名已改为 ${newHost}`));
    expect(kept).toBeLessThan(dated.indexOf('设备再打开旧地址时会自己清理'));
    unmount();

    // The instance reports no day (the list could not be read): kept, without a day, in the same place.
    view({ host: newHost, address: address({ hostname: newHost }), rename: { step: 'done', hostname: newHost, previous: host, retired: null } });
    const plain = screen.getByRole('dialog').textContent ?? '';
    const keptPlain = plain.indexOf('放弃的名字会先为你的账号保留一段时间，之后别人可以领取。');
    expect(keptPlain).toBeGreaterThan(-1);
    expect(keptPlain).toBeLessThan(plain.indexOf('设备再打开旧地址时会自己清理'));
  });

  it('shows a device clearing its old-address data while the clean-up dialog is open', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const old = 'studio-app.avibe.bot';
    const kept = (cleanup: 'cleared' | 'unconfirmed') => ({
      hostname: old, releaseAt: '2026-12-21T00:00:00.000Z', devices: [{ id: 'd1', name: 'Chrome', kind: 'browser' as const, cleanup }],
    });
    const read = vi.spyOn(devicesApi, 'address').mockResolvedValue(address({ retired: [kept('unconfirmed')] }));
    mount();

    fireEvent.click(await screen.findByRole('button', { name: '清理情况' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('未确认')).toBeTruthy();
    // The device reports in; the page's regular read brings it, and the open dialog shows it.
    read.mockResolvedValue(address({ retired: [kept('cleared')] }));
    for (let second = 0; second < 31; second += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    }
    expect(await within(dialog).findByText('已清理')).toBeTruthy();
    expect(within(dialog).queryByText('未确认')).toBeNull();
  });

  it('drops the answer to a 「继续」 that was closed or retyped before it arrived', async () => {
    const answers: Array<(value: RenamePreview) => void> = [];
    const asked = vi.spyOn(devicesApi, 'previewRename').mockImplementation(() => new Promise((resolve) => { answers.push(resolve); }));
    const previewOf = (name: string): RenamePreview => ({
      confirmationId: `conf-${name}`, hostname: `${name}-app.avibe.bot`, isReturn: false, holdDays: 30,
      releaseAt: '2026-12-23T00:00:00.000Z', healthConnected: false, devices: [],
    });
    mount('/settings/remote-access/connection/address');

    // Closed while the first answer is on its way: the confirmation does not open on a page
    // that has gone back to the connection settings.
    fireEvent.change(await screen.findByRole('textbox', { name: '新名称' }), { target: { value: 'first' } });
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    await waitFor(() => expect(asked).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/settings/remote-access/connection'));
    await act(async () => { answers[0](previewOf('first')); });
    expect(screen.queryByRole('dialog')).toBeNull();

    // Another name typed while an answer is on its way: the older answer cannot replace it.
    fireEvent.click(screen.getByRole('button', { name: '更改域名' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '新名称' }), { target: { value: 'second' } });
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    await waitFor(() => expect(asked).toHaveBeenCalledTimes(2));
    fireEvent.change(screen.getByRole('textbox', { name: '新名称' }), { target: { value: 'third' } });
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    await waitFor(() => expect(asked).toHaveBeenCalledTimes(3));
    await act(async () => { answers[1](previewOf('second')); });
    expect(screen.queryByText('改成 second-app.avibe.bot？')).toBeNull();
    await act(async () => { answers[2](previewOf('third')); });
    expect(await screen.findByText('改成 third-app.avibe.bot？')).toBeTruthy();
  });

  it('does not bring back the dialog of a change that was confirmed and then closed, and still follows it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const newHost = 'maxwell-app.avibe.bot';
    vi.spyOn(devicesApi, 'previewRename').mockResolvedValue({ confirmationId: 'conf-1', hostname: newHost, isReturn: false, holdDays: 30, releaseAt: '2026-12-23T00:00:00.000Z', healthConnected: false, devices: [] });
    let answer!: (value: { state: 'pending' }) => void;
    vi.spyOn(devicesApi, 'changeAddress').mockReturnValue(new Promise((resolve) => { answer = resolve; }) as never);
    const read = vi.spyOn(devicesApi, 'address').mockResolvedValue(address());
    mount('/settings/remote-access/connection/address');

    fireEvent.change(await screen.findByRole('textbox', { name: '新名称' }), { target: { value: 'maxwell' } });
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    await screen.findByText(`改成 ${newHost}？`);
    for (let second = 0; second < 6; second += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    }
    fireEvent.click(screen.getByRole('button', { name: '更改域名' }));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    // avibe.bot has not answered yet: said in the row, no dialog returns.
    await act(async () => { answer({ state: 'pending' }); });
    expect(await screen.findByText(`正在把域名改为 ${newHost}…`)).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    // It went through: that is said, because the address is a different one now.
    read.mockResolvedValue(address({ hostname: newHost, retired: [{ hostname: host, releaseAt: '2026-12-23T00:00:00.000Z', devices: [] }] }));
    await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
    expect(await screen.findByText(`域名已改为 ${newHost}`)).toBeTruthy();
  });

  it('reopens on a change that is still running, and offers no second one', async () => {
    vi.spyOn(devicesApi, 'address').mockResolvedValue(address({ operation: { state: 'pending', hostname: 'maxwell-app.avibe.bot' } }));
    mount('/settings/remote-access/connection/address');
    expect(await screen.findByText(said('avibe.bot 还没有答复，更改还在进行；这一页会自己更新，不用再点一次。'))).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: '新名称' })).toBeNull();

    // Closing it gives the page back; the change is still said in the domain's row.
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByTestId('where').textContent).toBe('/settings/remote-access/connection');
    expect(screen.getByText('正在把域名改为 maxwell-app.avibe.bot…')).toBeTruthy();
    expect(screen.getByRole('button', { name: '连接诊断与网络路径' })).toBeTruthy();
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
    // The key is retired when it is done, not before; neither the account nor the address is promised.
    expect(dialog.textContent).toContain('完成后，旧的设备密钥作废；这台电脑上的页面不受影响。重新绑定后，已登录的设备会自动重新连上。');
    expect(dialog.textContent).toContain('地址通常不变；如果变了，完成后这里会显示新地址');
    expect(dialog.textContent).not.toContain('立即作废');
    expect(start).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: '重新绑定' }));
    await waitFor(() => expect(fields).toEqual({ user_code: 'CODE', redirect_state: 'STATE' }));
  });

  it('lets a bind-again that was left half-way at avibe.bot be cancelled from the row', async () => {
    writeClaimMarker({ kind: 'rebind', id: 'rebind-1', at: Date.now(), away: true });
    const follow = vi.spyOn(devicesApi, 'rebindClaim').mockResolvedValue({ state: 'pending' });
    const cancel = vi.spyOn(devicesApi, 'cancelRebind').mockImplementation(async () => { follow.mockResolvedValue({ state: 'cancelling' }); });
    mount();

    expect(await screen.findByText(said('正在等待 avibe.bot 上的确认…'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重新绑定' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => expect(cancel).toHaveBeenCalledWith('rebind-1'));
    // Cancelled: said once, and the row offers binding again.
    expect(await screen.findByText(said('已取消，什么都没有绑定。'), {}, { timeout: 4000 })).toBeTruthy();
    expect(await screen.findByRole('button', { name: '重新绑定' })).toBeTruthy();
    expect(readClaimMarker()).toBeNull();
  });

  it('ends the trip when the browser\'s Back restores this page as it was left', async () => {
    mount();
    await screen.findByRole('button', { name: '重新绑定' });
    // The page left for avibe.bot, and Back brings it out of the back-forward cache: nothing mounts.
    writeClaimMarker({ kind: 'rebind', id: 'rebind-1', at: Date.now(), away: true });
    window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: false }));
    expect(readClaimMarker()?.away).toBe(true);
    window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true }));
    // The attempt is kept; only the trip is over, so 「返回设备」 opens Devices.
    expect(readClaimMarker()).toMatchObject({ kind: 'rebind', id: 'rebind-1' });
    expect(readClaimMarker()?.away).toBeUndefined();
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

  it('points to the terminal command for a binding record this page cannot handle', async () => {
    vi.spyOn(devicesApi, 'rebind').mockRejectedValue(new DevicesApiError(409, 'local_binding_unavailable', {}));
    mount();
    expect(await screen.findByText(said('这台电脑的绑定记录有问题，这一页处理不了。请在这台电脑的终端运行 vibe remote rebind。'))).toBeTruthy();
    expect(screen.queryByText(said('现在读不到这台电脑的绑定状态。'))).toBeNull();
    expect(screen.queryByRole('button', { name: '重新绑定' })).toBeNull();
  });

  it('says why a bind-again that came back refused did not happen, and lets it be tried again', async () => {
    writeClaimMarker({ kind: 'rebind', id: 'rebind-1', at: Date.now() });
    const follow = vi.spyOn(devicesApi, 'rebindClaim').mockResolvedValue({ state: 'pending' });
    const cancel = vi.spyOn(devicesApi, 'cancelRebind').mockResolvedValue();
    mount('/settings/remote-access/connection?claim_error=invalid_return');

    expect(await screen.findByText(said('没有重新绑定：从 avibe.bot 回来的这一步没有通过验证，什么都没有改变。请再试一次。'))).toBeTruthy();
    await waitFor(() => expect(cancel).toHaveBeenCalledWith('rebind-1'));
    expect(follow).not.toHaveBeenCalled();
    expect(readClaimMarker()).toBeNull();
    expect((screen.getByRole('button', { name: '重新绑定' }) as HTMLButtonElement).disabled).toBe(false);
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
    // Stored while no tunnel was running: saved, never said to be in effect.
    expect(line({ setting: 'protocol', state: 'applied', saved: true })).toBe('已保存');
    expect(line({ setting: 'protocol', state: 'restored', reason: 'verification_failed', from: 'auto' })).toBe('没连上，已改回「自动」');
    expect(line({ setting: 'outbound', state: 'restoring', from: '' })).toBe('正在改回「跟随系统」');
    view({ connection: connection({ change: { setting: 'protocol', state: 'verifying' } }) });
    expect(screen.queryByRole('button', { name: /保留/ })).toBeNull();
    expect(screen.getByRole('radio', { name: 'QUIC' }).hasAttribute('disabled')).toBe(true);
  });

  it('says more than one binding record at avibe.bot in its own sentence, and points to binding again only where that exists', () => {
    const refused = view({ address: address(), rename: { step: 'form', name: 'maxwell', refusal: { code: 'rebind_required' } }, rebind: { state: 'ready' } });
    expect(screen.getByText(said('没有改：avibe.bot 上这台电脑有不止一条绑定记录，avibe.bot 选不出该用哪一条。先重新绑定远程访问，再改一次。'))).toBeTruthy();
    refused.unmount();

    // Found while reading the name's state: said in the domain's row, beside the way out.
    const read = view({ address: address({ eligible: false, stateError: 'rebind_required' }), rebind: { state: 'ready' } });
    expect(screen.getByText(said('avibe.bot 上这台电脑有不止一条绑定记录，所以这里的域名信息可能不是最新的。先重新绑定远程访问。'))).toBeTruthy();
    read.unmount();
    // Without binding again on this instance, nothing sends anyone there.
    const without = view({ address: address({ eligible: false, stateError: 'rebind_required' }) });
    expect(document.body.textContent).not.toContain('重新绑定');
    without.unmount();

    // The three refusals whose way out is binding again say only their reason there.
    for (const [code, reason] of [
      ['pairing_invalid', '没有改：avibe.bot 不再接受这台电脑的绑定。'],
      ['remote_access_configuration_mismatch', '没有改：avibe.bot 上的记录和这台电脑的远程访问配置对不上。'],
      ['rebind_required', '没有改：avibe.bot 上这台电脑有不止一条绑定记录，avibe.bot 选不出该用哪一条。'],
    ] as const) {
      const bare = view({ address: address(), rename: { step: 'form', name: 'maxwell', refusal: { code } } });
      expect(screen.getByText(said(reason))).toBeTruthy();
      expect(document.body.textContent).not.toContain('重新绑定');
      bare.unmount();
      // Found again when the page is reopened: the same, in the domain's row.
      const reopened = view({ address: address({ operation: { state: 'failed', hostname: 'maxwell-app.avibe.bot', error: { code } } }) });
      expect(document.body.textContent).toContain(reason.replace('没有改：', ''));
      expect(document.body.textContent).not.toContain('重新绑定');
      reopened.unmount();
    }
  });

  it('marks the path this device really uses, and never calls a relayed one direct', () => {
    const inUse = () => Array.from(document.querySelectorAll('[role="switch"]'))
      .filter((each) => each.parentElement?.textContent?.includes('使用中'))
      .map((each) => each.getAttribute('aria-label'));
    const app = { remote: true, via: 'direct' as const, preference: { mode: 'automatic' as const, reverted: null } };
    const relay = { enabled: true, connected: true };

    // The phone App on the relay: the stream came in by the direct ingress, the path is the relay.
    const relayed = view({ ...app, path: { kind: 'relay', rttMs: 85 }, connection: connection({ relay }) });
    expect(inUse()).toEqual(['中继']);
    relayed.unmount();
    // The same where this instance shows no relay row: nothing is marked.
    const noRow = view({ ...app, path: { kind: 'relay', rttMs: 85 } });
    expect(document.body.textContent).not.toContain('使用中');
    noRow.unmount();
    // A direct path, of either kind, marks 「直连」.
    const punched = view({ ...app, path: { kind: 'punched', family: 'ipv6', rttMs: 38 }, connection: connection({ relay }) });
    expect(inUse()).toEqual(['直连']);
    punched.unmount();
    // The App names no path yet (or never does): it may be the relay, so nothing is marked.
    const unnamed = view({ ...app, path: null, connection: connection({ relay }) });
    expect(document.body.textContent).not.toContain('使用中');
    unnamed.unmount();
    // A browser has only its stream, which is the tunnel.
    view({ remote: true, via: 'tunnel' });
    expect(screen.getByText('使用中')).toBeTruthy();
    expect(inUse()).toEqual([]);
  });

  it('shows the certificate without a date when it is expired, and no last-connection race', () => {
    view({ diagnostics: { status: null, diagnostics: null, now: Date.now(), direct: { certificate: null } } });
    const rows = screen.getByTestId('direct-details');
    expect(rows.textContent).toContain('没有有效的证书');
    expect(rows.textContent).not.toContain('最近一次连接');
  });

  it('shows nothing measured until 「测速」 has run, then the median and worst per path with its route', () => {
    const onMeasure = vi.fn();
    const { unmount } = view({ onMeasure, diagnostics: { status: null, diagnostics: null, now: Date.now(), measure: {} } });
    const section = screen.getByTestId('direct-measure');
    expect(section.querySelector('dl')).toBeNull();
    fireEvent.click(within(section).getByRole('button', { name: /测速/ }));
    expect(onMeasure).toHaveBeenCalled();
    unmount();

    const leg = (extra: object) => ({ scope: null, relayRegion: null, ok: 5, medianMs: 38, maxMs: 52, error: null, ...extra });
    view({ diagnostics: { status: null, diagnostics: null, now: Date.now(), measure: { result: {
      measuredAt: Date.now(), samples: 5,
      current: leg({ kind: 'relay', relayRegion: 'euw1', medianMs: 85, maxMs: 140 }) as never,
      tunnel: leg({ kind: 'tunnel', ok: 3, medianMs: 212, maxMs: 1430, error: 'timeout' }) as never,
    } } } });
    const rows = screen.getByTestId('direct-measure').textContent;
    expect(rows).toContain('中继 · euw1');
    expect(rows).toContain('中位 85 ms · 最慢 140 ms');
    expect(rows).toContain('中位 212 ms · 最慢 1.4 s（5 次中 3 次完成）');
    expect(rows).toMatch(/今天 \d{2}:\d{2} 测得/);
  });

  it('says a path not in use, one that never completed, and a measurement already running', () => {
    view({ diagnostics: { status: null, diagnostics: null, now: Date.now(), measure: { error: 'busy', result: {
      measuredAt: Date.now(), samples: 5, current: null,
      tunnel: { kind: 'tunnel', scope: null, relayRegion: null, ok: 0, medianMs: null, maxMs: null, error: 'failed' },
    } } } });
    const rows = screen.getByTestId('direct-measure').textContent;
    expect(rows).toContain('直连或中继现在没有在用');
    expect(rows).toContain('没有完成：连接失败');
    expect(rows).toContain('另一次测速还在进行，请稍后再试。');
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

  it('offers this device 「自动」 and 「仅 Cloudflare 隧道」 only where 「仅直连和中继」 is not offered, and never a number of seconds', () => {
    view({ remote: true, preference: { mode: 'tunnel', reverted: { at: '2026-11-21T01:00:00.000Z', reason: 'unreachable', from: 'tunnel' } } });
    const choice = screen.getByRole('radiogroup', { name: '这台设备' });
    expect(within(choice).getAllByRole('radio').map((each) => each.textContent?.slice(0, 2))).toEqual(['自动', '仅 ']);
    expect(document.body.textContent).not.toContain('仅直连和中继');
    expect(document.body.textContent).not.toMatch(/\d+\s*秒/);
  });

  it('offers 「仅直连和中继」 third where it is offered, and keeps it listed while it is the choice', () => {
    const { unmount } = view({ remote: true, directOnly: true, preference: { mode: 'automatic', reverted: null } });
    const choice = screen.getByRole('radiogroup', { name: '这台设备' });
    const radios = within(choice).getAllByRole('radio');
    expect(radios.map((each) => each.textContent?.slice(0, 2))).toEqual(['自动', '仅 ', '仅直']);
    expect(radios[2].textContent).toContain('建立连接时经过一次 Cloudflare 隧道，之后只走直连和中继。');
    unmount();

    // Chosen, then direct turned off on the instance: the choice made is still the one shown.
    view({ remote: true, directOnly: false, preference: { mode: 'direct', reverted: null } });
    expect(screen.getByRole('radio', { name: /^仅直连和中继/ }).getAttribute('aria-checked')).toBe('true');
  });

  it('says under 「仅直连和中继」 when the App takes it only after it is opened again', () => {
    const { unmount } = view({ remote: true, directOnly: true, preference: { mode: 'direct', reverted: null, pendingRelaunch: true } });
    const option = screen.getByRole('radio', { name: /^仅直连和中继/ });
    expect(option.textContent).toContain('重新打开 App 后生效。');
    unmount();
    const unset = view({ remote: true, directOnly: true, preference: { mode: 'direct', reverted: null } });
    expect(document.body.textContent).not.toContain('重新打开 App 后生效');
    unset.unmount();
    // Never under the option while another one is chosen, whatever the shell sends.
    view({ remote: true, directOnly: true, preference: { mode: 'automatic', reverted: null, pendingRelaunch: true } });
    expect(document.body.textContent).not.toContain('重新打开 App 后生效');
  });

  it('says which choice went back to automatic, and by which shell', () => {
    const at = '2026-11-21T01:00:00.000Z';
    const { unmount } = view({ remote: true, directOnly: true, preference: { mode: 'automatic', reverted: { at, reason: 'unreachable', from: 'direct' } } });
    expect(document.body.textContent).toMatch(/「仅直连和中继」.*连不上，App 已自己改回「自动」。/);
    unmount();

    view({ remote: true, directOnly: true, choiceShell: 'desktop', preference: { mode: 'automatic', reverted: { at, reason: 'unreachable', from: 'direct' } } });
    expect(document.body.textContent).toMatch(/「仅直连和中继」.*连不上，Avibe Desktop 已自己改回「自动」。/);
    expect(document.body.textContent).toContain('选「仅直连和中继」后连不上，Avibe Desktop 会自己改回「自动」。');
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
