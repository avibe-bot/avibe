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

/** Cloudflare's edge in a request's place, as captured from a real turn-off: problem details in JSON. */
const edgeJson = (status: number) => json(status, {
  type: 'https://developers.cloudflare.com/support/troubleshooting/http-status-codes/', title: 'Bad gateway', status,
  detail: 'The origin did not answer.', instance: 'masked', error_code: 1033, error_name: 'masked', error_category: 'masked',
});
const offline = () => { throw new TypeError('Failed to fetch'); };

/** The instance stops; its stop answers with `stopAnswer`; every later request gets `gone`. */
function closing(stopAnswer: () => Response, gone: () => Response) {
  fetched.mockImplementation(async (path: string, init?: RequestInit) => {
    if (!tunnelUp) return gone();
    if (path === '/api/remote-access/stop' && init?.method === 'POST') {
      tunnelUp = false;
      return stopAnswer();
    }
    return answer(path, init);
  });
}

const turnOff = async () => {
  mount();
  fireEvent.click(await screen.findByRole('switch', { name: '远程访问' }));
  fireEvent.click(await screen.findByRole('button', { name: '关闭远程访问' }));
};
const offFromAfar = said('没有收到回答，这正是关闭成功时的样子');
const closedFromAfar = said('远程访问已关闭。这台设备现在连不上这个 Avibe 了');

describe('DevicesPage · turning remote access off from a remote device', () => {
  // Which screen it lands on is decided by what the page did and saw after, not by parsing bodies.
  it.each([
    ['the edge answers in JSON (502, then 530), as captured', () => edgeJson(502), () => edgeJson(530)],
    ['the edge answers in HTML', () => edge(502), () => edge(530)],
  ] as const)('lands on the turned-off screen when the state cannot be read after: %s', async (_name, stop, gone) => {
    closing(stop, gone);
    await turnOff();
    expect(await screen.findByText(offFromAfar)).toBeTruthy();
    expect(screen.queryByText(said('没有完成，请再试一次。'))).toBeNull();
    expect(screen.queryByText(said('现在读不到远程访问的状态。'))).toBeNull();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  });

  it('lands there too when the network itself fails', async () => {
    closing(offline, offline);
    await turnOff();
    expect(await screen.findByText(offFromAfar)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  });

  it('says it is off where the stop said done and nothing can be read after', async () => {
    closing(() => json(200, { ...running, running: false, ok: true, stopped: true }), () => edgeJson(530));
    await turnOff();
    expect(await screen.findByText(closedFromAfar)).toBeTruthy();
    expect(screen.queryByText(offFromAfar)).toBeNull();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  });

  it('takes the instance\'s refusal at its word only when the state is read after and still on', async () => {
    fetched.mockImplementation(async (path: string, init?: RequestInit) => (
      path === '/api/remote-access/stop' && init?.method === 'POST'
        ? json(400, { ...running, ok: false, error: 'cloudflared_stop_failed' })
        : answer(path, init)
    ));
    await turnOff();
    expect(await screen.findByText(said('没有完成，请再试一次。'))).toBeTruthy();
    expect(screen.queryByText(offFromAfar)).toBeNull();
    expect(screen.getByRole('switch', { name: '远程访问' }).getAttribute('aria-checked')).toBe('true');
  });

  it('says nothing is known where nothing came back and the state is read still on', async () => {
    fetched.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/api/remote-access/stop' && init?.method === 'POST') throw new TypeError('Failed to fetch');
      return answer(path, init);
    });
    await turnOff();
    expect(await screen.findByText(said('没有收到结果，不确定有没有完成。'))).toBeTruthy();
    expect(screen.queryByText(offFromAfar)).toBeNull();
  });
});
