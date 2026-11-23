/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import en from '@/i18n/en.json';
import { RouteSurfaceActiveContext } from '@/lib/routeSurfaceActivity';
import { connectHealthRequest, observeHealthTerminal, type HealthRequest } from '@/lib/healthRequests';
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
    expect(screen.queryByText(en.health.connecting)).toBeNull();
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
    expect(screen.queryByText(en.health.connecting)).toBeNull();
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
    const restored = screen.getByRole('button', { name: /Authorize on iPhone|Connecting Apple Health/ });
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
      const button = screen.getByRole('button', { name: en.health.connecting }) as HTMLButtonElement;
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
    expect(screen.getAllByRole('button', { name: en.health.preparing }).every(button => (button as HTMLButtonElement).disabled)).toBe(true);
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
    const button = screen.getByRole('button', { name: en.health.connecting }) as HTMLButtonElement;
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

describe('Recoverable native Health setup', () => {
  it('permits explicit retry after Native settles failed and the server still offers authorization', async () => {
    vi.useFakeTimers();
    const call = installBridge();
    call.mockResolvedValueOnce({ state: 'failed' }).mockResolvedValueOnce({ state: 'connected' });
    mount(<HealthRequestCard request={request} />);
    await tick();
    fireEvent.click(screen.getByRole('button', { name: en.health.authorize }));
    await tick();
    const retry = screen.getByRole('button', { name: en.health.retry }) as HTMLButtonElement;
    expect(retry.disabled).toBe(false);
    fireEvent.click(retry);
    await tick();
    expect(call).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings')).toHaveLength(2);
    expect(screen.getByText(en.health.note.connected)).toBeTruthy();
  });

  it('explains an unresponsive older shell without unlocking its live Native owner', async () => {
    vi.useFakeTimers();
    const call = installBridge();
    let resolveNative!: (value: { state: string }) => void;
    call.mockImplementationOnce(() => new Promise(resolve => { resolveNative = resolve; }));
    const view = mount(<HealthRequestCard request={request} />);
    await tick();
    fireEvent.click(screen.getByRole('button', { name: en.health.authorize }));
    await tick();
    expect(screen.getByRole('button', { name: en.health.connecting })).toBeTruthy();
    await tick(100_000);
    expect(screen.getByText(en.health.reopenApp)).toBeTruthy();
    expect((screen.getByRole('button', { name: en.health.connecting }) as HTMLButtonElement).disabled).toBe(true);
    view.unmount();
    mount(<HealthRequestCard request={request} />);
    await tick();
    expect(screen.getByText(en.health.reopenApp)).toBeTruthy();
    await connectHealthRequest(request, new AbortController().signal);
    expect(call).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings')).toHaveLength(1);
    await act(async () => { resolveNative({ state: 'failed' }); });
    await tick();
    expect((screen.getByRole('button', { name: en.health.retry }) as HTMLButtonElement).disabled).toBe(false);
  });

  it.each(['claimed', 'failed', 'expired', 'canceled', 'succeeded'] as const)(
    'never retries a local failed receipt over fresh server %s state', async (state) => {
      vi.useFakeTimers();
      const call = installBridge();
      call.mockImplementationOnce(async () => {
        current = { ...request, state };
        return { state: 'failed' };
      });
      mount(<HealthRequestCard request={request} />);
      await tick();
      fireEvent.click(screen.getByRole('button', { name: en.health.authorize }));
      await tick();
      expect(screen.queryByRole('button', { name: en.health.retry })).toBeNull();
      expect(screen.queryByRole('button', { name: en.health.authorize })).toBeNull();
      expect(call).toHaveBeenCalledTimes(1);
    },
  );
});


// Original reviewer scenarios with the desired invariant replacing defect assertions.
describe('Reviewer receipt-retention counterexamples', () => {
  it.each(['connected', 'declined'] as const)('retains completed A %s after B fails with both cards retained', async (outcome) => {
    vi.useFakeTimers(); const call = installBridge();
    const a = { ...request };
    const b = { ...request, id: `mreq_f${request.id.slice(6)}` };
    call.mockResolvedValueOnce({ state: outcome }).mockResolvedValueOnce({ state: 'failed' });
    fetchMock.mockImplementation(async (url: string) => url === '/api/mobile/pairings'
      ? Response.json({ pairing_code: 'local-only' }) : Response.json(url.endsWith(b.id) ? b : a));
    mount(<><HealthRequestCard request={a} /><HealthRequestCard request={b} /></>); await tick();
    fireEvent.click(screen.getAllByRole('button', { name: en.health.authorize })[0]); await tick();
    expect(screen.getByText(en.health.note[outcome])).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: en.health.authorize })); await tick();
    expect(call).toHaveBeenCalledTimes(2);
    expect(screen.getByText(en.health.note[outcome])).toBeTruthy();
    expect(screen.getByText(en.health.note.failed)).toBeTruthy();
    expect(screen.queryByRole('button', { name: en.health.authorize })).toBeNull();
  });

  it('retains completed A after B pairing is canceled and A is truly remounted', async () => {
    vi.useFakeTimers(); const call = installBridge();
    const a = { ...request };
    const b = { ...request, id: `mreq_f${request.id.slice(6)}` };
    let pairs = 0;
    let resolveAbandoned!: (value: Response) => void;
    fetchMock.mockImplementation(async (url: string) => {
      if (url !== '/api/mobile/pairings') return Response.json(url.endsWith(b.id) ? b : a);
      if (++pairs === 1) return Response.json({ pairing_code: 'a-code' });
      return new Promise<Response>(resolve => { resolveAbandoned = resolve; });
    });
    const first = mount(<HealthRequestCard request={a} />); await tick();
    fireEvent.click(screen.getByRole('button', { name: en.health.authorize })); await tick();
    expect(screen.getByText(en.health.note.connected)).toBeTruthy(); first.unmount();
    const second = mount(<HealthRequestCard request={b} />); await tick();
    fireEvent.click(screen.getByRole('button', { name: en.health.authorize })); await tick(); second.unmount();
    await act(async () => { resolveAbandoned(Response.json({ pairing_code: 'canceled-b' })); });
    mount(<HealthRequestCard request={a} />); await tick();
    expect(call).toHaveBeenCalledTimes(1);
    expect(screen.getByText(en.health.note.connected)).toBeTruthy();
    expect(screen.queryByRole('button', { name: en.health.authorize })).toBeNull();
  });

  it('shares confirmed terminal knowledge despite an older same-request queued GET', async () => {
    vi.useFakeTimers(); const call = installBridge();
    const a = { ...request };
    let gets = 0;
    let resolveStale!: (value: Response) => void;
    fetchMock.mockImplementation(async (url: string) => {
      if (url === '/api/mobile/pairings') return Response.json({ pairing_code: 'a-code' });
      if (++gets === 1) return new Promise<Response>(resolve => { resolveStale = resolve; });
      return Response.json({ ...a, state: 'succeeded' });
    });
    mount(<><HealthRequestCard request={a} /><HealthRequestCard request={a} /></>); await tick();
    expect(screen.getAllByText(en.health.note.succeeded)).toHaveLength(2);
    await act(async () => { resolveStale(Response.json(a)); });
    expect(screen.queryByRole('button', { name: en.health.authorize })).toBeNull();
    expect(call).not.toHaveBeenCalled();
  });
});


const relatedRequest = (prefix: string): HealthRequest => ({ ...request, id: `mreq_${prefix}${request.id.slice(6)}` });
const pairingIds = () => fetchMock.mock.calls.filter(([url]) => url === '/api/mobile/pairings')
  .map(([, init]) => (JSON.parse((init as RequestInit).body as string) as { request_id: string }).request_id);
const authorizeIn = (view: ReturnType<typeof mount>) => fireEvent.click(within(view.container).getByRole('button', { name: en.health.authorize }));
const assertReceipt = (view: ReturnType<typeof mount>, outcome: 'connected' | 'declined' | 'failed') => {
  expect(within(view.container).getByText(en.health.note[outcome])).toBeTruthy();
  expect(within(view.container).queryByRole('button', { name: en.health.authorize })).toBeNull();
};

describe('Per-request knowledge independently of the single active attempt', () => {
  for (const outcome of ['connected', 'declined', 'failed'] as const) {
    for (const next of ['failed', 'canceled', 'pending'] as const) {
      it.each(['retained', 'remounted'] as const)(`preserves A ${outcome} when B is ${next}, with A %s`, async (surface) => {
        vi.useFakeTimers(); const call = installBridge();
        const a = { ...request }; const b = relatedRequest('b'); const c = relatedRequest('c');
        let resolveNative!: (value: { state: string }) => void;
        let resolvePairing!: (value: Response) => void;
        call.mockImplementation((_method: string, params: { requestId: string }) => {
          if (params.requestId === a.id) return Promise.resolve({ state: outcome });
          if (params.requestId === b.id && next === 'pending') return new Promise(resolve => { resolveNative = resolve; });
          return Promise.resolve({ state: params.requestId === b.id ? 'failed' : 'connected' });
        });
        fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
          if (url !== '/api/mobile/pairings') return Response.json(url.endsWith(b.id) ? b : url.endsWith(c.id) ? c : a);
          if (JSON.parse(init?.body as string).request_id === b.id && next === 'canceled') {
            return new Promise<Response>(resolve => { resolvePairing = resolve; });
          }
          return Response.json({ pairing_code: 'synthetic-code' });
        });
        let first = mount(<HealthRequestCard request={a} />); await tick();
        authorizeIn(first); await tick(); assertReceipt(first, outcome);
        if (surface === 'remounted') first.unmount();
        const readsA = fetchMock.mock.calls.filter(([url]) => url.endsWith(a.id)).length;
        const second = mount(<HealthRequestCard request={b} />); await tick();
        authorizeIn(second); await tick();
        // B's publications must not be interpreted as new A outcomes that restart A's GET.
        expect(fetchMock.mock.calls.filter(([url]) => url.endsWith(a.id))).toHaveLength(readsA);
        if (next === 'canceled') {
          second.unmount();
          await act(async () => { resolvePairing(Response.json({ pairing_code: 'abandoned-b-code' })); });
        }
        if (surface === 'remounted') { first = mount(<HealthRequestCard request={a} />); await tick(); }
        assertReceipt(first, outcome);
        const third = mount(<HealthRequestCard request={c} />); await tick();
        const button = within(third.container).getByRole('button', { name: en.health.authorize }) as HTMLButtonElement;
        expect(button.disabled).toBe(next === 'pending');
        if (next === 'pending') {
          fireEvent.click(button); await tick(); expect(pairingIds()).toEqual([a.id, b.id]);
          await act(async () => { resolveNative({ state: 'connected' }); });
          expect(button.disabled).toBe(false); assertReceipt(first, outcome);
        }
        fireEvent.click(button); await tick();
        assertReceipt(first, outcome);
        expect(pairingIds()).toEqual([a.id, b.id, c.id]);
        expect(call.mock.calls.filter(([, params]) => params.requestId === a.id)).toHaveLength(1);
        expect(call.mock.calls.filter(([, params]) => params.requestId === b.id)).toHaveLength(next === 'canceled' ? 0 : 1);
      });
    }
  }

  it('reserves exactly one POST when different request consumers act in the same tick', async () => {
    vi.useFakeTimers(); const call = installBridge();
    const a = { ...request }; const b = relatedRequest('b');
    let resolvePairing!: (value: Response) => void;
    fetchMock.mockImplementation(async (url: string) => url === '/api/mobile/pairings'
      ? new Promise<Response>(resolve => { resolvePairing = resolve; }) : Response.json(url.endsWith(b.id) ? b : a));
    const first = mount(<HealthRequestCard request={a} />);
    const second = mount(<HealthRequestCard request={b} />); await tick();
    act(() => { authorizeIn(first); authorizeIn(second); }); await tick();
    expect(pairingIds()).toEqual([a.id]);
    await act(async () => { resolvePairing(Response.json({ pairing_code: 'a-code' })); });
    assertReceipt(first, 'connected');
    expect(within(second.container).queryByText(en.health.note.connected)).toBeNull();
    expect((within(second.container).getByRole('button') as HTMLButtonElement).disabled).toBe(false);
    expect(call).toHaveBeenCalledTimes(1);
  });

  for (const target of ['a', 'b'] as const) {
    for (const timing of ['before', 'after'] as const) {
      it.each(['succeeded', 'failed', 'expired', 'canceled'] as const)(`retains only ${target}'s terminal %s ${timing} B Native completion`, async (state) => {
        vi.useFakeTimers(); const call = installBridge();
        const a = { ...request }; const b = relatedRequest('b'); const c = relatedRequest('c');
        let serverA = a; let serverB = b;
        let resolveNative!: (value: { state: string }) => void;
        call.mockResolvedValueOnce({ state: 'declined' }).mockImplementationOnce(() => new Promise(resolve => { resolveNative = resolve; }));
        fetchMock.mockImplementation(async (url: string) => url === '/api/mobile/pairings'
          ? Response.json({ pairing_code: 'synthetic-code' }) : Response.json(url.endsWith(b.id) ? serverB : url.endsWith(c.id) ? c : serverA));
        const first = mount(<HealthRequestCard request={a} />); await tick();
        authorizeIn(first); await tick();
        const second = mount(<HealthRequestCard request={b} />); await tick();
        authorizeIn(second); await tick();
        const third = mount(<HealthRequestCard request={c} />); await tick();
        const button = within(third.container).getByRole('button') as HTMLButtonElement;
        if (timing === 'after') await act(async () => { resolveNative({ state: 'connected' }); });
        if (target === 'a') serverA = { ...a, state }; else serverB = { ...b, state };
        await tick(3000);
        const terminalView = target === 'a' ? first : second;
        expect(within(terminalView.container).getByText(en.health.note[state])).toBeTruthy();
        if (target === 'b') assertReceipt(first, 'declined');
        if (timing === 'before') {
          expect(button.disabled).toBe(true); fireEvent.click(button); await tick();
          expect(pairingIds()).toEqual([a.id, b.id]);
          await act(async () => { resolveNative({ state: 'connected' }); });
        }
        expect(button.disabled).toBe(false);
        expect(within(terminalView.container).getByText(en.health.note[state])).toBeTruthy();
        if (target === 'a') assertReceipt(second, 'connected'); else assertReceipt(first, 'declined');
        terminalView.unmount();
        const again = mount(<HealthRequestCard request={target === 'a' ? a : b} />); await tick();
        expect(within(again.container).getByText(en.health.note[state])).toBeTruthy();
        expect(within(again.container).queryByRole('button')).toBeNull();
        authorizeIn(third); await tick();
        expect(pairingIds()).toEqual([a.id, b.id, c.id]);
      });
    }
  }

  for (const late of ['queued', 'unknown', 'error'] as const) {
    it.each(['succeeded', 'failed', 'expired', 'canceled'] as const)(`shares terminal %s against a peer's late ${late}, then survives remount`, async (state) => {
      vi.useFakeTimers(); const call = installBridge();
      const a = { ...request };
      let gets = 0;
      let resolveOld!: (value: Response) => void;
      fetchMock.mockImplementation(() => ++gets === 1
        ? new Promise<Response>(resolve => { resolveOld = resolve; }) : Promise.resolve(Response.json({ ...a, state })));
      const first = mount(<HealthRequestCard request={a} />);
      const second = mount(<HealthRequestCard request={a} />); await tick();
      expect(screen.getAllByText(en.health.note[state])).toHaveLength(2);
      await act(async () => { resolveOld(late === 'queued' ? Response.json(a)
        : late === 'unknown' ? Response.json({ ...a, state: 'unrecognized' }) : Response.json({}, { status: 503 })); });
      await tick(30000);
      expect(screen.getAllByText(en.health.note[state])).toHaveLength(2);
      expect(screen.queryByRole('button')).toBeNull(); expect(gets).toBe(2);
      first.unmount(); second.unmount();
      fetchMock.mockResolvedValue(Response.json({}, { status: 503 }));
      mount(<HealthRequestCard request={a} />); await tick(30000);
      expect(screen.getByText(en.health.note[state])).toBeTruthy();
      expect(screen.queryByRole('button')).toBeNull();
      expect(fetchMock).toHaveBeenCalledTimes(2); // terminal knowledge needs no fresh polling window
      expect(call).not.toHaveBeenCalled();
    });
  }

  it.each(['connected', 'declined', 'failed'] as const)('keeps a remounted %s receipt through unknown live status until the server terminal arrives', async (outcome) => {
    vi.useFakeTimers(); const call = installBridge(); call.mockResolvedValue({ state: outcome });
    const a = { ...request };
    const first = mount(<HealthRequestCard request={a} />); await tick(); authorizeIn(first); await tick(); first.unmount();
    let resolveLive!: (value: Response) => void;
    fetchMock.mockImplementationOnce(() => new Promise<Response>(resolve => { resolveLive = resolve; }));
    const second = mount(<HealthRequestCard request={a} />); await tick(); assertReceipt(second, outcome);
    await act(async () => { resolveLive(Response.json({}, { status: 503 })); });
    assertReceipt(second, outcome);
    expect(screen.getByText(en.health.loadError)).toBeTruthy();
    current = { ...a, state: 'succeeded' }; await tick(3000);
    expect(screen.getByText(en.health.note.succeeded)).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
    expect(pairingIds()).toEqual([a.id]); expect(call).toHaveBeenCalledTimes(1);
  });

  it('terminalizes a pairing without losing A or allowing its late body to clear C Native', async () => {
    vi.useFakeTimers(); const call = installBridge();
    const a = { ...request }; const b = relatedRequest('b'); const c = relatedRequest('c');
    let serverB = b;
    let resolveBody!: (value: unknown) => void;
    let resolveNative!: (value: { state: string }) => void;
    call.mockResolvedValueOnce({ state: 'connected' }).mockImplementationOnce(() => new Promise(resolve => { resolveNative = resolve; }));
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url !== '/api/mobile/pairings') return Response.json(url.endsWith(b.id) ? serverB : url.endsWith(c.id) ? c : a);
      if (JSON.parse(init?.body as string).request_id !== b.id) return Response.json({ pairing_code: 'synthetic-code' });
      const response = Response.json({}); response.json = () => new Promise(resolve => { resolveBody = resolve; }); return response;
    });
    const first = mount(<HealthRequestCard request={a} />); await tick(); authorizeIn(first); await tick();
    const second = mount(<HealthRequestCard request={b} />); await tick(); authorizeIn(second); await tick();
    serverB = { ...b, state: 'canceled' }; await tick(3000);
    assertReceipt(first, 'connected'); expect(within(second.container).getByText(en.health.note.canceled)).toBeTruthy();
    const third = mount(<HealthRequestCard request={c} />); await tick(); authorizeIn(third); await tick();
    await act(async () => { resolveBody({ pairing_code: 'retired-b-code' }); });
    const button = within(third.container).getByRole('button', { name: en.health.connecting }) as HTMLButtonElement;
    expect(button.disabled).toBe(true); fireEvent.click(button); await tick();
    expect(pairingIds()).toEqual([a.id, b.id, c.id]); expect(call).toHaveBeenCalledTimes(2);
    await act(async () => { resolveNative({ state: 'connected' }); });
    assertReceipt(first, 'connected'); assertReceipt(third, 'connected');
    expect(within(second.container).getByText(en.health.note.canceled)).toBeTruthy();
  });
});

describe('Peer terminal adoption after this consumer failed a read', () => {
  for (const late of ['queued', 'error'] as const) {
    it.each(['succeeded', 'failed', 'expired', 'canceled'] as const)(`shows only the peer's %s terminal and ignores this consumer's late ${late} read`, async (state) => {
      vi.useFakeTimers(); const call = installBridge();
      const a = { ...request };
      let gets = 0;
      let resolveOld!: (value: Response) => void;
      fetchMock.mockImplementation(() => {
        gets++;
        if (gets === 1) return Promise.resolve(Response.json({}, { status: 503 }));
        // This consumer's retry ignores abort, so it can return after the peer's terminal.
        if (gets === 2) return new Promise<Response>(resolve => { resolveOld = resolve; });
        return Promise.resolve(Response.json({ ...a, state }));
      });
      const failing = mount(<HealthRequestCard request={a} />); await tick();
      expect(within(failing.container).getByText(en.health.loadError)).toBeTruthy();
      await tick(3000); expect(gets).toBe(2);
      const peer = mount(<HealthRequestCard request={a} />); await tick();
      for (const view of [failing, peer]) {
        expect(within(view.container).getByText(en.health.note[state])).toBeTruthy();
        expect(within(view.container).queryByText(en.health.loadError)).toBeNull();
        expect(within(view.container).queryByRole('button')).toBeNull();
      }
      await act(async () => { resolveOld(late === 'queued' ? Response.json(a) : Response.json({}, { status: 503 })); });
      await tick(30000);
      expect(within(failing.container).getByText(en.health.note[state])).toBeTruthy();
      expect(within(failing.container).queryByText(en.health.loadError)).toBeNull();
      expect(within(failing.container).queryByRole('button')).toBeNull();
      expect(gets).toBe(3); expect(call).not.toHaveBeenCalled();
    });
  }
});

describe('Visible state across own read, peer knowledge and route activity', () => {
  type OwnRead = 'unread' | 'live' | 'failed';
  type PeerKnowledge = 'none' | 'receipt' | 'terminal';
  const cells: Array<[OwnRead, PeerKnowledge, boolean]> = [];
  for (const own of ['unread', 'live', 'failed'] as const) {
    for (const peer of ['none', 'receipt', 'terminal'] as const) {
      for (const active of [true, false]) cells.push([own, peer, active]);
    }
  }
  it.each(cells)('own %s, peer %s, active %s', async (own, peer, active) => {
    vi.useFakeTimers(); const call = installBridge();
    const a = { ...request };
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url === '/api/mobile/pairings') return Promise.resolve(Response.json({ pairing_code: 'synthetic-code' }));
      if (own === 'unread') return stalled(init?.signal);
      return Promise.resolve(own === 'live' ? Response.json(a) : Response.json({}, { status: 503 }));
    });
    // The surface reads while visible; an inactive cell is the same consumer after it is hidden.
    const view = render(activeCard(true, a)); await tick();
    if (!active) { view.rerender(activeCard(false, a)); await tick(); }
    if (peer === 'receipt') {
      // Another consumer's settled Native result, through the document owner it would use.
      await act(async () => { await connectHealthRequest(a, new AbortController().signal); });
      expect(call).toHaveBeenCalledTimes(1);
    }
    if (peer === 'terminal') await act(async () => { observeHealthTerminal({ ...a, state: 'succeeded' }); });
    await tick();

    const shown = within(view.container);
    const expected = peer === 'terminal' ? 'succeeded' : peer === 'receipt' ? 'connected' : 'authorize';
    expect(shown.getByText(en.health.note[expected])).toBeTruthy();
    // A confirmed terminal never appears together with an unavailable status.
    const loadErrorShown = active && own === 'failed' && peer !== 'terminal';
    expect(shown.queryByText(en.health.loadError) !== null).toBe(loadErrorShown);
    // Shared receipt or terminal knowledge never opens authorization; only this surface's live read does.
    const button = shown.queryByRole('button', { name: en.health.authorize }) as HTMLButtonElement | null;
    expect(button !== null).toBe(peer === 'none');
    if (button) expect(button.disabled).toBe(!(active && own === 'live'));
  });
});
