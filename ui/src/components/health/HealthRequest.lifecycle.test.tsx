/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import en from '@/i18n/en.json';
import { type HealthRequest } from '@/lib/healthRequests';
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
let current = request;
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  current = { ...request };
  document.cookie = 'vibe_csrf_token=synthetic-csrf; path=/';
  fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url === `/api/mobile/requests/${request.id}`) return Response.json(current);
    if (url === '/api/mobile/pairings') return Response.json({ pairing_code: 'synthetic-one-time-code', expires_at: '2026-11-18T05:05:00Z' });
    throw new Error(`Unexpected synthetic route: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => { cleanup(); Reflect.deleteProperty(window, 'avibeNative'); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers(); });

const tick = (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const landing = <Routes><Route path="/mobile/health/:requestId" element={<HealthLandingPage />} /></Routes>;
const stalled = (signal?: AbortSignal | null) => new Promise<never>((_, reject) => {
  if (signal?.aborted) reject(signal.reason);
  else signal?.addEventListener('abort', () => reject(signal.reason), { once: true });
});

describe('Health request live ownership and bounded network waits', () => {
  it('requires a successful live read before a creation-time card can pair', async () => {
    vi.useFakeTimers();
    const call = installBridge();
    let resolveRead!: (value: Response) => void;
    fetchMock.mockImplementation(() => new Promise<Response>(resolve => { resolveRead = resolve; }));
    mount(<HealthRequestCard request={request} />);
    const button = screen.getByRole('button', { name: 'Authorize on iPhone' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(call).not.toHaveBeenCalled();
    await act(async () => { resolveRead(Response.json(request)); });
    expect(button.disabled).toBe(false);
  });

  it('gives poll and post-authorization reads one owner and preserves a confirmed terminal result', async () => {
    vi.useFakeTimers(); installBridge();
    let gets = 0;
    let resolveOld!: (value: Response) => void;
    fetchMock.mockImplementation(async (url: string) => {
      if (url === '/api/mobile/pairings') return Response.json({ pairing_code: 'synthetic-one-time-code' });
      gets++;
      if (gets === 1) return Response.json(request);
      // The old polling response deliberately ignores abort, to test ownership too.
      if (gets === 2) return new Promise<Response>(resolve => { resolveOld = resolve; });
      return Response.json({ ...request, state: 'succeeded' });
    });
    mount(landing);
    await tick();
    await tick(3000);
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' }));
    await tick();
    expect(screen.getByText(en.health.note.succeeded)).toBeTruthy();
    await act(async () => { resolveOld(Response.json({ ...request, state: 'claimed' })); });
    await tick(30000);
    expect(screen.getByText(en.health.note.succeeded)).toBeTruthy();
    expect(gets).toBe(3);
  });

  it('serializes the reviewer’s delayed authorization refresh and eventually observes completion', async () => {
    vi.useFakeTimers(); installBridge();
    let gets = 0;
    let resolveOld!: (value: Response) => void;
    fetchMock.mockImplementation(async (url: string) => {
      if (url === '/api/mobile/pairings') return Response.json({ pairing_code: 'synthetic-one-time-code' });
      gets++;
      if (gets === 1) return Response.json(request);
      if (gets === 2) return new Promise<Response>(resolve => { resolveOld = resolve; });
      return Response.json({ ...request, state: 'succeeded' });
    });
    mount(landing); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' }));
    await tick(); await tick(3000);
    expect(gets).toBe(2);
    await act(async () => { resolveOld(Response.json({ ...request, state: 'claimed' })); });
    await tick(3000);
    expect(screen.getByText(en.health.note.succeeded)).toBeTruthy();
    await tick(30000);
    expect(gets).toBe(3);
  });

  it.each(['fetch', 'body'])('bounds a stalled status %s, shows unknown state, and recovers on the next read', async (phase) => {
    vi.useFakeTimers();
    let gets = 0;
    let oldSignal: AbortSignal | null | undefined;
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      gets++;
      if (gets > 1) return Response.json({ ...request, state: 'succeeded' });
      oldSignal = init?.signal;
      if (phase === 'fetch') return stalled(oldSignal);
      const response = Response.json({});
      response.json = () => stalled(oldSignal);
      return response;
    });
    mount(landing); await tick(10000);
    expect(oldSignal?.aborted).toBe(true);
    expect(screen.getByText(en.health.loadError)).toBeTruthy();
    await tick(3000);
    expect(screen.getByText(en.health.note.succeeded)).toBeTruthy();
  });

  it.each(['fetch', 'body'])('bounds pairing %s without replaying the mutation or calling Native', async (phase) => {
    vi.useFakeTimers(); const call = installBridge();
    let pairingSignal: AbortSignal | null | undefined;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url !== '/api/mobile/pairings') return Response.json(request);
      pairingSignal = init?.signal;
      if (phase === 'fetch') return stalled(pairingSignal);
      const response = Response.json({});
      response.json = () => stalled(pairingSignal);
      return response;
    });
    mount(<HealthRequestCard request={request} />); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' }));
    await tick(10000);
    expect(pairingSignal?.aborted).toBe(true);
    expect(screen.getByText(en.health.note.failed)).toBeTruthy();
    expect(call).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings')).toHaveLength(1);
    expect(screen.queryByText(en.health.opening)).toBeNull();
  });

  it('bounds consecutive failed status attempts instead of retrying forever', async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => stalled(init?.signal));
    mount(landing); await tick(120000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(screen.getByText(en.health.loadError)).toBeTruthy();
  });

  it('isolates request identity changes from a late read and immediately closes authorization', async () => {
    vi.useFakeTimers(); installBridge();
    const second = { ...request, id: 'mreq_abcdef0123456789abcdef0123456789' };
    let resolveOld!: (value: Response) => void;
    let resolveNew!: (value: Response) => void;
    fetchMock.mockImplementation((url: string) => new Promise<Response>(resolve => {
      if (url.endsWith(second.id)) resolveNew = resolve;
      else resolveOld = resolve;
    }));
    const renderCard = (value: HealthRequest) => <I18nextProvider i18n={i18n}><MemoryRouter><HealthRequestCard request={value} /></MemoryRouter></I18nextProvider>;
    const view = render(renderCard(request));
    view.rerender(renderCard(second));
    await act(async () => { resolveOld(Response.json({ ...request, state: 'succeeded' })); });
    expect(screen.queryByText(en.health.note.succeeded)).toBeNull();
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { resolveNew(Response.json(second)); });
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(false);
  });

  it('aborts pairing when its route identity leaves and never forwards its late code', async () => {
    vi.useFakeTimers(); const call = installBridge();
    let resolvePairing!: (value: Response) => void;
    let pairingSignal: AbortSignal | null | undefined;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url !== '/api/mobile/pairings') return Response.json(request);
      pairingSignal = init?.signal;
      return new Promise<Response>(resolve => { resolvePairing = resolve; });
    });
    const view = mount(<HealthRequestCard request={request} />); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    view.unmount();
    await act(async () => { resolvePairing(Response.json({ pairing_code: 'late-synthetic-code' })); });
    expect(pairingSignal?.aborted).toBe(true);
    expect(call).not.toHaveBeenCalled();
  });
});
