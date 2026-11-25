/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import i18n from '../../i18n';
import { DevicesApiError, devicesApi } from './api';
import { ConnectionPage } from './ConnectionPage';

// 「更改域名」 as this release ships it: offered where the instance says the name is eligible.

const host = 'max-app.avibe.bot';

function Where() {
  const location = useLocation();
  return <div data-testid="where">{location.pathname}</div>;
}

function mount(path = '/settings/remote-access/connection') {
  const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
  return render(
    <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/settings/remote-access/connection" element={<><ConnectionPage /><Where /></>} />
          <Route path="/settings/remote-access/connection/address" element={<><ConnectionPage /><Where /></>} />
        </Routes>
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

beforeEach(async () => {
  await i18n.changeLanguage('zh');
  vi.spyOn(devicesApi, 'interfaces').mockResolvedValue([]);
  vi.spyOn(devicesApi, 'connection').mockResolvedValue({
    settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' },
    interfaces: [], direct: 'ready',
  });
  vi.spyOn(devicesApi, 'status').mockResolvedValue({ ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}` } as never);
  // The instance says the name may be changed here.
  vi.spyOn(devicesApi, 'address').mockResolvedValue({ hostname: host, eligible: true, holdDays: 30, retired: [], healthConnected: false });
  vi.spyOn(devicesApi, 'rebind').mockRejectedValue(new DevicesApiError(404, 'not_found', {}));
  vi.spyOn(devicesApi, 'devices').mockResolvedValue({ devices: [], unlinkedHealth: [], signedOut: [], bulk: null });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('ConnectionPage · 「更改域名」 in this release', () => {
  it('is offered where the instance says the name is eligible, and opens its dialog', async () => {
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '更改域名' }));
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/settings/remote-access/connection/address'));
    expect(await screen.findByRole('textbox', { name: '新名称' })).toBeTruthy();
  });

  it('opens its dialog at the rename\'s own address', async () => {
    mount('/settings/remote-access/connection/address');
    expect(await screen.findByRole('textbox', { name: '新名称' })).toBeTruthy();
    expect(screen.getByTestId('where').textContent).toBe('/settings/remote-access/connection/address');
  });

  it('is not offered where the name is not eligible, and its address goes back to the connection page', async () => {
    vi.spyOn(devicesApi, 'address').mockResolvedValue({ hostname: host, eligible: false, retired: [], healthConnected: false });
    mount('/settings/remote-access/connection/address');
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/settings/remote-access/connection'));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('button', { name: '更改域名' })).toBeNull();
  });
});
