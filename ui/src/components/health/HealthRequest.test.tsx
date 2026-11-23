/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import en from '@/i18n/en.json';
import { healthRequestView, type HealthRequest } from '@/lib/healthRequests';
import HealthLandingPage, { HealthRequestCard } from './HealthRequest';

const request: HealthRequest = {
  id: 'mreq_0123456789abcdef0123456789abcdef', operation: 'health.connect', state: 'queued',
  created_at: '2026-11-18T05:00:00Z', expires_at: '2026-11-19T05:00:00Z', requires_user: true,
  metrics: ['steps', 'activity', 'workouts', 'sleep'], error: null,
};
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const mount = (node: React.ReactNode) => render(<I18nextProvider i18n={i18n}><MemoryRouter initialEntries={[`/mobile/health/${request.id}`]}>{node}</MemoryRouter></I18nextProvider>);
const installBridge = (capabilities: Record<string, number> = { health: 1 }) => {
  const call = vi.fn().mockResolvedValue({ state: 'connected' });
  Object.defineProperty(window, 'avibeNative', { configurable: true, value: { contract: 1, platform: 'ios', shellVersion: '1', capabilities, call, on: vi.fn() } });
  return call;
};
let requestNumber = 0;
let current = request;
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  request.id = `mreq_${(++requestNumber).toString(16).padStart(32, '0')}`;
  current = { ...request };
  document.cookie = 'vibe_csrf_token=synthetic-csrf; path=/';
  fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url === `/api/mobile/requests/${request.id}`) return Response.json(current);
    if (url === '/api/mobile/pairings') return Response.json({ pairing_code: 'synthetic-one-time-code', expires_at: '2026-11-18T05:05:00Z' });
    throw new Error(`Unexpected synthetic route: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => { cleanup(); Reflect.deleteProperty(window, 'avibeNative'); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('Health request production consumers (synthetic server and native peer)', () => {
  it('binds pairing to the request with CSRF and keeps connected distinct from sync completion', async () => {
    const call = installBridge();
    mount(<Routes><Route path="/mobile/health/:requestId" element={<HealthLandingPage />} /></Routes>);
    fireEvent.click(await screen.findByRole('button', { name: 'Authorize on iPhone' }));
    await screen.findByRole('heading', { name: 'Connected' });
    expect(call).toHaveBeenCalledWith('health.connect', { requestId: request.id, pairingCode: 'synthetic-one-time-code' });
    const pairing = fetchMock.mock.calls.find(([url]) => url === '/api/mobile/pairings');
    const init = pairing?.[1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({ request_id: request.id });
    expect(new Headers(init.headers).get('X-Vibe-CSRF-Token')).toBe('synthetic-csrf');
    expect(screen.queryByText('This sync is complete.')).toBeNull();
    expect(document.body.textContent).not.toContain('synthetic-one-time-code');
    expect(screen.getByRole('link', { name: 'Open conversations' }).getAttribute('href')).toBe('/inbox');
    current = { ...request, state: 'succeeded' };
    await screen.findByText('This sync is complete.', {}, { timeout: 4500 });
  });

  it('renders live card state from GET rather than the stale message and never pairs refresh requests', async () => {
    const call = installBridge();
    current = { ...request, operation: 'health.refresh', requires_user: false, state: 'claimed' };
    mount(<HealthRequestCard request={{ ...current, state: 'queued' }} />);
    await screen.findByText('In progress. Return to the conversation for the result.');
    expect(screen.queryByRole('button', { name: 'Authorize on iPhone' })).toBeNull();
    expect(call).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.every(([url]) => url.includes('/requests/'))).toBe(true);
  });

  it('requires an iOS health capability, connect operation and explicit user requirement for authorization', () => {
    expect(healthRequestView(request)).toBe('waiting');
    installBridge({ app: 1 });
    expect(healthRequestView(request)).toBe('needsUpdate');
    installBridge();
    expect(healthRequestView(request)).toBe('authorize');
    expect(healthRequestView({ ...request, operation: 'health.refresh' })).toBe('waiting');
    expect(healthRequestView({ ...request, requires_user: false })).toBe('waiting');
  });

  it('keeps server failures and cancellation factual and disables pairing when metadata cannot be refreshed', async () => {
    installBridge();
    fetchMock.mockResolvedValue(Response.json({ code: 'instance_eligibility_unavailable' }, { status: 503 }));
    mount(<HealthRequestCard request={request} />);
    await screen.findByText(en.health.loadError);
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true);
    expect(healthRequestView({ ...request, state: 'failed', error: { code: 'internal' } })).toBe('failed');
    expect(healthRequestView({ ...request, state: 'canceled' })).toBe('canceled');
  });

  it('does not call the bridge when pairing fails', async () => {
    const call = installBridge();
    fetchMock.mockImplementation(async (url: string) => url.includes('/pairings') ? Response.json({ code: 'request_expired' }, { status: 409 }) : Response.json(request));
    mount(<HealthRequestCard request={request} />);
    await waitFor(() => expect((screen.getByRole('button', { name: 'Authorize on iPhone' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' }));
    await screen.findByText(en.health.note.failed);
    expect(call).not.toHaveBeenCalled();
  });
});
