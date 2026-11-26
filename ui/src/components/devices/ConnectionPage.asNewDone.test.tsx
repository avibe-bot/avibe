/** @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import i18n from '../../i18n';
import { devicesApi } from './api';
import { writeClaimMarker } from './claimFlow';
import { ConnectionPage } from './ConnectionPage';
import { DevicesPage } from './DevicesPage';
import { said } from './testText';
import type { DevicesSnapshot } from './wire';

// Binding as a new computer, deleting first, ends on the Devices page. Whose name is kept there
// follows whose Avibe it was: another account's says 「为原来的账号」, never 「在你的账号下」.
// ui/design: settings/devices-unbind/done-original-account*, done*

vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));

const host = 'max-app.avibe.bot';
const empty: DevicesSnapshot = { devices: [], unlinkedHealth: [], signedOut: [], bulk: null };

function mount() {
  const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
  return render(
    <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
      <MemoryRouter initialEntries={['/settings/remote-access/connection']}>
        <Routes>
          <Route path="/settings/remote-access/connection" element={<ConnectionPage />} />
          <Route path="/settings/remote-access" element={<DevicesPage />} />
        </Routes>
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

let devices: ReturnType<typeof vi.spyOn>;
beforeEach(async () => {
  await i18n.changeLanguage('zh');
  window.sessionStorage.clear();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.spyOn(devicesApi, 'interfaces').mockResolvedValue([]);
  vi.spyOn(devicesApi, 'connection').mockResolvedValue({
    settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' },
    interfaces: [], direct: 'ready',
  });
  vi.spyOn(devicesApi, 'status').mockResolvedValue({ ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}` } as never);
  vi.spyOn(devicesApi, 'address').mockResolvedValue({ hostname: host, eligible: true, holdDays: 30, retired: [], healthConnected: false });
  vi.spyOn(devicesApi, 'activity').mockResolvedValue({ items: [], nextCursor: null });
  vi.spyOn(devicesApi, 'rebindOffered').mockResolvedValue(true);
  vi.spyOn(devicesApi, 'rebindClaim').mockResolvedValue({ state: 'error', code: 'installation_unverified', reasonCode: 'other_account' });
  vi.spyOn(devicesApi, 'previewReset').mockResolvedValue({ confirmationId: 'reset-1', healthConnected: false });
  devices = vi.spyOn(devicesApi, 'devices').mockResolvedValue(empty);
  vi.spyOn(devicesApi, 'reset').mockImplementation(async () => {
    devices.mockResolvedValue({ ...empty, unbind: { state: 'unbinding' } });
    return 'unbinding';
  });
  writeClaimMarker({ kind: 'rebind', id: 'rebind-1', at: Date.now() });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function bindAsNewAndFinish(releaseAt?: string) {
  fireEvent.click(await screen.findByRole('button', { name: '作为一台新电脑重新绑定' }));
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '作为一台新电脑重新绑定' }));
  expect(await screen.findByText(said('正在解除绑定，会自动继续。'))).toBeTruthy();
  vi.spyOn(devicesApi, 'status').mockResolvedValue({ ok: true, enabled: false, paired: false, running: false } as never);
  devices.mockResolvedValue({ ...empty, unbind: { state: 'unbound', host, ...(releaseAt ? { releaseAt } : {}) } });
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(await screen.findByText('这台电脑已解除绑定')).toBeTruthy();
}

describe('binding as a new computer, deleting first, done on the Devices page', () => {
  it('of another account\'s Avibe, says the name stays with the original account, until its day', async () => {
    vi.spyOn(devicesApi, 'rebind').mockResolvedValue({ state: 'asNewOffered', reason: 'other_account', hint: 'm***@gmail.com' });
    mount();
    await bindAsNewAndFinish('2026-12-25T00:00:00.000Z');
    expect(screen.getByText(said(`${host} 为原来的账号保留到 12月25日；之后别人可以领取。`))).toBeTruthy();
    expect(document.body.textContent).not.toContain('在你的账号下');
  });

  it('of another account\'s Avibe without a day, says 30 days', async () => {
    vi.spyOn(devicesApi, 'rebind').mockResolvedValue({ state: 'asNewOffered', reason: 'other_account', hint: 'm***@gmail.com' });
    mount();
    await bindAsNewAndFinish();
    expect(screen.getByText(said(`${host} 为原来的账号保留 30 天；之后别人可以领取。`))).toBeTruthy();
  });

  it('of this account\'s own Avibe, says 「在你的账号下」 as unbinding does', async () => {
    vi.spyOn(devicesApi, 'rebind').mockResolvedValue({ state: 'asNewOffered', reason: 'predecessor_unrecoverable' });
    mount();
    await bindAsNewAndFinish('2026-12-25T00:00:00.000Z');
    expect(screen.getByText(said(`${host} 在你的账号下保留到 12月25日，这期间可以在这台或别的电脑上领回。`))).toBeTruthy();
  });
});
