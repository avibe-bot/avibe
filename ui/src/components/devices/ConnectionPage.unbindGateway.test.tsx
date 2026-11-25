/** @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import i18n from '../../i18n';
import { ConnectionPage } from './ConnectionPage';
import { said } from './testText';

// 「解除绑定」 pressed from a remote device, against HTTP answers shaped as a real run gave them: the
// instance stops the tunnel before its 202 can travel back, so the commit gets Cloudflare's edge in
// the instance's place (502 with an empty body, or 530, or an HTML page, or the edge's own JSON),
// and every later request too. That is an answer lost, never a failure.
// ui/design: settings/devices-unbind/unbinding

vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
const fetched = vi.hoisted(() => vi.fn());
vi.mock('@/lib/apiFetch', () => ({ apiFetch: fetched }));

const host = 'max-app.avibe.bot';
const json = (status: number, body: unknown) => ({ ok: status >= 200 && status < 300, status, json: async () => body }) as unknown as Response;
/** No JSON at all: an empty body or an HTML page. */
const notJson = (status: number) => ({ ok: false, status, json: async () => { throw new SyntaxError('Unexpected end of JSON input'); } }) as unknown as Response;
/** Cloudflare's problem details, when the request asked for JSON. */
const edgeJson = (status: number) => json(status, {
  type: 'https://developers.cloudflare.com/support/troubleshooting/http-status-codes/', title: 'Bad gateway', status,
  detail: 'The origin did not answer.', instance: 'masked', error_code: 1033, error_name: 'masked', error_category: 'masked',
});

const connection = {
  settings: { transport_protocol: 'auto', auto_recovery: true, optimization_profile: 'balanced', edge_ip_version: 'auto', edge_bind_address: '' },
  direct: { state: 'ready' },
};
let tunnelUp = true;
let commitAnswer: () => Response;
let gone: () => Response;
function answer(path: string, init?: RequestInit): Response {
  const method = init?.method ?? 'GET';
  if (!tunnelUp) return gone();
  if (path === '/api/remote-access/unbind/preview' && method === 'POST') return json(200, { confirmation_id: 'unbind-1', instance_id: 'i-1' });
  if (path === '/api/remote-access/unbind/commit' && method === 'POST') {
    tunnelUp = false;
    return commitAnswer();
  }
  if (path === '/api/remote-access/status') return json(200, { ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}` });
  if (path === '/api/devices') return json(200, { mine: [{ id: 'host', kind: 'runtime', group: 'mine', display_name: 'MacBook', functions: [] }], others: [], unattributed: [], recent_sign_outs: [] });
  if (path === '/api/devices/connection') return json(200, connection);
  if (path === '/api/remote-access/network-interfaces') return json(200, { interfaces: [] });
  if (path === '/api/instance-address') return json(200, { hostname: host, eligible: true, hold_days: 30, retired: [] });
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
  vi.useRealTimers();
});

function mount() {
  const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true };
  return render(
    <InstanceAuthorizationContext.Provider value={{ remote: true, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
      <MemoryRouter initialEntries={['/settings/remote-access/connection']}>
        <Routes><Route path="/settings/remote-access/connection" element={<ConnectionPage />} /></Routes>
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

describe('ConnectionPage · 「解除绑定」 from a remote device, as the edge answers', () => {
  it.each([
    ['502 with an empty body, then 530', () => notJson(502), () => notJson(530)],
    ['502 and 530 as HTML pages', () => notJson(502), () => notJson(530)],
    ['the edge\'s own JSON (502, then 530)', () => edgeJson(502), () => edgeJson(530)],
  ] as const)('shows it running, never failed, and stays so while nothing can be read: %s', async (_name, commit, after) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    commitAnswer = commit;
    gone = after;
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '解除绑定' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '解除绑定' }));

    expect(await screen.findByText(said('正在解除绑定，会自动继续。'))).toBeTruthy();
    expect(screen.queryByText(said('没有完成，请再试一次。'))).toBeNull();
    // Every read after it gets the edge: the page keeps saying it runs.
    await act(async () => { await vi.advanceTimersByTimeAsync(40_000); });
    expect(screen.getByText(said('正在解除绑定，会自动继续。'))).toBeTruthy();
    expect(screen.queryByText(said('没有完成，请再试一次。'))).toBeNull();
    expect(screen.queryByRole('button', { name: '解除绑定' })).toBeNull();
  });
});
