/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
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
  it.each([
    ['en', 1, 'card'], ['en', 1, 'landing'],
    ['en', 2, 'card'], ['en', 2, 'landing'],
    ['zh', 1, 'card'], ['zh', 1, 'landing'],
    ['zh', 2, 'card'], ['zh', 2, 'landing'],
  ] as const)('shows the initial scope supported by the %s health:%s shell on the %s', async (lng, version, surface) => {
    const localized = createInstance();
    await localized.init({ lng, resources: { en: { translation: en }, zh: { translation: zh } } });
    installBridge({ health: version });
    const legacy = ['steps', 'activity', 'workouts', 'sleep'];
    const heart = ['heart_rate', 'resting_heart_rate', 'hrv'];
    current = { ...request, metrics: [...legacy, ...heart], minimum_health_version: 1 };
    const result = render(<I18nextProvider i18n={localized}><MemoryRouter initialEntries={[`/mobile/health/${request.id}`]}>
      {surface === 'card' ? <HealthRequestCard request={current} />
        : <Routes><Route path="/mobile/health/:requestId" element={<HealthLandingPage />} /></Routes>}
    </MemoryRouter></I18nextProvider>);
    await screen.findByRole('button', { name: localized.t('health.authorize') });
    const text = result.container.textContent?.toLocaleLowerCase(lng);
    for (const metric of legacy) expect(text).toContain(localized.t(`health.metric.${metric}`).toLocaleLowerCase(lng));
    for (const metric of heart) {
      const label = localized.t(`health.metric.${metric}`).toLocaleLowerCase(lng);
      if (version === 1) expect(text).not.toContain(label);
      else expect(text).toContain(label);
    }
    // Presentation must not narrow the server request or its eventual native payload.
    expect(current.metrics).toEqual([...legacy, ...heart]);
  });

  it.each([
    ['en', ['Heart rate', 'Resting heart rate', 'Heart rate variability']],
    ['zh', ['心率', '静息心率', '心率变异性']],
  ] as const)('localizes heart metrics on the %s request card and landing page', async (lng, labels) => {
    const localized = createInstance();
    await localized.init({ lng, resources: { en: { translation: en }, zh: { translation: zh } } });
    installBridge({ health: 2 });
    current = { ...request, metrics: ['heart_rate', 'resting_heart_rate', 'hrv'], minimum_health_version: 2 };
    const result = render(<I18nextProvider i18n={localized}><MemoryRouter initialEntries={[`/mobile/health/${request.id}`]}>
      <HealthRequestCard request={current} />
      <Routes><Route path="/mobile/health/:requestId" element={<HealthLandingPage />} /></Routes>
    </MemoryRouter></I18nextProvider>);
    await screen.findByRole('heading', { name: localized.t('health.title.connect') });
    for (const label of labels) expect(result.container.textContent?.toLowerCase().split(label.toLowerCase()).length).toBeGreaterThanOrEqual(3);
    expect(result.container.textContent).not.toContain('heart_rate');
  });

  it.each([
    ['en', 'Update Avibe on this iPhone to add these. Your current sync keeps running.'],
    ['zh', '更新这台 iPhone 上的 Avibe 后才能添加这些数据，现有同步不受影响。'],
  ] as const)('explains expansion updates without implying existing sync is broken (%s)', async (lng, message) => {
    const localized = createInstance();
    await localized.init({ lng, resources: { en: { translation: en }, zh: { translation: zh } } });
    installBridge({ health: 1 });
    current = { ...request, metrics: ['heart_rate', 'resting_heart_rate', 'hrv'], minimum_health_version: 2 };
    render(<I18nextProvider i18n={localized}><MemoryRouter initialEntries={[`/mobile/health/${request.id}`]}>
      <HealthRequestCard request={current} />
      <Routes><Route path="/mobile/health/:requestId" element={<HealthLandingPage />} /></Routes>
    </MemoryRouter></I18nextProvider>);
    await waitFor(() => expect(screen.getAllByText(message)).toHaveLength(2));
    expect(screen.queryByText(localized.t('health.note.needsUpdate'))).toBeNull();
    expect(screen.queryByRole('button', { name: localized.t('health.authorize') })).toBeNull();
    for (const metric of current.metrics) {
      expect(screen.getByText((text, node) => node?.tagName === 'SPAN'
        && text.includes(localized.t(`health.metric.${metric}`)))).toBeTruthy();
    }
  });

  it('uses sentence-case metric names and the locale conjunction on the English landing', async () => {
    installBridge({ health: 2 });
    current = { ...request, metrics: ['heart_rate', 'resting_heart_rate', 'hrv'], minimum_health_version: 2 };
    mount(<Routes><Route path="/mobile/health/:requestId" element={<HealthLandingPage />} /></Routes>);
    await screen.findByText('Read this iPhone’s heart rate, resting heart rate, and heart rate variability, and sync them to this instance. Next, choose access in Avibe and iOS.');
  });

  it('requires the new native capability before expanding an existing connection', () => {
    installBridge({ health: 1 });
    const expansion = { ...request, minimum_health_version: 2, metrics: ['hrv'] };
    expect(healthRequestView(expansion)).toBe('needsUpdate');
    expect(healthRequestView(request)).toBe('authorize');
    installBridge({ health: 2 });
    expect(healthRequestView(expansion)).toBe('authorize');
  });

  it('forwards the negotiated types only to a shell supporting expansion', async () => {
    const call = installBridge({ health: 2 });
    current = { ...request, minimum_health_version: 2, metrics: ['hrv'] };
    fetchMock.mockImplementation(async (url: string) => {
      if (url === `/api/mobile/requests/${request.id}`) return Response.json(current);
      return Response.json({ pairing_code: 'synthetic-code', supported_metrics: ['steps', 'hrv'], metrics: ['hrv'], expansion: true });
    });
    mount(<Routes><Route path="/mobile/health/:requestId" element={<HealthLandingPage />} /></Routes>);
    fireEvent.click(await screen.findByRole('button', { name: 'Authorize on iPhone' }));
    await screen.findByRole('heading', { name: 'Connected' });
    expect(call).toHaveBeenCalledWith('health.connect', {
      requestId: request.id, pairingCode: 'synthetic-code', supportedMetrics: ['steps', 'hrv'],
      requestedMetrics: ['hrv'], expansion: true,
    });
  });

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
