/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import i18n from '../../i18n';
import { DevicesPage } from './DevicesPage';
import { said } from './testText';

// The Devices page against HTTP answers shaped as the network really gives them, not against a
// faked page API: a remote browser reaches the instance through its tunnel, and once that tunnel is
// stopped Cloudflare's edge answers in its own HTML, with a gateway status. A fake one layer above
// this (a dropped connection) is what let the page call a successful turn-off a failure.
// ui/design: settings/devices/off-from-afar*

vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
const fetched = vi.hoisted(() => vi.fn());
vi.mock('@/lib/apiFetch', () => ({ apiFetch: fetched }));

const host = 'max-app.avibe.bot';
const json = (status: number, body: unknown) => ({ ok: status >= 200 && status < 300, status, json: async () => body }) as unknown as Response;
/** Cloudflare's own error page: not the instance's JSON. */
const edge = (status: number) => ({
  ok: false, status, json: async () => { throw new SyntaxError('Unexpected token < in JSON at position 0'); },
}) as unknown as Response;

const running = { ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}` };
const listed = {
  mine: [
    { id: 'host', kind: 'runtime', group: 'mine', display_name: 'MacBook', functions: [] },
    { id: 'chrome', kind: 'browser', group: 'mine', display_name: 'Chrome · macOS', current: true, functions: [], current_path: 'tunnel' },
  ],
  others: [], unattributed: [], recent_sign_outs: [], bulk_action: 'other_sessions',
};

/** The instance, until its tunnel is stopped; after that only the edge answers. */
let tunnelUp = true;
function answer(path: string, init?: RequestInit): Response {
  if (!tunnelUp) return edge(530);
  const method = init?.method ?? 'GET';
  if (path === '/api/remote-access/stop' && method === 'POST') {
    // The instance stops the connector, then answers through it: the answer never reaches us.
    tunnelUp = false;
    return edge(502);
  }
  if (path === '/api/remote-access/status') return json(200, running);
  if (path === '/api/devices') return json(200, listed);
  if (path.startsWith('/api/devices/activity')) return json(200, { items: [], next_cursor: null });
  if (path === '/api/instance-address') return json(200, { hostname: host, eligible: false, cleanup: [] });
  return json(404, { error: 'not_found' });
}

beforeEach(async () => {
  await i18n.changeLanguage('zh');
  window.sessionStorage.clear();
  tunnelUp = true;
  fetched.mockImplementation(async (path: string, init?: RequestInit) => answer(path, init));
});
afterEach(() => {
  cleanup();
  fetched.mockReset();
});

function mount() {
  const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
  return render(
    <InstanceAuthorizationContext.Provider value={{ remote: true, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
      <MemoryRouter initialEntries={['/settings/remote-access']}>
        <Routes><Route path="/settings/remote-access" element={<DevicesPage />} /></Routes>
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

describe('DevicesPage · through a tunnel that is stopped', () => {
  it('says turning remote access off from afar as it is when the edge answers instead of the instance', async () => {
    mount();
    fireEvent.click(await screen.findByRole('switch', { name: '远程访问' }));
    fireEvent.click(await screen.findByRole('button', { name: '关闭远程访问' }));

    expect(await screen.findByText(said('没有收到回答，这正是关闭成功时的样子'))).toBeTruthy();
    expect(screen.queryByText(said('没有完成，请再试一次。'))).toBeNull();
    expect(screen.queryByText(said('现在读不到远程访问的状态。'))).toBeNull();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  });

  it('still says a refusal the instance itself gave', async () => {
    fetched.mockImplementation(async (path: string, init?: RequestInit) => (
      path === '/api/remote-access/stop' && init?.method === 'POST'
        ? json(400, { ok: false, error: 'remote_access_request_withdraw_failed' })
        : answer(path, init)
    ));
    mount();
    fireEvent.click(await screen.findByRole('switch', { name: '远程访问' }));
    fireEvent.click(await screen.findByRole('button', { name: '关闭远程访问' }));

    expect(await screen.findByText(said('没有完成，请再试一次。'))).toBeTruthy();
    expect(screen.queryByText(said('这正是关闭成功时的样子'))).toBeNull();
  });
});
