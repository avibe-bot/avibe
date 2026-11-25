/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import i18n from '../../i18n';
import { devicesApi } from './api';
import { ConnectionPage } from './ConnectionPage';
import type { DirectState } from './types';

// 「仅直连和中继」 on 「连接方式」: offered only where the shell takes it (`directPreference` 2) and
// this instance has direct turned on; otherwise not shown at all.
// ui/design: settings/devices-connection/app-direct-only*

const host = 'max-app.avibe.bot';
let call: ReturnType<typeof vi.fn>;

function app(directPreference: number) {
  call = vi.fn(async (method: string, params?: { mode?: string }) => (
    method === 'direct.setPreference' ? { mode: params?.mode, reverted: null }
      : method === 'direct.preference' ? { mode: 'automatic', reverted: null }
        : method === 'direct.status' ? { preferred: 'tunnel', sequence: 1, closure: 0, path: null }
          : null));
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: { contract: 1, platform: 'ios', shellVersion: '3.2.8', capabilities: { direct: 3, directPreference }, call, on: () => () => undefined },
  });
}

function mount(direct: DirectState) {
  vi.spyOn(devicesApi, 'connection').mockResolvedValue({
    settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' },
    interfaces: [], direct,
  });
  const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
  return render(
    <InstanceAuthorizationContext.Provider value={{ remote: true, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
      <MemoryRouter initialEntries={['/settings/remote-access/connection']}>
        <Routes><Route path="/settings/remote-access/connection" element={<ConnectionPage />} /></Routes>
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

beforeEach(async () => {
  await i18n.changeLanguage('zh');
  vi.spyOn(devicesApi, 'interfaces').mockResolvedValue([]);
  vi.spyOn(devicesApi, 'status').mockResolvedValue({ ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}` } as never);
  vi.spyOn(devicesApi, 'address').mockResolvedValue({ hostname: host, eligible: false, retired: [], healthConnected: false });
  vi.spyOn(devicesApi, 'rebind').mockRejectedValue(new Error('none'));
  vi.spyOn(devicesApi, 'devices').mockResolvedValue({ devices: [], unlinkedHealth: [], signedOut: [], bulk: null });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  Reflect.deleteProperty(window, 'avibeNative');
});

const options = async () => within(await screen.findByRole('radiogroup', { name: '这台设备' })).getAllByRole('radio').map((each) => each.textContent?.slice(0, 4));

describe('ConnectionPage · 「仅直连和中继」', () => {
  it('is offered third where the shell takes it and direct is on, and choosing it asks the shell for direct', async () => {
    app(2);
    mount('ready');
    await waitFor(async () => expect(await options()).toEqual(['自动哪条', '仅 Cl', '仅直连和']));
    fireEvent.click(screen.getByRole('radio', { name: /^仅直连和中继/ }));
    await waitFor(() => expect(call).toHaveBeenCalledWith('direct.setPreference', { mode: 'direct' }));
    await waitFor(() => expect(screen.getByRole('radio', { name: /^仅直连和中继/ }).getAttribute('aria-checked')).toBe('true'));
  });

  it('is not shown where direct is off on the instance', async () => {
    app(2);
    mount('off');
    expect(await options()).toEqual(['自动哪条', '仅 Cl']);
    expect(document.body.textContent).not.toContain('仅直连和中继');
  });

  it('is not shown on a shell that does not take it', async () => {
    app(1);
    mount('ready');
    expect(await options()).toEqual(['自动哪条', '仅 Cl']);
    expect(document.body.textContent).not.toContain('仅直连和中继');
  });
});
