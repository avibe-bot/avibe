/** @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { DENIED_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';
import i18n from '../../i18n';
import { DevicesPage } from './DevicesPage';
import { said } from './testText';

// A pairing request this computer saved and has not settled (#4210), against the instance's HTTP
// answers: `pending_pairing` in the status, and `POST /api/remote-access/vibe-cloud/pair` with an
// empty body for 「继续」. The page never offers a key, nor a new binding in the request's place.
// ui/design: settings/devices/hold-*

vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
const fetched = vi.hoisted(() => vi.fn());
vi.mock('@/lib/apiFetch', () => ({ apiFetch: fetched }));

const host = 'max-app.avibe.bot';
const json = (status: number, body: unknown) => ({ ok: status >= 200 && status < 300, status, json: async () => body }) as unknown as Response;
const unpaired = (pending: Record<string, unknown> | null) => ({ ok: true, enabled: false, paired: false, running: false, pending_pairing: pending });
const bound = { ok: true, enabled: true, paired: true, running: true, public_url: `https://${host}`, pending_pairing: null };
const listed = { mine: [{ id: 'host', kind: 'runtime', group: 'mine', display_name: 'MacBook', functions: [] }], others: [], unattributed: [], recent_sign_outs: [] };

let status: Record<string, unknown>;
let pair: (body: unknown) => Response;
const pairBodies: unknown[] = [];
function answer(path: string, init?: RequestInit): Response {
  const method = init?.method ?? 'GET';
  if (path === '/api/remote-access/vibe-cloud/pair' && method === 'POST') {
    const body = JSON.parse(String(init?.body ?? 'null'));
    pairBodies.push(body);
    return pair(body);
  }
  if (path === '/api/remote-access/status') return json(200, status);
  if (path === '/api/devices') return json(200, listed);
  if (path.startsWith('/api/devices/activity')) return json(200, { items: [], next_cursor: null });
  if (path === '/api/instance-address') return json(200, { hostname: host, eligible: false, cleanup: [] });
  return json(404, { error: 'not_found' });
}

beforeEach(async () => {
  await i18n.changeLanguage('zh');
  window.sessionStorage.clear();
  pairBodies.length = 0;
  pair = () => json(404, { error: 'not_found' });
  fetched.mockImplementation(async (path: string, init?: RequestInit) => answer(path, init));
});
afterEach(() => {
  cleanup();
  fetched.mockReset();
  vi.useRealTimers();
});

function mount() {
  const capabilities = { ...DENIED_INSTANCE_CAPABILITIES, can_manage_instance: true, is_instance_owner: true, can_manage_access_members: true };
  return render(
    <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: 'personal', instanceRole: 'owner', capabilities }}>
      <MemoryRouter initialEntries={['/settings/remote-access']}>
        <Routes><Route path="/settings/remote-access" element={<DevicesPage />} /></Routes>
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

const support = said('这次绑定没法确认是否成功，这台电脑不会再重试。请联系 support@avibe.bot；在那之前不要删除 Avibe 的文件。');
const noButton = () => {
  expect(screen.queryByRole('button', { name: '开启远程访问' })).toBeNull();
  expect(screen.queryByRole('textbox')).toBeNull();
  expect(document.body.textContent).not.toMatch(/key|密钥/i);
};

describe('DevicesPage · a saved pairing request', () => {
  it('cannot be confirmed: one sentence and the address to write to, nothing to press', async () => {
    status = unpaired({ phase: 'unknown', can_resume: false, replay_state: 'needs_reconciliation' });
    mount();

    const line = await screen.findByText(support);
    expect(line.closest('p')?.querySelector('a')?.getAttribute('href')).toBe('mailto:support@avibe.bot');
    noButton();
  });

  it('is sent again by Avibe itself: 「正在完成绑定…」 with no time, followed until it settles', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    status = unpaired({ phase: 'prepared', can_resume: true, replay_state: 'scheduled', next_retry_at: Date.now() / 1000 + 30 });
    mount();

    expect(await screen.findByText('正在完成绑定…')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/\d{1,2}:\d{2}/);
    noButton();

    status = bound;
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000); });
    await waitFor(() => expect(screen.queryByText('正在完成绑定…')).toBeNull());
  });

  it('where pressing it continues the saved request, the page offers only 「开启远程访问」', async () => {
    for (const pending of [
      { phase: 'prepared', can_resume: true, replay_state: 'manual', operation_owner: 'personal_claim' },
      { phase: 'redeemed', can_resume: true, replay_state: 'local_archive', operation_owner: 'cli' },
      { phase: 'prepared', can_resume: false, replay_state: 'terminated', operation_owner: 'personal_claim' },
    ]) {
      status = unpaired(pending);
      const page = mount();
      expect(await screen.findByRole('button', { name: '开启远程访问' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: '继续' })).toBeNull();
      expect(screen.queryByText('正在完成绑定…')).toBeNull();
      page.unmount();
    }
  });

  it('pressing it is starting a claim, whose start continues the saved request: bound already is bound', async () => {
    status = unpaired({ phase: 'redeemed', can_resume: true, replay_state: 'local_archive' });
    const starts: unknown[] = [];
    fetched.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/api/remote-access/claims' && init?.method === 'POST') {
        starts.push(JSON.parse(String(init.body)));
        status = bound;
        return json(409, { ok: false, error: 'already_bound' });
      }
      return answer(path, init);
    });
    mount();

    fireEvent.click(await screen.findByRole('button', { name: '开启远程访问' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '开启远程访问' })).toBeNull());
    expect(starts).toHaveLength(1);
    expect(pairBodies).toEqual([]);
  });

  it('another step on this computer kept the press from being taken: the page\'s own 「没有完成，请再试一次」', async () => {
    status = unpaired({ phase: 'prepared', can_resume: true, replay_state: 'manual' });
    fetched.mockImplementation(async (path: string, init?: RequestInit) => (
      path === '/api/remote-access/claims' && init?.method === 'POST'
        ? json(409, { ok: false, error: 'pairing_local_busy', accepted: false })
        : answer(path, init)));
    mount();

    fireEvent.click(await screen.findByRole('button', { name: '开启远程访问' }));
    expect(await screen.findByText(said('没有完成，请再试一次。'))).toBeTruthy();
    expect((screen.getByRole('button', { name: '开启远程访问' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('Avibe Desktop\'s own is finished there, whatever its state; the terminal\'s cannot be confirmed', async () => {
    status = unpaired({ phase: 'prepared', can_resume: false, replay_state: 'terminated', operation_owner: 'desktop_app' });
    const desktop = mount();
    expect(await screen.findByText(said('这次绑定在 Avibe Desktop 里完成。'))).toBeTruthy();
    noButton();
    desktop.unmount();

    // Desktop's request still being sent: the owner is named on every record.
    status = unpaired({ phase: 'prepared', can_resume: true, replay_state: 'scheduled', next_retry_at: Date.now() / 1000 + 30, operation_owner: 'desktop_app' });
    const sending = mount();
    expect(await screen.findByText(said('这次绑定在 Avibe Desktop 里完成。'))).toBeTruthy();
    expect(screen.queryByText('正在完成绑定…')).toBeNull();
    noButton();
    sending.unmount();

    status = unpaired({ phase: 'prepared', can_resume: false, replay_state: 'terminated', operation_owner: 'cli' });
    mount();
    expect(await screen.findByText(support)).toBeTruthy();
    noButton();
  });

  it('a claim refused because the request cannot be confirmed says the same, with nothing to press', async () => {
    status = unpaired(null);
    fetched.mockImplementation(async (path: string, init?: RequestInit) => (
      path === '/api/remote-access/claims' && init?.method === 'POST'
        ? json(409, { ok: false, error: 'pairing_needs_reconciliation' })
        : answer(path, init)));
    mount();

    fireEvent.click(await screen.findByRole('button', { name: '开启远程访问' }));
    expect(await screen.findByText(support)).toBeTruthy();
    noButton();
  });

  it('a state this page does not know leaves the page as it was', async () => {
    status = unpaired({ phase: 'prepared', can_resume: false, replay_state: 'something_newer' });
    mount();
    expect(await screen.findByRole('button', { name: '开启远程访问' })).toBeTruthy();
  });
});

describe('the shared error copy where a pairing key can still be typed', () => {
  it('names a used key and the two new refusals in both languages', async () => {
    for (const language of ['zh', 'en']) {
      await i18n.changeLanguage(language);
      for (const code of ['pairing_key_used', 'pairing_needs_reconciliation', 'pairing_local_busy']) {
        expect(i18n.exists(`errors.${code}`, { lng: language, fallbackLng: [] })).toBe(true);
      }
    }
  });
});
