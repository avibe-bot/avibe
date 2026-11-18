/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import en from '@/i18n/en.json';
import { RouteSurfaceActiveContext } from '@/lib/routeSurfaceActivity';
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
let requestNumber = 0;
let current = request;
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  // Each case represents a distinct server request in this document.
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
    const second = { ...request, id: `mreq_f${request.id.slice(6)}` };
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

describe('Route and completion consumers from the independent review', () => {
  it.each(['succeeded', 'failed', 'expired', 'canceled'] as const)('keeps %s final while an older Native completion returns', async (state) => {
    vi.useFakeTimers(); const call = installBridge();
    let resolveNative!: (value: { state: string }) => void;
    call.mockImplementation(() => new Promise(resolve => { resolveNative = resolve; }));
    let gets = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (url === '/api/mobile/pairings') return Response.json({ pairing_code: 'one-local-code' });
      gets++;
      return Response.json(gets === 1 ? request : { ...request, state });
    });
    mount(<HealthRequestCard request={request} />); await tick();
    const button = screen.getByRole('button', { name: 'Authorize on iPhone' });
    fireEvent.click(button); fireEvent.click(button); await tick();
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings')).toHaveLength(1);
    expect(call).toHaveBeenCalledTimes(1);
    await tick(3000);
    expect(screen.getByText(en.health.note[state])).toBeTruthy();
    await act(async () => { resolveNative({ state: 'connected' }); });
    await tick(30000);
    expect(screen.getByText(en.health.note[state])).toBeTruthy();
    expect(gets).toBe(2);
  });

  it('does not apply an old Native receipt to the replacement request', async () => {
    vi.useFakeTimers(); const call = installBridge();
    let resolveNative!: (value: { state: string }) => void;
    call.mockImplementation(() => new Promise(resolve => { resolveNative = resolve; }));
    const second = { ...request, id: `mreq_f${request.id.slice(6)}` };
    fetchMock.mockImplementation(async (url: string) => {
      if (url === '/api/mobile/pairings') return Response.json({ pairing_code: 'old-local-code' });
      return Response.json(url.endsWith(second.id) ? second : request);
    });
    const renderCard = (value: HealthRequest) => <I18nextProvider i18n={i18n}><MemoryRouter><HealthRequestCard request={value} /></MemoryRouter></I18nextProvider>;
    const view = render(renderCard(request)); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    view.rerender(renderCard(second)); await tick();
    const blocked = screen.getByRole('button', { name: 'Authorize on iPhone' }) as HTMLButtonElement;
    expect(blocked.disabled).toBe(true);
    expect(screen.queryByText(en.health.opening)).toBeNull();
    fireEvent.click(blocked); await tick();
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings')).toHaveLength(1);
    await act(async () => { resolveNative({ state: 'declined' }); });
    expect(screen.queryByText(en.health.note.declined)).toBeNull();
    expect((screen.getByRole('button', { name: 'Authorize on iPhone' }) as HTMLButtonElement).disabled).toBe(false);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it('retires an inactive pairing and allows one fresh action after reactivation', async () => {
    vi.useFakeTimers(); const call = installBridge();
    let resolveOld!: (value: Response) => void;
    let pairs = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (url !== '/api/mobile/pairings') return Response.json(request);
      pairs++;
      if (pairs === 1) return new Promise<Response>(resolve => { resolveOld = resolve; });
      return Response.json({ pairing_code: 'new-local-code' });
    });
    const renderCard = (active: boolean) => <I18nextProvider i18n={i18n}><MemoryRouter><RouteSurfaceActiveContext.Provider value={active}><HealthRequestCard request={request} /></RouteSurfaceActiveContext.Provider></MemoryRouter></I18nextProvider>;
    const view = render(renderCard(true)); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    view.rerender(renderCard(false)); await tick(30000);
    await act(async () => { resolveOld(Response.json({ pairing_code: 'retired-local-code' })); });
    expect(call).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.filter(([url]) => url !== '/api/mobile/pairings')).toHaveLength(1);
    view.rerender(renderCard(true)); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    expect(pairs).toBe(2);
    expect(call).toHaveBeenCalledExactlyOnceWith('health.connect', { requestId: request.id, pairingCode: 'new-local-code' });
  });

  it('restarts its stopped three-failure read window only when reactivated', async () => {
    vi.useFakeTimers();
    let gets = 0;
    fetchMock.mockImplementation(async () => {
      gets++;
      if (gets <= 3) throw new Error('synthetic-outage');
      return Response.json({ ...request, state: 'succeeded' });
    });
    const renderCard = (active: boolean) => <I18nextProvider i18n={i18n}><MemoryRouter><RouteSurfaceActiveContext.Provider value={active}><HealthRequestCard request={request} /></RouteSurfaceActiveContext.Provider></MemoryRouter></I18nextProvider>;
    const view = render(renderCard(true)); await tick(120000);
    expect(gets).toBe(3); expect(screen.getByText(en.health.loadError)).toBeTruthy();
    view.rerender(renderCard(false)); await tick();
    view.rerender(renderCard(true)); await tick();
    expect(screen.getByText(en.health.note.succeeded)).toBeTruthy();
    await tick(30000); expect(gets).toBe(4);
  });
});

describe('Retained-surface Native ownership regression from the independent review', () => {
  it('keeps the existing Native action exclusive while the same retained request is hidden and restored', async () => {
    vi.useFakeTimers(); const call = installBridge();
    // Native consent/authorization completed, but setup has not returned yet.
    // S3 does not accept another connect while its connecting token is live.
    let resolveNative!: (value: { state: string }) => void;
    let nativeInFlight = false;
    call.mockImplementation(() => {
      if (nativeInFlight) return Promise.resolve({ state: 'failed' });
      nativeInFlight = true;
      return new Promise(resolve => { resolveNative = resolve; });
    });
    const renderCard = (active: boolean) => <I18nextProvider i18n={i18n}><MemoryRouter><RouteSurfaceActiveContext.Provider value={active}><HealthRequestCard request={request} /></RouteSurfaceActiveContext.Provider></MemoryRouter></I18nextProvider>;
    const view = render(renderCard(true)); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    expect(call).toHaveBeenCalledTimes(1);
    view.rerender(renderCard(false)); await tick();
    view.rerender(renderCard(true)); await tick();
    const restored = screen.getByRole('button', { name: /Authorize on iPhone|Opening authorization/ });
    fireEvent.click(restored); await tick();
    const pairingCalls = fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings').length;
    const nativeCalls = call.mock.calls.length;
    await act(async () => { resolveNative({ state: 'connected' }); });
    expect({ pairingCalls, nativeCalls, incorrectFailure: !!screen.queryByText(en.health.note.failed) })
      .toEqual({ pairingCalls: 1, nativeCalls: 1, incorrectFailure: false });
    expect(screen.getByText(en.health.note.connected)).toBeTruthy();
  });
});


const activeCard = (active: boolean, value = request) => <I18nextProvider i18n={i18n}><MemoryRouter><RouteSurfaceActiveContext.Provider value={active}><HealthRequestCard request={value} /></RouteSurfaceActiveContext.Provider></MemoryRouter></I18nextProvider>;

// These exercise shipped components with synthetic HTTP and Native peers, not a real iPhone.
describe('Document-owned connection attempts across consumers', () => {
  it('retains a completion received while hidden and resumes status polling only on reopen', async () => {
    vi.useFakeTimers(); const call = installBridge();
    let resolveNative!: (value: { state: string }) => void;
    call.mockImplementation(() => new Promise(resolve => { resolveNative = resolve; }));
    const view = render(activeCard(true)); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    view.rerender(activeCard(false)); await tick();
    const reads = fetchMock.mock.calls.filter(([url]) => url !== '/api/mobile/pairings').length;
    await act(async () => { resolveNative({ state: 'connected' }); });
    await tick(30000);
    expect(fetchMock.mock.calls.filter(([url]) => url !== '/api/mobile/pairings')).toHaveLength(reads);
    view.rerender(activeCard(true)); await tick();
    expect(screen.getByText(en.health.note.connected)).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => url !== '/api/mobile/pairings')).toHaveLength(reads + 1);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it.each(['before', 'after'])('reattaches after actual unmount when Native settles %s remount', async (completion) => {
    vi.useFakeTimers(); const call = installBridge();
    let resolveNative!: (value: { state: string }) => void;
    call.mockImplementation(() => new Promise(resolve => { resolveNative = resolve; }));
    const view = mount(<HealthRequestCard request={request} />); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    view.unmount();
    if (completion === 'before') await act(async () => { resolveNative({ state: 'connected' }); });
    mount(landing); await tick();
    if (completion === 'after') {
      const button = screen.getByRole('button', { name: en.health.opening }) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      fireEvent.click(button); await tick();
      await act(async () => { resolveNative({ state: 'connected' }); });
    }
    expect(screen.getByText(en.health.note.connected)).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings')).toHaveLength(1);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it('reserves before pairing for two consumers and delivers one receipt to both', async () => {
    vi.useFakeTimers(); const call = installBridge();
    let resolvePairing!: (value: Response) => void;
    let resolveNative!: (value: { state: string }) => void;
    call.mockImplementation(() => new Promise(resolve => { resolveNative = resolve; }));
    fetchMock.mockImplementation(async (url: string) => url === '/api/mobile/pairings'
      ? new Promise<Response>(resolve => { resolvePairing = resolve; }) : Response.json(request));
    mount(<><HealthRequestCard request={request} /><HealthRequestCard request={request} /></>); await tick();
    const buttons = screen.getAllByRole('button', { name: 'Authorize on iPhone' });
    act(() => { fireEvent.click(buttons[0]); fireEvent.click(buttons[1]); });
    await tick();
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings')).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: en.health.opening }).every(button => (button as HTMLButtonElement).disabled)).toBe(true);
    await act(async () => { resolvePairing(Response.json({ pairing_code: 'shared-synthetic-code' })); });
    expect(call).toHaveBeenCalledTimes(1);
    await act(async () => { resolveNative({ state: 'connected' }); });
    expect(screen.getAllByText(en.health.note.connected)).toHaveLength(2);
  });

  it('does not let an abandoned pairing body dispatch Native or clear its replacement lock', async () => {
    vi.useFakeTimers(); const call = installBridge();
    let resolveBody!: (value: unknown) => void;
    let resolveNative!: (value: { state: string }) => void;
    let pairs = 0;
    call.mockImplementation(() => new Promise(resolve => { resolveNative = resolve; }));
    fetchMock.mockImplementation(async (url: string) => {
      if (url !== '/api/mobile/pairings') return Response.json(request);
      if (++pairs > 1) return Response.json({ pairing_code: 'replacement-synthetic-code' });
      const response = Response.json({});
      response.json = () => new Promise(resolve => { resolveBody = resolve; });
      return response;
    });
    const view = render(activeCard(true)); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    view.rerender(activeCard(false)); await tick();
    view.rerender(activeCard(true)); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    await act(async () => { resolveBody({ pairing_code: 'abandoned-synthetic-code' }); });
    const button = screen.getByRole('button', { name: en.health.opening }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button); await tick();
    expect(pairs).toBe(2);
    expect(call).toHaveBeenCalledExactlyOnceWith('health.connect', { requestId: request.id, pairingCode: 'replacement-synthetic-code' });
    await act(async () => { resolveNative({ state: 'connected' }); });
    expect(screen.getByText(en.health.note.connected)).toBeTruthy();
  });

  it.each(['succeeded', 'failed', 'expired', 'canceled'] as const)('keeps Native exclusive after %s and preserves terminal across hide/reopen', async (state) => {
    vi.useFakeTimers(); const call = installBridge();
    let resolveNative!: (value: { state: string }) => void;
    call.mockImplementationOnce(() => new Promise(resolve => { resolveNative = resolve; }));
    const second = { ...request, id: `mreq_f${request.id.slice(6)}` };
    fetchMock.mockImplementation(async (url: string) => url === '/api/mobile/pairings'
      ? Response.json({ pairing_code: 'synthetic-code' }) : Response.json(url.endsWith(second.id) ? second : current));
    const view = render(activeCard(true)); await tick();
    fireEvent.click(screen.getByRole('button', { name: 'Authorize on iPhone' })); await tick();
    current = { ...request, state };
    await tick(3000);
    expect(screen.getByText(en.health.note[state])).toBeTruthy();
    view.rerender(activeCard(false)); await tick();
    // A stale creation-time card must not replace already observed terminal knowledge.
    view.rerender(activeCard(true)); await tick();
    expect(screen.getByText(en.health.note[state])).toBeTruthy();
    mount(<HealthRequestCard request={second} />); await tick();
    const button = screen.getByRole('button', { name: 'Authorize on iPhone' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button); await tick();
    expect(call).toHaveBeenCalledTimes(1);
    await act(async () => { resolveNative({ state: 'connected' }); });
    expect(screen.getByText(en.health.note[state])).toBeTruthy();
    expect(screen.queryByText(en.health.note.connected)).toBeNull();
    expect(button.disabled).toBe(false);
    fireEvent.click(button); await tick();
    expect(call).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings')).toHaveLength(2);
  });
});
